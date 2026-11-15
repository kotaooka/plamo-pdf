# -*- coding: utf-8 -*-
"""
plamo_pdf.py — 論文PDFをローカルの PLaMo 3 Translate（llama-server）で対訳HTMLにする

前提:
  1) llama-server が起動していること（--jinja 付き）
       llama-server -hf pfnet/plamo-3-translate-8b-GGUF:Q4_K_M -ngl 99 -c 8192 --port 8000 --jinja --no-prefill-assistant
  2) pip install pymupdf

使い方（PowerShell）:
  python plamo_pdf.py paper.pdf
  python plamo_pdf.py paper.pdf --form polite --keep-refs
  → paper.ja.html（対訳）と paper.ja.cache.json（途中再開用）ができる
"""

import argparse
import html
import json
import re
import sys
import time
import urllib.request
import urllib.error
from collections import Counter
from pathlib import Path

import pymupdf  # PyMuPDF

# 参考文献の見出し（ここ以降は既定で翻訳しない）
REF_HEAD = re.compile(r"^\s*(\d+\.?\s*)?(references|bibliography|参考文献|引用文献)\s*$", re.I)


# ---------------------------------------------------------------- PDF抽出
def extract_blocks(pdf_path: Path, keep_refs: bool):
    """ページごとにテキストブロックを取り出し、段落単位に整える"""
    doc = pymupdf.open(pdf_path)
    pages = []
    for page in doc:
        h = page.rect.height
        blocks = []
        for b in page.get_text("blocks", sort=True):
            if b[6] != 0:  # 画像ブロックは除外
                continue
            # ページ上下 8% にあるブロックはヘッダー・フッター候補
            edge = b[3] < h * 0.08 or b[1] > h * 0.92
            blocks.append((b[4], edge))
        pages.append(blocks)

    # 上下端で全ページ的に繰り返すヘッダー・フッター（ページ番号含む）を除去
    def norm(t):
        return re.sub(r"\d+", "#", t.strip())

    cnt = Counter(norm(t) for blks in pages for t in {t for t, e in blks if e})
    th = max(2, len(pages) // 2)

    result = []
    stop = False
    for pno, blks in enumerate(pages, 1):
        paras = []
        for t, edge in blks:
            if edge and (cnt[norm(t)] >= th or re.fullmatch(r"\s*\d{1,4}\s*", t)):
                continue
            text = clean(t)
            if not text:
                continue
            if not keep_refs and REF_HEAD.match(text):
                stop = True
                break
            paras.append(text)
        result.append((pno, paras))
        if stop:
            break
    return result


def clean(t: str) -> str:
    """行末ハイフネーションの結合・改行の連結"""
    t = t.replace("­", "")
    t = re.sub(r"(\w)-\n(\w)", r"\1\2", t)  # hyphen-\nation → hyphenation
    t = re.sub(r"\s*\n\s*", " ", t)
    t = re.sub(r"\s{2,}", " ", t)
    return t.strip()


def should_translate(text: str) -> bool:
    """数式・数値表・短すぎる断片は訳さない"""
    letters = sum(c.isalpha() for c in text)
    if letters < 8:
        return False
    if letters / max(len(text), 1) < 0.5:
        return False
    return True


# ---------------------------------------------------------------- 翻訳
class Translator:
    def __init__(self, url, target, source, form, use_context, timeout):
        self.url = url.rstrip("/") + "/v1/chat/completions"
        self.target, self.source, self.form = target, source, form
        self.use_context = use_context
        self.timeout = timeout
        self.prev = None  # 直前の原文段落（文脈として渡す）

    def __call__(self, text: str) -> str:
        kw = {"target_lang": self.target}
        if self.source:
            kw["source_lang"] = self.source
        if self.form and self.target == "ja":
            kw["form"] = self.form
        if self.use_context and self.prev:
            kw["context"] = self.prev[-600:]
        body = {
            "model": "plamo-3-translate",
            "messages": [{"role": "user", "content": text}],
            "chat_template_kwargs": kw,
            "temperature": 0,
            "max_tokens": max(256, len(text) * 2),
            "stream": False,
        }
        req = urllib.request.Request(
            self.url,
            data=json.dumps(body).encode("utf-8"),
            headers={"Content-Type": "application/json"},
        )
        for attempt in range(3):
            try:
                with urllib.request.urlopen(req, timeout=self.timeout) as r:
                    out = json.load(r)["choices"][0]["message"]["content"]
                self.prev = text
                return out.strip()
            except (urllib.error.URLError, TimeoutError) as e:
                if attempt == 2:
                    raise
                print(f"  再試行 {attempt + 1}/2: {e}", file=sys.stderr)
                time.sleep(3)


# ---------------------------------------------------------------- HTML出力
CSS = """
body{font-family:system-ui,'Noto Sans JP',sans-serif;margin:0;background:#fafafa;color:#222;line-height:1.75}
header{position:sticky;top:0;background:#fff;border-bottom:1px solid #ddd;padding:8px 14px;z-index:1;
 display:flex;gap:10px;align-items:center;flex-wrap:wrap}
header h1{font-size:15px;margin:0;flex:1;min-width:200px;overflow-wrap:anywhere}
header label{font-size:13px;white-space:nowrap}
main{max-width:1400px;margin:0 auto;padding:10px 14px 60px}
h2{font-size:13px;color:#888;border-top:1px solid #ddd;padding-top:6px;margin:28px 0 8px}
.p{display:grid;grid-template-columns:1fr 1fr;gap:18px;padding:8px 0;border-bottom:1px dashed #e4e4e4}
.src{color:#555;font-family:Georgia,serif;font-size:15px}
.dst{font-size:15.5px}
.raw .dst{display:none}.raw{grid-template-columns:1fr}
body.hide-src .src{display:none} body.hide-src .p{grid-template-columns:1fr}
@media (max-width:800px){
 .p{grid-template-columns:1fr;gap:4px}
 .src{font-size:13px;order:2;border-left:3px solid #ddd;padding-left:8px}
 .dst{order:1}
}
"""

JS = """
const cb=document.getElementById('t');
cb.onchange=()=>document.body.classList.toggle('hide-src',!cb.checked);
"""


def write_html(out: Path, title: str, items):
    parts = [
        "<!DOCTYPE html><html lang='ja'><head><meta charset='utf-8'>",
        "<meta name='viewport' content='width=device-width,initial-scale=1'>",
        f"<title>{html.escape(title)}</title><style>{CSS}</style></head><body>",
        f"<header><h1>{html.escape(title)}</h1>",
        "<label><input type='checkbox' id='t' checked> 原文を表示</label></header><main>",
    ]
    cur = None
    for pno, src, dst in items:
        if pno != cur:
            parts.append(f"<h2>p.{pno}</h2>")
            cur = pno
        s = html.escape(src)
        if dst is None:
            parts.append(f"<div class='p raw'><div class='src'>{s}</div></div>")
        else:
            parts.append(f"<div class='p'><div class='src'>{s}</div><div class='dst'>{html.escape(dst)}</div></div>")
    parts.append(f"</main><script>{JS}</script></body></html>")
    out.write_text("".join(parts), encoding="utf-8")


# ---------------------------------------------------------------- メイン
def main():
    ap = argparse.ArgumentParser(description="PLaMo 3 Translate で論文PDFを対訳HTMLにする")
    ap.add_argument("pdf", type=Path)
    ap.add_argument("--url", default="http://127.0.0.1:8000", help="llama-server のURL")
    ap.add_argument("--to", default="ja", help="翻訳先言語（既定 ja）")
    ap.add_argument("--from", dest="src", default="en", help="原文の言語（空文字で自動検出）")
    ap.add_argument("--form", choices=["plain", "polite"], default="plain",
                    help="日本語の文体 plain=である調 / polite=です・ます調")
    ap.add_argument("--no-context", action="store_true", help="直前段落を文脈として渡さない")
    ap.add_argument("--keep-refs", action="store_true", help="参考文献も翻訳する")
    ap.add_argument("--timeout", type=int, default=600)
    a = ap.parse_args()

    if not a.pdf.exists():
        sys.exit(f"ファイルがありません: {a.pdf}")

    out = a.pdf.with_suffix(".ja.html")
    cache_path = a.pdf.with_suffix(".ja.cache.json")
    cache = json.loads(cache_path.read_text(encoding="utf-8")) if cache_path.exists() else {}

    pages = extract_blocks(a.pdf, a.keep_refs)
    total = sum(len(p) for _, p in pages)
    print(f"{a.pdf.name}: {len(pages)}ページ / {total}段落")

    tr = Translator(a.url, a.to, a.src or None, a.form, not a.no_context, a.timeout)
    items, done, t0 = [], 0, time.time()
    try:
        for pno, paras in pages:
            for text in paras:
                done += 1
                if not should_translate(text):
                    items.append((pno, text, None))
                    continue
                key = f"{a.to}|{a.form}|{text}"  # 訳先言語・文体が変わったら訳し直す
                if key in cache:
                    dst = cache[key]
                    tr.prev = text
                else:
                    dst = tr(text)
                    cache[key] = dst
                    cache_path.write_text(json.dumps(cache, ensure_ascii=False), encoding="utf-8")
                items.append((pno, text, dst))
                el = time.time() - t0
                print(f"\r  {done}/{total} 段落  経過 {el:5.0f} s", end="", flush=True)
            write_html(out, a.pdf.stem, items)  # ページごとに途中保存
    except KeyboardInterrupt:
        print("\n中断しました（再実行すると続きから訳します）")
    except urllib.error.URLError as e:
        print(f"\nllama-server に接続できません（{a.url}）: {e}")
        print("llama-server を --jinja 付きで起動しているか確認してください。")
    write_html(out, a.pdf.stem, items)
    print(f"\n出力: {out}")


if __name__ == "__main__":
    main()
