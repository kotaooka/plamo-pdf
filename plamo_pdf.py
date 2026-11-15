# -*- coding: utf-8 -*-
"""
plamo_pdf.py — 論文PDFをローカルの PLaMo 3 Translate（llama-server）で対訳HTMLにする

前提:
  pip install -r requirements.txt   （pymupdf）
  llama.cpp の llama-server（PATH / 環境変数 LLAMA_SERVER / LM Studio 同梱版を自動で探す）

使い方（PowerShell）:
  python plamo_pdf.py paper.pdf                    # 8B で翻訳（サーバーとモデルは自動で用意）
  python plamo_pdf.py paper.pdf --model 0.8b       # 軽量モデル（VRAM 2 GB 程度で動く）
  python plamo_pdf.py a.pdf b.pdf --form polite --open
  → paper.ja.html（対訳）と paper.ja.cache.json（途中再開用）ができる
"""

import argparse
import html
import json
import os
import re
import shutil
import subprocess
import sys
import time
import unicodedata
import urllib.parse
import urllib.request
import urllib.error
from collections import Counter
from pathlib import Path

try:
    import pymupdf  # PyMuPDF
except ImportError:
    sys.exit("pymupdf がありません。先に  pip install -r requirements.txt  を実行してください。")

# 選べるモデル（Hugging Face のリポジトリ, ファイル名）
MODELS = {
    "8b": ("pfnet/plamo-3-translate-8b-GGUF", "plamo-3-translate-8b-Q4_K_M.gguf"),      # 4.9 GB, VRAM 約6.5 GB
    "0.8b": ("pfnet/plamo-3-translate-0.8b-GGUF", "plamo-3-translate-0.8b-Q4_K_M.gguf"),  # 0.6 GB, VRAM 約1 GB
}
DEFAULT_MODEL = "8b"
MODELS_DIR = Path(os.environ.get("PLAMO_PDF_MODELS", Path.home() / ".cache" / "plamo-pdf"))

# 参考文献の見出し（ここ以降は既定で翻訳しない）
REF_HEAD = re.compile(r"^\s*(\d+\.?\s*)?(references|bibliography|参考文献|引用文献)\s*$", re.I)
# 見出しのない文献リストの先頭（APS系など）: "[1] J. Meixner and ..." / "[1] A. B. Smith, ..."
REF_FIRST = re.compile(r"^\s*\[1\]\s+(?:[A-Z][a-z]?\.\s*-?\s*)+[A-Z]")
REF_ITEM = re.compile(r"^\s*\[\d+\]\s+(?:[A-Z][a-z]?\.\s*-?\s*)+[A-Z]")
# 分離アクセント（´ ` ¨ ˇ ˜）→ 結合文字
ACCENT = {"\u00b4": "\u0301", "`": "\u0300", "\u00a8": "\u0308", "\u02c7": "\u030c", "\u02dc": "\u0303"}


def reading_order(blocks, width):
    """2段組を考慮して読み順に並べる。
    両段にまたがるブロック（タイトル・アブストラクト・全幅の図表など）で縦に区切り、
    各区間の中では 左段（上→下）→ 右段（上→下）の順にする。1段組ではすべて全幅扱いになる。
    戻り値は (block, col) の列。col は 0=左段または全幅, 1=右段。"""
    mid, margin = width / 2, width * 0.05
    full, cols = [], []
    for b in blocks:
        (full if b[0] < mid - margin and b[2] > mid + margin else cols).append(b)
    full.sort(key=lambda b: b[1])
    out, top = [], float("-inf")
    for bottom_block in full + [None]:
        bottom = bottom_block[1] if bottom_block else float("inf")
        seg = [b for b in cols if top <= (b[1] + b[3]) / 2 < bottom]
        out += [(b, 0) for b in sorted((b for b in seg if b[0] < mid), key=lambda b: (b[1], b[0]))]
        out += [(b, 1) for b in sorted((b for b in seg if b[0] >= mid), key=lambda b: (b[1], b[0]))]
        if bottom_block:
            out.append((bottom_block, 0))
            top = (bottom_block[1] + bottom_block[3]) / 2
    return out


# ---------------------------------------------------------------- PDF抽出
def extract_blocks(pdf_path: Path, keep_refs: bool):
    """ページごとにテキストブロックを取り出し、段落単位に整える"""
    doc = pymupdf.open(pdf_path)
    pages = []
    for page in doc:
        h = page.rect.height
        blocks = []
        for b, col in reading_order(page.get_text("blocks"), page.rect.width):
            if b[6] != 0:  # 画像ブロックは除外
                continue
            # ページ上下 8% にあるブロックはヘッダー・フッター候補
            edge = b[3] < h * 0.08 or b[1] > h * 0.92
            blocks.append((b[4], edge, b[1], col))
        pages.append(blocks)

    # 上下端で全ページ的に繰り返すヘッダー・フッター（ページ番号含む）を除去
    def norm(t):
        return re.sub(r"\d+", "#", t.strip())

    cnt = Counter(t for blks in pages for t in {norm(b[0]) for b in blks if b[1]})  # 1ページ1回まで数える
    th = max(2, len(pages) // 2)

    result = []
    for pno, blks in enumerate(pages, 1):
        items = []
        for t, edge, y0, col in blks:
            if edge and (cnt[norm(t)] >= th or re.fullmatch(r"\s*\d{1,4}\s*", t)):
                continue
            text = clean(t)
            if text:
                items.append((text, y0, col))
        # 参考文献の開始位置を探す。そこから下で、同じ段かそれより後ろの段にあるブロックは文献とみなす。
        # （左段の途中から始まる場合: 両段とも下は文献。右段から始まる場合: 左段は本文のまま）
        ref = None
        if not keep_refs:
            ref = next(((y0, col) for text, y0, col in items
                        if REF_HEAD.match(text) or REF_FIRST.match(text)), None)
        if ref:
            # 後ろの段では、文献番号で始まるブロック（"[10] L. Evans, ..."）が開始位置より上にあればそこから切る
            cut = {ref[1]: ref[0]}
            for text, y0, col in items:
                if col > ref[1] and REF_ITEM.match(text):
                    cut[col] = min(cut.get(col, ref[0]), y0)
            items = [it for it in items if not (it[2] >= ref[1] and it[1] >= cut.get(it[2], ref[0]))]
        result.append((pno, [text for text, _, _ in items]))
        if ref:
            break
    return result


def clean(t: str) -> str:
    """合字・分離アクセント・制御文字の正規化、行末ハイフネーションの結合・改行の連結"""
    t = t.replace("­", "")
    # TeX由来の分離アクセント "V´aclav" → "Václav"（NFKC より先に。NFKC は ´ を空白+結合文字にする）
    t = re.sub("[" + "".join(ACCENT) + r"]\s?([A-Za-z])",
               lambda m: unicodedata.normalize("NFC", m.group(1) + ACCENT[m.group(0)[0]]), t)
    t = unicodedata.normalize("NFKC", t)  # ﬁ ﬂ などの合字を分解
    t = re.sub(r"[\x00-\x08\x0b-\x1f\x7f]", "", t)  # 数式フォント由来の制御文字
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


# ---------------------------------------------------------------- llama-server の用意
def find_server(explicit: str | None) -> Path | None:
    """llama-server を探す: --server → 環境変数 LLAMA_SERVER → PATH → LM Studio 同梱版（CUDA 優先・新しい順）"""
    for c in (explicit, os.environ.get("LLAMA_SERVER")):
        if c:
            p = Path(c)
            if p.is_dir():
                p = p / ("llama-server.exe" if os.name == "nt" else "llama-server")
            return p if p.exists() else None
    w = shutil.which("llama-server")
    if w:
        return Path(w)
    backends = Path.home() / ".lmstudio" / "extensions" / "backends"
    if backends.is_dir():
        exe = "llama-server.exe" if os.name == "nt" else "llama-server"
        found = [p for p in backends.glob(f"llama.cpp-*/{exe}")]

        def rank(p):
            name = p.parent.name
            m = re.search(r"(\d+)\.(\d+)\.(\d+)$", name)
            ver = tuple(map(int, m.groups())) if m else ()
            return ("cuda" in name, "vulkan" in name, ver)

        if found:
            return max(found, key=rank)
    return None


def download(url: str, dst: Path):
    dst.parent.mkdir(parents=True, exist_ok=True)
    part = dst.with_suffix(dst.suffix + ".part")
    req = urllib.request.Request(url, headers={"User-Agent": "plamo-pdf"})
    with urllib.request.urlopen(req, timeout=60) as r, open(part, "wb") as f:
        total = int(r.headers.get("Content-Length") or 0)
        got, t0 = 0, time.time()
        while chunk := r.read(1 << 20):
            f.write(chunk)
            got += len(chunk)
            if total:
                mbps = got / 1e6 / max(time.time() - t0, 1e-3)
                print(f"\r  {got / 1e9:.2f} / {total / 1e9:.2f} GB  ({mbps:.0f} MB/s)", end="", flush=True)
    print()
    part.replace(dst)


def resolve_model(name: str) -> Path:
    """--model の値（8b / 0.8b / .gguf のパス）からモデルファイルを返す。無ければダウンロードする"""
    p = Path(name)
    if p.suffix.lower() == ".gguf":
        if not p.exists():
            sys.exit(f"モデルファイルがありません: {p}")
        return p
    key = name.lower()
    if key not in MODELS:
        sys.exit(f"--model は {' / '.join(MODELS)} か .gguf ファイルのパスで指定してください: {name}")
    repo, fname = MODELS[key]
    # 既にある場所を優先（このツールの保存先 → LM Studio のモデルフォルダ）
    for cand in (MODELS_DIR / fname, Path.home() / ".lmstudio" / "models" / repo / fname):
        if cand.exists():
            return cand
    dst = MODELS_DIR / fname
    print(f"モデルをダウンロードします: {repo}/{fname} → {dst}")
    download(f"https://huggingface.co/{repo}/resolve/main/{fname}", dst)
    return dst


def http_json(url: str, timeout=3):
    with urllib.request.urlopen(url, timeout=timeout) as r:
        return json.load(r)


def server_model(url: str) -> str | None:
    """起動中サーバーが読み込んでいるモデル名（起動していなければ None）"""
    try:
        if http_json(url + "/health").get("status") != "ok":
            return None
        return Path(http_json(url + "/v1/models")["data"][0]["id"]).stem
    except (urllib.error.URLError, OSError, KeyError, IndexError, ValueError):
        return None


class Server:
    """llama-server を起動し、終了時に止める（既に起動中ならそれを使う）"""

    def __init__(self, a):
        self.url = a.url.rstrip("/")
        self.proc = None
        running = server_model(self.url)
        if running:
            want = a.model and Path(MODELS.get(a.model.lower(), ("", a.model))[1]).stem
            if want and want != running:
                sys.exit(f"{self.url} では別のモデル（{running}）が動いています。"
                         f"そのサーバーを止めるか、--url で別のポートを指定してください。")
            print(f"起動中の llama-server を使います（{running}）")
            self.model = running
            return

        model = resolve_model(a.model or DEFAULT_MODEL)
        exe = find_server(a.server)
        if not exe:
            sys.exit("llama-server が見つかりません。llama.cpp の Releases から入手し、"
                     "--server でパスを指定するか PATH に入れてください（README 参照）。")
        port = urllib.parse.urlparse(self.url).port or 8000
        cmd = [str(exe), "-m", str(model), "-ngl", str(a.gpu_layers), "-c", str(a.ctx),
               "--port", str(port), "--jinja", "--no-prefill-assistant"]
        MODELS_DIR.mkdir(parents=True, exist_ok=True)
        self.log = MODELS_DIR / "llama-server.log"
        print(f"llama-server を起動します: {model.name}（{exe}）")
        with open(self.log, "wb") as lf:
            self.proc = subprocess.Popen(cmd, stdout=lf, stderr=subprocess.STDOUT)
        self.model = model.stem
        t0 = time.time()
        while time.time() - t0 < 600:
            if self.proc.poll() is not None:
                self._fail("llama-server が終了しました")
            if server_model(self.url):
                print(f"  準備完了（{time.time() - t0:.0f} s）")
                return
            time.sleep(1)
        self._fail("llama-server が 600 秒以内に起動しませんでした")

    def _fail(self, msg):
        tail = self.log.read_text(encoding="utf-8", errors="replace").splitlines()[-15:]
        self.close()
        sys.exit(msg + "\n--- " + str(self.log) + " ---\n" + "\n".join(tail))

    def close(self):
        if self.proc and self.proc.poll() is None:
            self.proc.terminate()
            try:
                self.proc.wait(10)
            except subprocess.TimeoutExpired:
                self.proc.kill()
        self.proc = None


# ---------------------------------------------------------------- メイン
def translate_pdf(pdf: Path, a, model_tag: str) -> Path:
    out = pdf.with_suffix(".ja.html")
    cache_path = pdf.with_suffix(".ja.cache.json")
    cache = json.loads(cache_path.read_text(encoding="utf-8")) if cache_path.exists() else {}

    pages = extract_blocks(pdf, a.keep_refs)
    total = sum(len(p) for _, p in pages)
    print(f"{pdf.name}: {len(pages)}ページ / {total}段落")

    tr = Translator(a.url, a.to, a.src or None, a.form, not a.no_context, a.timeout)
    items, done, t0 = [], 0, time.time()
    try:
        for pno, paras in pages:
            for text in paras:
                done += 1
                if not should_translate(text):
                    items.append((pno, text, None))
                    continue
                key = f"{model_tag}|{a.to}|{a.form}|{text}"  # モデル・訳先言語・文体が変わったら訳し直す
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
            write_html(out, f"{pdf.stem}（{model_tag}）", items)  # ページごとに途中保存
    finally:
        write_html(out, f"{pdf.stem}（{model_tag}）", items)
        print(f"\n出力: {out}")
    return out


def main():
    for s in (sys.stdout, sys.stderr):  # cp932 のコンソールで表示できない文字があっても落ちないように
        s.reconfigure(errors="replace")
    ap = argparse.ArgumentParser(description="PLaMo 3 Translate で論文PDFを対訳HTMLにする")
    ap.add_argument("pdf", type=Path, nargs="+", help="翻訳するPDF（複数可）")
    ap.add_argument("--model", default=None,
                    help=f"使うモデル: {' / '.join(MODELS)} または .gguf のパス（既定 {DEFAULT_MODEL}）")
    ap.add_argument("--url", default="http://127.0.0.1:8000", help="llama-server のURL")
    ap.add_argument("--server", default=None, help="llama-server 実行ファイルのパス（既定は自動検出）")
    ap.add_argument("--gpu-layers", type=int, default=99, help="GPU に載せる層数（VRAM 不足なら減らす）")
    ap.add_argument("--ctx", type=int, default=8192, help="コンテキスト長")
    ap.add_argument("--to", default="ja", help="翻訳先言語（既定 ja）")
    ap.add_argument("--from", dest="src", default="en", help="原文の言語（空文字で自動検出）")
    ap.add_argument("--form", choices=["plain", "polite"], default="plain",
                    help="日本語の文体 plain=である調 / polite=です・ます調")
    ap.add_argument("--no-context", action="store_true", help="直前段落を文脈として渡さない")
    ap.add_argument("--keep-refs", action="store_true", help="参考文献も翻訳する")
    ap.add_argument("--timeout", type=int, default=600, help="1段落あたりのタイムアウト [s]")
    ap.add_argument("--open", action="store_true", help="終わったら対訳HTMLをブラウザで開く")
    a = ap.parse_args()

    for p in a.pdf:
        if not p.exists():
            sys.exit(f"ファイルがありません: {p}")

    server = Server(a)
    try:
        for pdf in a.pdf:
            out = translate_pdf(pdf, a, server.model)
            if a.open:
                import webbrowser
                webbrowser.open(out.resolve().as_uri())
    except KeyboardInterrupt:
        print("\n中断しました（再実行すると続きから訳します）")
    except urllib.error.URLError as e:
        print(f"\nllama-server に接続できません（{a.url}）: {e}")
    finally:
        server.close()


if __name__ == "__main__":
    main()
