# -*- coding: utf-8 -*-
"""
plamo_pdf.py — 論文PDFをローカルの PLaMo 3 Translate（llama-server）で翻訳し、
               対訳HTML と 元のレイアウトのまま訳文に置き換えたPDF にする

前提:
  pip install -r requirements.txt   （pymupdf）
  llama.cpp の llama-server（PATH / 環境変数 LLAMA_SERVER / LM Studio 同梱版を自動で探す）

使い方（PowerShell）:
  python plamo_pdf.py paper.pdf                    # 8B で翻訳（サーバーとモデルは自動で用意）
  python plamo_pdf.py paper.pdf --model 0.8b       # 軽量モデル（VRAM 2 GB 程度で動く）
  python plamo_pdf.py a.pdf b.pdf --form polite --open html,pdf
  python plamo_pdf.py paper.pdf -o D:\\翻訳 --format pdf,dual --open none
  → paper.ja.html（対訳）・paper.ja.pdf（レイアウト維持）ができ、終わると paper.ja.pdf が開く
  出力先・出力形式・終わったら開くファイルの既定値は config.ini で変えられる
"""

import argparse
import configparser
import hashlib
import html
import io
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
from typing import NamedTuple

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
class Para(NamedTuple):
    """1段落（= PDF の1テキストブロック）。レイアウト置き換え用に位置と書式も持つ"""
    text: str
    rect: tuple       # (x0, y0, x1, y1) [pt]
    size: float       # 主な文字サイズ [pt]
    color: str        # 主な文字色 "#rrggbb"
    bold: bool        # 太字が主か
    horizontal: bool  # すべての行が横書き（回転していない）か
    center: bool      # ページ中央にそろえた1〜2行のブロック（タイトルなど）か
    table: bool       # 表の行（同じ高さに4つ以上の行が横に並ぶ）か。訳さない
    leading: float    # 元の行送り ÷ 文字サイズ（訳文もできるだけこの行間で組む）


# 箇条書き・選択肢の行頭: "(A)" "(1)" "a)" "2." "•" など（後ろは空白か行末）
LIST_MARK = re.compile(r"^\s*(?:\([A-Za-z0-9]{1,3}\)|[A-Za-z0-9]{1,3}[.)]|[•●▪◦‣\-–])(?=\s|$)")


def line_text(line) -> str:
    return "".join(s["text"] for s in line["spans"])


def block_rows(lines):
    """行を見た目の行（同じ高さに並ぶ行のまとまり）に分け、上から順に返す。各行は左から右へ"""
    rows = []
    for line in sorted(lines, key=lambda l: (l["bbox"][1], l["bbox"][0])):
        a = line["bbox"]
        for row in rows:
            b = row["bbox"]
            if min(a[3], b[3]) - max(a[1], b[1]) > 0.5 * min(a[3] - a[1], b[3] - b[1]):
                row["lines"].append(line)
                row["bbox"] = (min(a[0], b[0]), min(a[1], b[1]), max(a[2], b[2]), max(a[3], b[3]))
                break
        else:
            rows.append({"lines": [line], "bbox": tuple(a)})
    for row in rows:
        row["lines"].sort(key=lambda l: l["bbox"][0])
        row["text"] = " ".join(line_text(l).strip() for l in row["lines"])
    return rows


def split_block(d: dict, page_width: float):
    """1つのテキストブロックを段落単位に分ける。
    - 選択肢・箇条書き（"(A) ..." "2. ..." の行）は1項目ずつ
    - "(1)" だけの行（段落番号）は、続く本文と分ける
    - ページ中央にそろえた短い行だけのブロック（"TOEFL iBT Sample / Reading / Cougars" のような表題）は1行ずつ
    MuPDF はこれらを1ブロックにまとめるので、そのままだと改行が消えて1文として訳され、配置も崩れる"""
    if is_table_row(d["lines"]):
        return [d]
    rows = block_rows(d["lines"])
    if len(rows) < 2:
        return [d]
    x0, _, x1, _ = d["bbox"]
    width = x1 - x0
    titles = len(rows) <= 4 and all(
        abs((r["bbox"][0] + r["bbox"][2]) / 2 - page_width / 2) < page_width * 0.03
        and r["bbox"][2] - r["bbox"][0] < page_width * 0.5 for r in rows)
    segs = [[rows[0]]]
    for prev, row in zip(rows, rows[1:]):
        prev_text = prev["text"].strip()
        mark_only = LIST_MARK.match(prev_text) and not LIST_MARK.sub("", prev_text).strip()
        # 行頭記号の行で切るのは、前の行が文末で終わるか短いとき、または前の項目も行頭記号で始まるとき
        # （本文の途中で行頭に来た "1." と区別する）
        new_item = LIST_MARK.match(row["text"]) and (
            re.search(r"[.?!:)\"”]$", prev_text) or prev["bbox"][2] - prev["bbox"][0] < width * 0.8
            or LIST_MARK.match(segs[-1][0]["text"]))
        if titles or mark_only or new_item:
            segs.append([row])
        else:
            segs[-1].append(row)
    if len(segs) == 1:
        return [d]
    out = []
    for seg in segs:
        lines = [l for r in seg for l in r["lines"]]
        bb = (min(r["bbox"][0] for r in seg), min(r["bbox"][1] for r in seg),
              max(r["bbox"][2] for r in seg), max(r["bbox"][3] for r in seg))
        ids = {id(l) for l in lines}
        # 文字は MuPDF の行の順のまま（行の並べ替えで分数の分子などの位置が変わらないように）
        text = "\n".join(line_text(l) for l in d["lines"] if id(l) in ids)
        out.append({"bbox": bb, "lines": lines, "text": text})
    return out


def is_table_row(lines) -> bool:
    """同じ高さに4つ以上の行が横に並んでいれば、表の見出し行・データ行とみなす。
    （MuPDF は表のセルを別々の行として取り出す。節番号＋見出しや「Encoder: 本文」は2つまで）"""
    boxes = [line["bbox"] for line in lines]
    for a in boxes:
        h = a[3] - a[1]
        side = sum(1 for b in boxes
                   if min(a[3], b[3]) - max(a[1], b[1]) > 0.5 * min(h, b[3] - b[1]))
        if side >= 4:
            return True
    return False


def block_style(d: dict, page_width: float):
    """dict 形式のブロックから、文字数で重み付けした主な文字サイズ・色・太字を求める"""
    size, color, bold = Counter(), Counter(), Counter()
    horizontal = True
    for line in d.get("lines", []):
        if abs(line["dir"][0] - 1) > 1e-3:
            horizontal = False
        for s in line["spans"]:
            n = len(s["text"].strip())
            size[round(s["size"], 1)] += n
            color[s["color"]] += n
            bold[bool(s["flags"] & 16) or "bold" in s["font"].lower()] += n
    if not size:
        return 10.0, "#000000", False, horizontal, False, False, 1.25
    x0, _, x1, _ = d["bbox"]
    rows = block_rows(d["lines"])
    # 1〜2行の各行がページ中央にそろっている（2行段落の短い最終行は左寄りなので除かれる）
    # 本文幅いっぱいの2行段落も各行が中央に来るので、行が短いか、行の幅がそろっていない（＝中央寄せ）ときだけ
    widths = [r["bbox"][2] - r["bbox"][0] for r in rows]
    center = (len(rows) <= 2 and x1 - x0 < page_width * 0.8 and all(
        abs((r["bbox"][0] + r["bbox"][2]) / 2 - page_width / 2) < page_width * 0.02 for r in rows)
        and (max(widths) < page_width * 0.55 or min(widths) < max(widths) * 0.9))
    main_size = size.most_common(1)[0][0]
    # 行送り = 隣り合う行の下端の間隔の中央値。1行だけなら行の高さから
    steps = sorted(b["bbox"][3] - a["bbox"][3] for a, b in zip(rows, rows[1:]))
    pitch = steps[len(steps) // 2] if steps else rows[0]["bbox"][3] - rows[0]["bbox"][1]
    leading = min(max(pitch / main_size, 1.15), 2.2) if main_size else 1.25
    return (main_size, f"#{color.most_common(1)[0][0]:06x}",
            bold.most_common(1)[0][0], horizontal, center, is_table_row(d["lines"]), leading)


def extract_blocks(pdf_path: Path, keep_refs: bool):
    """ページごとにテキストブロックを取り出し、段落単位に整える。
    戻り値は (ページ番号, [Para, ...]) の列"""
    doc = pymupdf.open(pdf_path)
    pages = []
    for page in doc:
        h, w = page.rect.height, page.rect.width
        # テキストブロックを段落（選択肢・表題の行など）に分け、blocks 形式のタプル
        # (x0, y0, x1, y1, text, 番号, 0) にして読み順を決める。画像ブロックは除外
        # 分けなかったブロックの文字は従来どおり blocks 形式から取る（同じ TextPage ならブロック番号が一致する）
        tp = page.get_textpage()
        block_text = {b[5]: b[4] for b in page.get_text("blocks", textpage=tp)}
        segs, tuples = [], []
        for d in page.get_text("dict", textpage=tp)["blocks"]:
            if d["type"] != 0:
                continue
            # 空白だけの行（Word 由来の PDF に多い）で枠が上下に広がり、訳文の位置がずれるので除く
            lines = [l for l in d.get("lines", []) if line_text(l).strip()]
            if not lines:
                continue
            if len(lines) < len(d["lines"]):
                d = dict(d, lines=lines, bbox=(min(l["bbox"][0] for l in lines), min(l["bbox"][1] for l in lines),
                                               max(l["bbox"][2] for l in lines), max(l["bbox"][3] for l in lines)))
            for s in split_block(d, w):
                text = s["text"] + "\n" if "text" in s else block_text.get(d["number"], "")
                tuples.append((*s["bbox"], text, len(segs), 0))
                segs.append(s)
        blocks = []
        for b, col in reading_order(tuples, w):
            # ページ上下 8% にあるブロックはヘッダー・フッター候補
            edge = b[3] < h * 0.08 or b[1] > h * 0.92
            style = block_style(segs[b[5]], w)
            blocks.append((b[4], edge, b[1], col, (b[0], b[1], b[2], b[3]), style))
        pages.append(blocks)

    # 上下端で全ページ的に繰り返すヘッダー・フッター（ページ番号含む）を除去
    def norm(t):
        return re.sub(r"\d+", "#", t.strip())

    cnt = Counter(t for blks in pages for t in {norm(b[0]) for b in blks if b[1]})  # 1ページ1回まで数える
    th = max(2, len(pages) // 2)

    result = []
    for pno, blks in enumerate(pages, 1):
        items = []
        for t, edge, y0, col, rect, style in blks:
            if edge and (cnt[norm(t)] >= th or re.fullmatch(r"\s*\d{1,4}\s*", t)):
                continue
            text = clean(t)
            if text:
                items.append((text, y0, col, Para(text, rect, *style)))
        # 参考文献の開始位置を探す。そこから下で、同じ段かそれより後ろの段にあるブロックは文献とみなす。
        # （左段の途中から始まる場合: 両段とも下は文献。右段から始まる場合: 左段は本文のまま）
        ref = None
        if not keep_refs:
            ref = next(((y0, col) for text, y0, col, _ in items
                        if REF_HEAD.match(text) or REF_FIRST.match(text)), None)
        if ref:
            # 後ろの段では、文献番号で始まるブロック（"[10] L. Evans, ..."）が開始位置より上にあればそこから切る
            cut = {ref[1]: ref[0]}
            for text, y0, col, _ in items:
                if col > ref[1] and REF_ITEM.match(text):
                    cut[col] = min(cut.get(col, ref[0]), y0)
            items = [it for it in items if not (it[2] >= ref[1] and it[1] >= cut.get(it[2], ref[0]))]
        result.append((pno, [p for _, _, _, p in items]))
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


def should_translate(text: str, short_ok: bool = False) -> bool:
    """数式・数値表・短すぎる断片は訳さない。選択肢・箇条書き（"(A) clear"）と、
    short_ok（中央そろえの表題 "Reading" など）は1語でも訳す"""
    if re.fullmatch(r"\s*(\S+@\S+\.\S+|https?://\S+|www\.\S+)\s*", text):  # メールアドレス・URL
        return False
    letters = sum(c.isalpha() for c in text)
    if short_ok and letters >= 3 and letters / max(len(text), 1) >= 0.5:
        return True
    item = LIST_MARK.sub("", text, count=1) if LIST_MARK.match(text) else None
    if item is not None and item.strip():
        letters = sum(c.isalpha() for c in item)
        return letters >= 3 and letters / max(len(item.strip()), 1) >= 0.5
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


# ---------------------------------------------------------------- PDF出力（元のレイアウトのまま置き換え）
# Windows 標準の日本語フォント（上ほど優先）。本文は明朝、太字の見出しはゴシックにする
BODY_FONTS = ["BIZ-UDMinchoM.ttc", "yumin.ttf", "msmincho.ttc"]
BOLD_FONTS = ["BIZ-UDGothicB.ttc", "YuGothB.ttc", "meiryob.ttc", "BIZ-UDGothicR.ttc", "msgothic.ttc"]
# 英数字用の欧文フォント。BIZ UD明朝などは英数字が等幅で、論文中の英単語・数値が間延びするため
LATIN_FONTS = ["times.ttf"]
LATIN_BOLD_FONTS = ["arialbd.ttf", "timesbd.ttf"]
# 欧文フォントで組む部分（英数字・記号・ラテン拡張・ギリシャ文字の並び。間の半角スペースも含める）
LATIN_RUN = re.compile(r"[\x21-\x7eÀ-ɏͰ-Ͽ]+(?: +[\x21-\x7eÀ-ɏͰ-Ͽ]+)*")


def font_dirs():
    dirs = [Path(os.environ.get("WINDIR", r"C:\Windows")) / "Fonts"]
    if os.environ.get("LOCALAPPDATA"):
        dirs.append(Path(os.environ["LOCALAPPDATA"]) / "Microsoft" / "Windows" / "Fonts")
    return dirs


def find_font(names) -> Path | None:
    for d in font_dirs():
        for n in names:
            if (d / n).exists():
                return d / n
    return None


class PdfFonts:
    """insert_htmlbox に渡す CSS とフォント置き場（Archive）。
    フォントが見つからなければ PyMuPDF 内蔵の CJK フォント（Droid Sans Fallback）を使う"""

    def __init__(self, explicit: str | None):
        if explicit:
            p = Path(explicit).expanduser()
            if not p.exists():
                sys.exit(f"--pdf-font のフォントファイルがありません: {p}")
            body = bold = p
            latin = latin_bold = None  # 指定されたフォントで英数字も組む
        else:
            body, bold = find_font(BODY_FONTS), find_font(BOLD_FONTS)
            bold = bold or body
            latin, latin_bold = find_font(LATIN_FONTS), find_font(LATIN_BOLD_FONTS)
        self.archive = pymupdf.Archive()
        css = []
        added = set()
        self.has_latin, self.has_latin_bold = latin is not None, latin_bold is not None
        for fam, f in (("jbody", body), ("jbold", bold), ("lbody", latin), ("lbold", latin_bold)):
            if f:
                if f.parent not in added:
                    self.archive.add(str(f.parent))
                    added.add(f.parent)
                css.append(f"@font-face{{font-family:{fam};src:url({f.name})}}")
        self.has_body, self.has_bold = body is not None, bold is not None
        self.bold_is_body = bold == body  # 太字用の書体が無い → font-weight で太らせる
        self.css = "".join(css)
        self.name = body.name if body else "PyMuPDF 内蔵フォント"

    def html(self, p: Para, text: str, leading: float = 1.25) -> str:
        fam = "jbold" if p.bold and self.has_bold else "jbody" if self.has_body else None
        # 両端揃えは半角スペースだけを広げるので、和文では英単語の前後が大きく空く。左揃えにする
        style = [f"font-size:{p.size:.2f}pt", f"color:{p.color}", f"line-height:{leading:.2f}", "margin:0",
                 "text-align:" + ("center" if p.center else "left")]
        if fam:
            style.append(f"font-family:{fam}")
        if p.bold and (fam != "jbold" or self.bold_is_body):
            style.append("font-weight:bold")
        lfam = ("lbold" if self.has_latin_bold else None) if fam == "jbold" else (
            "lbody" if self.has_latin else None)
        if lfam:
            # 英数字の並びだけ欧文フォントにする（MuPDF は CSS の font-family を字ごとに切り替えないため span で分ける）
            body = LATIN_RUN.sub(lambda m: f"\0{m.group(0)}\1", text)
            body = html.escape(body).replace("\0", f"<span style='font-family:{lfam}'>").replace("\1", "</span>")
        else:
            body = html.escape(text)
        return f"<p style='{';'.join(style)}'>{body}</p>"


def page_obstacles(page) -> list:
    """訳文の枠を伸ばすときにぶつかってはいけないもの（元の文字ブロック・画像・線画）の矩形"""
    # ブロックではなく行単位で見る（1ブロックを選択肢・表題の行ごとに分けて訳すので、同じブロックの次の行も障害物）
    rects = [pymupdf.Rect(l["bbox"]) for b in page.get_text("dict")["blocks"] if b["type"] == 0
             for l in b["lines"]]
    for d in page.get_drawings():
        rects.append(pymupdf.Rect(d["rect"]))
    return rects


def text_right_edge(page) -> float:
    """本文の右端（横書きの行の右端の最大値）。無ければページ幅の 92%"""
    xs = [l["bbox"][2] for b in page.get_text("dict")["blocks"] if b["type"] == 0
          for l in b["lines"] if abs(l["dir"][0] - 1) < 1e-3 and line_text(l).strip()]
    return max(xs) if xs else page.rect.width * 0.92


def room_right(r, obstacles, limit: float) -> float:
    """r の右で、縦方向に重なる最初の障害物の左端（無ければ limit）"""
    right = limit
    for o in obstacles:
        if o.x0 >= r.x1 - 1 and min(o.y1, r.y1) - max(o.y0, r.y0) > 1:
            right = min(right, o.x0 - 6)
    return right


def room_below(r, obstacles, limit: float) -> float:
    """r の真下で、横方向に重なる最初の障害物の上端（無ければ limit）"""
    bottom = limit
    for o in obstacles:
        if o.y0 >= r.y1 - 1 and min(o.x1, r.x1) - max(o.x0, r.x0) > 1:
            bottom = min(bottom, o.y0 - 3)
    return bottom


def write_pdf(src_pdf: Path, out: Path, items, fonts: PdfFonts, dual: Path | None = None):
    """元PDFの訳した段落だけを消し、同じ枠に訳文を流し込む。図・数式・訳さなかった段落はそのまま残す。
    文字が枠に収まらなければ縮小して収める。dual を渡すと 原文｜訳文 の見開きPDFも作る"""
    doc = pymupdf.open(src_pdf)
    by_page = {}
    for pno, p, dst in items:
        if dst is not None and p.horizontal:  # 回転した文字（図の軸ラベルなど）は原文のまま
            by_page.setdefault(pno, []).append((p, dst))

    # 訳文を1段落1ページの作業用PDFに組む。page.insert_htmlbox を段落ごとに呼ぶと
    # 日本語フォント（数 MB）が段落の数だけ埋め込まれるので、1つの DocumentWriter にまとめて
    # フォントを共有させ、show_pdf_page で元のページへ貼る（同じ作業用PDFからの貼り付けはフォントを使い回す）
    buf = io.BytesIO()
    writer = pymupdf.DocumentWriter(buf)
    css = "body{margin:1px}" + fonts.css
    placed, shrunk = [], 0

    def fit(story, r):
        # 枠を広げて収まる倍率を探す（= 文字を縮める倍率の逆数）。長い単語も枠からはみ出させない
        story.reset()
        return story.fit_scale(pymupdf.Rect(0, 0, r.width, r.height), scale_min=1,
                               flags=pymupdf.mupdf.FZ_PLACE_STORY_FLAG_NO_OVERFLOW)

    for pno, paras in by_page.items():
        page = doc[pno - 1]
        obstacles = None
        for p, dst in paras:
            r = pymupdf.Rect(p.rect)
            if p.center:
                # 中央そろえの表題は、訳文が長くなっても左右の余白まで広げて1行に収める
                cx, pw = (r.x0 + r.x1) / 2, page.rect.width
                half = max(r.width / 2, min(cx - pw * 0.08, pw * 0.92 - cx))
                r = pymupdf.Rect(cx - half, r.y0, cx + half, r.y1)
            # まず元の行送りで組み、収まらなければ行間を詰めてから（下へ伸ばす・縮める）
            lead = p.leading
            # 1行の枠は和文1行（文字サイズ×行送り）より低いことがあり、それだけで縮小されて
            # 選択肢ごとに文字の大きさがばらつくので、少なくとも1行分の高さにする
            # （ただし下の行には重ねない）
            need = r.y0 + p.size * max(lead, 1.25) + 2
            if need > r.y1:
                if obstacles is None:
                    obstacles = page_obstacles(page)
                r.y1 = max(r.y1, min(need, room_below(r, obstacles, page.rect.height) + 3))
            story = pymupdf.Story(html=fonts.html(p, dst, lead), user_css=css, archive=fonts.archive)
            f = fit(story, r)
            if f.parameter > 1.02 and lead > 1.25:
                lead = 1.25
                story = pymupdf.Story(html=fonts.html(p, dst, lead), user_css=css, archive=fonts.archive)
                f = fit(story, r)
            if f.parameter > 1.02 and not page.rotation and not p.center and LIST_MARK.match(p.text):
                # 選択肢・箇条書きは、右の空き（本文の右端まで）へ枠を伸ばして1行に収める
                if obstacles is None:
                    obstacles = page_obstacles(page)
                right = room_right(r, obstacles, text_right_edge(page))
                if right > r.x1 + 1:
                    r2 = pymupdf.Rect(r.x0, r.y0, right, r.y1)
                    f2 = fit(story, r2)
                    if f2.parameter < f.parameter:
                        r, f = r2, f2
            if f.parameter > 1.02 and not page.rotation:
                # 収まらないときは、下にある空き（次の段落・図・罫線まで）へ枠を伸ばして縮小を抑える
                if obstacles is None:
                    obstacles = page_obstacles(page)
                bottom = room_below(r, obstacles, page.rect.height * 0.94)
                if bottom > r.y1 + 1:
                    r2 = pymupdf.Rect(r.x0, r.y0, r.x1, bottom)
                    f2 = fit(story, r2)
                    if f2.parameter < f.parameter:
                        # 使った高さまで枠を縮める（文字の倍率は変えない）
                        used = max(f2.filled[3] / f2.parameter, r.height)
                        r = pymupdf.Rect(r.x0, r.y0, r.x1, min(r.y0 + used + 1, bottom))
                        f = fit(story, r)
            story.reset()
            story.place(f.rect)
            dev = writer.begin_page(f.rect)
            story.draw(dev)
            writer.end_page()
            placed.append((pno, r))
            if f.parameter > 1 / 0.75:
                shrunk += 1
    writer.close()
    layer = pymupdf.open("pdf", buf.getvalue()) if placed else None

    for pno, paras in by_page.items():
        # 抽出した座標も注釈・show_pdf_page も回転前のページ座標なので、/Rotate 付きのページもそのまま扱える
        page = doc[pno - 1]
        for p, _ in paras:
            page.add_redact_annot(pymupdf.Rect(p.rect), fill=False)
        # 文字だけ消す。画像・線画（図・表の罫線・背景色）は残す
        page.apply_redactions(images=pymupdf.PDF_REDACT_IMAGE_NONE,
                              graphics=pymupdf.PDF_REDACT_LINE_ART_NONE)
    for k, (pno, r) in enumerate(placed):
        doc[pno - 1].show_pdf_page(r, layer, k)
    doc.subset_fonts()  # 日本語フォントは数 MB あるので使った字だけ埋め込む
    doc.save(out, garbage=3, deflate=True)
    if shrunk:
        print(f"  {shrunk} 段落は訳文が長く、文字を 75% 未満に縮めて収めました")
    if dual:
        tr = pymupdf.open(out)
        src = pymupdf.open(src_pdf)
        dd = pymupdf.open()
        for i in range(len(src)):
            r0, r1 = src[i].rect, tr[i].rect
            page = dd.new_page(width=r0.width + r1.width, height=max(r0.height, r1.height))
            page.show_pdf_page(pymupdf.Rect(0, 0, r0.width, r0.height), src, i)
            page.show_pdf_page(pymupdf.Rect(r0.width, 0, r0.width + r1.width, r1.height), tr, i)
        dd.save(dual, garbage=3, deflate=True)


# ---------------------------------------------------------------- llama-server の用意
def find_server(explicit: str | None) -> Path | None:
    """llama-server を探す: --server → 環境変数 LLAMA_SERVER → PATH → --setup で入れた版
    → LM Studio 同梱版（CUDA 優先・新しい順）"""
    exe = "llama-server.exe" if os.name == "nt" else "llama-server"
    for c in (explicit, os.environ.get("LLAMA_SERVER")):
        if c:
            p = Path(c)
            if p.is_dir():
                p = p / exe
            return p if p.exists() else None
    w = shutil.which("llama-server")
    if w:
        return Path(w)
    own = list((MODELS_DIR / "llama.cpp").glob(f"b*/{exe}"))
    if own:
        return max(own, key=lambda p: int(re.sub(r"\D", "", p.parent.name) or 0))
    backends = Path.home() / ".lmstudio" / "extensions" / "backends"
    if backends.is_dir():
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
FORMATS = ("html", "pdf", "dual")


def parse_formats(s: str) -> list[str]:
    """--format の値（"html,pdf" / "all" など）を解釈する"""
    fs = [f.strip().lower() for f in re.split(r"[,\s]+", s) if f.strip()]
    if fs == ["all"]:
        return list(FORMATS)
    bad = [f for f in fs if f not in FORMATS]
    if bad or not fs:
        sys.exit(f"--format は {' / '.join(FORMATS)} をカンマ区切りで指定してください（all で全部）: {s}")
    return fs


def parse_open(s: str | None) -> set[str]:
    """--open の値（"pdf" / "html,pdf" / "none" / "all"）を解釈する。古い config.ini の true/false も受け付ける"""
    v = (s or "").strip().lower()
    if v in ("", "none", "no", "off", "false", "0"):
        return set()
    if v in ("yes", "on", "true", "1"):
        return {"pdf"}
    fs = {f for f in re.split(r"[,\s]+", v) if f}
    if fs == {"all"}:
        return set(FORMATS)
    if not fs <= set(FORMATS):
        sys.exit(f"--open（config.ini の open）は {' / '.join(FORMATS)} をカンマ区切りで、"
                 f"または none を指定してください: {s}")
    return fs


def out_dir_for(pdf: Path, a) -> Path:
    """出力先フォルダ。--out-dir（config.ini の out_dir）が空なら PDF と同じフォルダ"""
    if not a.out_dir:
        return pdf.resolve().parent
    d = Path(os.path.expandvars(str(a.out_dir))).expanduser()
    d.mkdir(parents=True, exist_ok=True)
    return d


CACHE_DIR = MODELS_DIR / "cache"


def cache_path_for(pdf: Path, to: str) -> Path:
    """途中再開用の訳文キャッシュ。出力先を散らかさないよう %USERPROFILE%\\.cache\\plamo-pdf\\cache\\ に置く。
    PDF の中身のハッシュで区別するので、PDF を移動・改名しても続きから訳せる"""
    h = hashlib.sha1(pdf.read_bytes()).hexdigest()[:12]
    stem = re.sub(r'[\\/:*?"<>|]', "_", pdf.stem)[:60]
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    return CACHE_DIR / f"{stem}-{h}.{to}.json"


def translate_pdf(pdf: Path, a, model_tag: str, fonts: "PdfFonts | None") -> list[tuple[str, Path]]:
    """戻り値は書き出した (形式, パス) の列"""
    formats = parse_formats(a.format)
    base = out_dir_for(pdf, a) / f"{pdf.stem}.{a.to}"
    outs = {"html": base.with_name(base.name + ".html"),
            "pdf": base.with_name(base.name + ".pdf"),
            "dual": base.with_name(base.name + ".dual.pdf")}
    if pdf.resolve() in {p.resolve() for p in outs.values()}:
        sys.exit(f"出力先が入力PDFと同じになります: {pdf}")
    cache_path = cache_path_for(pdf, a.to)
    cache = {}
    # 以前の版は出力先に *.ja.cache.json を作っていた。あれば読み込んで引き継ぐ
    for c in (base.with_name(base.name + ".cache.json"), cache_path):
        if c.exists():
            try:
                cache.update(json.loads(c.read_text(encoding="utf-8")))
            except (OSError, ValueError):
                pass

    pages = extract_blocks(pdf, a.keep_refs)
    total = sum(len(p) for _, p in pages)
    print(f"{pdf.name}: {len(pages)}ページ / {total}段落")

    tr = Translator(a.url, a.to, a.src or None, a.form, not a.no_context, a.timeout)
    items, done, t0 = [], 0, time.time()  # items: (ページ番号, Para, 訳文 or None)
    title = f"{pdf.stem}（{model_tag}）"

    def save_html():
        write_html(outs["html"], title, [(pno, p.text, dst) for pno, p, dst in items])

    try:
        for pno, paras in pages:
            for p in paras:
                text = p.text
                done += 1
                if p.table or not should_translate(text, short_ok=p.center and p.horizontal):
                    items.append((pno, p, None))
                    continue
                key = f"{model_tag}|{a.to}|{a.form}|{text}"  # モデル・訳先言語・文体が変わったら訳し直す
                if key in cache:
                    dst = cache[key]
                    tr.prev = text
                else:
                    dst = tr(text)
                    cache[key] = dst
                    cache_path.write_text(json.dumps(cache, ensure_ascii=False), encoding="utf-8")
                items.append((pno, p, dst))
                el = time.time() - t0
                print(f"\r  {done}/{total} 段落  経過 {el:5.0f} s", end="", flush=True)
            if "html" in formats:
                save_html()  # ページごとに途中保存
    finally:
        # 中断したときも、そこまでの訳で出力する（PDF は訳していない段落が原文のまま残る）
        print()
        written = []
        if "html" in formats:
            save_html()
            written.append(("html", outs["html"]))
        if "pdf" in formats or "dual" in formats:
            # 見開きだけ欲しいときは、置き換えPDFを作業用の名前で作って後で消す
            rep = outs["pdf"] if "pdf" in formats else base.with_name(base.name + ".tmp.pdf")
            write_pdf(pdf, rep, items, fonts or PdfFonts(a.pdf_font),
                      outs["dual"] if "dual" in formats else None)
            written += [(f, outs[f]) for f in ("pdf", "dual") if f in formats]
            if rep != outs["pdf"]:
                rep.unlink()
        for _, o in written:
            print(f"出力: {o}")
    return written


def install_llama_cpp(dest_root: Path = None) -> Path:
    """llama.cpp の公式リリース（GitHub）から Windows 版を取ってきて展開する。
    NVIDIA GPU があれば CUDA 12.4 版（+ cudart）、無ければ Vulkan 版。"""
    if os.name != "nt":
        sys.exit("自動インストールは Windows のみ対応です。llama.cpp を入れて --server で指定してください。")
    dest_root = dest_root or MODELS_DIR / "llama.cpp"
    rels = http_json("https://api.github.com/repos/ggml-org/llama.cpp/releases?per_page=10", timeout=30)
    nvidia = shutil.which("nvidia-smi") is not None
    for rel in rels:
        if not re.fullmatch(r"b\d+", rel["tag_name"]):
            continue
        assets = {x["name"]: x["browser_download_url"] for x in rel["assets"]}
        tag = rel["tag_name"]
        if nvidia:
            want = [f"llama-{tag}-bin-win-cuda-12.4-x64.zip", "cudart-llama-bin-win-cuda-12.4-x64.zip"]
        else:
            want = [f"llama-{tag}-bin-win-vulkan-x64.zip"]
        if all(w in assets for w in want):
            break
    else:
        sys.exit("llama.cpp のリリースに必要なファイルが見つかりませんでした。手動で入れてください（README 参照）。")

    import zipfile
    dest = dest_root / tag
    print(f"llama.cpp {tag}（{'CUDA 12.4' if nvidia else 'Vulkan'} 版）をダウンロードします → {dest}")
    for name in want:
        z = dest_root / name
        download(assets[name], z)
        with zipfile.ZipFile(z) as zf:
            zf.extractall(dest)
        z.unlink()
    exe = next(dest.rglob("llama-server.exe"), None)
    if not exe:
        sys.exit(f"展開したフォルダに llama-server.exe がありません: {dest}")
    if exe.parent != dest:  # zip 内にフォルダがある場合に備えて cudart の DLL を同じ場所へ
        for dll in dest.glob("*.dll"):
            dll.replace(exe.parent / dll.name)
    return exe


def setup(a):
    """llama-server とモデルを用意し、1文だけ訳して動作を確かめる"""
    exe = find_server(a.server)
    if exe:
        print(f"llama-server: {exe}")
    else:
        exe = install_llama_cpp()
        print(f"llama-server: {exe}")
    a.server = str(exe)
    model = resolve_model(a.model or DEFAULT_MODEL)
    print(f"モデル: {model}")
    print("動作確認として1文だけ翻訳します…")
    server = Server(a)
    try:
        tr = Translator(a.url, a.to, a.src or None, a.form, False, a.timeout)
        src = "This tool translates research papers on your own PC."
        print(f"  {src}\n  → {tr(src)}")
    finally:
        server.close()
    print("セットアップ完了")


# ---------------------------------------------------------------- 設定ファイル（config.ini）
CONFIG = Path(__file__).resolve().parent / "config.ini"
CONFIG_SECTION = "plamo-pdf"
CONFIG_TEMPLATE = f"""\
; plamo-pdf の設定。コマンドラインで同じオプションを指定すると、そちらが優先される。
; 行頭の ; はコメント。値を空にすると既定値になる。キーはオプション名（--out-dir → out_dir）。
[{CONFIG_SECTION}]
; 出力先フォルダ。空なら PDF と同じフォルダ。set_outdir.bat でフォルダを選んで設定できる
;   例: out_dir = %USERPROFILE%\\Documents\\翻訳
out_dir =
; 出力形式（カンマ区切りで複数可。all で全部）
;   html = 対訳HTML / pdf = 元のレイアウトのまま訳文に置き換えたPDF / dual = 原文｜訳文 の見開きPDF
;   対訳HTMLが要らなければ format = pdf にする
format = html,pdf
; 終わったら開くファイル（カンマ区切りで複数可）。none なら何も開かない
;   pdf = 既定の PDF ビューアーで開く / html = ブラウザで開く / dual = 見開きPDFを開く
;   例: open = pdf,html（HTML もブラウザで開く）
open = pdf
; 日本語の文体: plain = である調 / polite = です・ます調
form = plain
; PDF に使う日本語フォントファイル。空なら BIZ UD明朝（見出しは BIZ UDゴシック）などを自動で探す
pdf_font =
"""


def read_config_text() -> str:
    raw = CONFIG.read_bytes()
    try:
        return raw.decode("utf-8-sig")
    except UnicodeDecodeError:
        return raw.decode("cp932")  # メモ帳で ANSI 保存された場合


def load_config(ap: argparse.ArgumentParser):
    """config.ini の値をオプションの既定値にする（コマンドラインの指定が優先）"""
    if not CONFIG.exists():
        return
    cp = configparser.ConfigParser(interpolation=None)
    try:
        cp.read_string(read_config_text())
    except configparser.Error as e:
        sys.exit(f"config.ini を読めません: {e}")
    if not cp.has_section(CONFIG_SECTION):
        return
    acts = {}
    for act in ap._actions:
        if act.option_strings and act.dest not in ("help", "setup", "choose_out_dir"):
            acts[act.dest] = act
            for o in act.option_strings:
                acts[o.lstrip("-").replace("-", "_")] = act
    defaults = {}
    for k, v in cp[CONFIG_SECTION].items():
        act = acts.get(k.replace("-", "_"))
        if act is None:
            print(f"config.ini: 知らない設定 {k} は無視します", file=sys.stderr)
            continue
        v = v.strip()
        if v == "":
            continue
        if isinstance(act, argparse._StoreTrueAction):
            v = v.lower() in ("1", "true", "yes", "on")
        elif act.type:
            try:
                v = act.type(v)
            except ValueError:
                sys.exit(f"config.ini: {k} の値が正しくありません: {v}")
        if act.choices and v not in act.choices:
            sys.exit(f"config.ini: {k} は {' / '.join(act.choices)} のどれかにしてください: {v}")
        defaults[act.dest] = v
    ap.set_defaults(**defaults)


def set_config(key: str, value: str):
    """config.ini の1項目を書き換える（コメントや他の項目はそのまま残す）"""
    text = read_config_text() if CONFIG.exists() else CONFIG_TEMPLATE
    pat = re.compile(rf"^[ \t]*{re.escape(key)}[ \t]*[=:].*$", re.M)
    line = f"{key} = {value}"
    if pat.search(text):
        text = pat.sub(lambda m: line, text, count=1)
    elif re.search(rf"^\[{re.escape(CONFIG_SECTION)}\]", text, re.M):
        text = re.sub(rf"^(\[{re.escape(CONFIG_SECTION)}\][^\n]*\n)", lambda m: m.group(1) + line + "\n",
                      text, count=1, flags=re.M)
    else:
        text = text.rstrip("\n") + f"\n[{CONFIG_SECTION}]\n{line}\n"
    CONFIG.write_text(text, encoding="utf-8")


def choose_out_dir(current: str | None):
    """フォルダ選択ダイアログで出力先を選び、config.ini に保存する"""
    print(f"現在の保存先: {current or 'PDF と同じフォルダ'}")
    start = Path(os.path.expandvars(current)).expanduser() if current else Path.home() / "Documents"
    d = None
    try:
        import tkinter as tk
        from tkinter import filedialog
        root = tk.Tk()
        root.withdraw()
        root.attributes("-topmost", True)  # ダイアログがコンソールの裏に隠れないように
        d = filedialog.askdirectory(title="翻訳結果の保存先フォルダを選んでください",
                                    initialdir=str(start if start.is_dir() else Path.home()), mustexist=False)
        root.destroy()
    except Exception:  # tkinter が無い・画面が無い環境では入力で受け付ける
        d = input("保存先フォルダのパス（空 Enter でキャンセル）: ").strip().strip('"')
    if not d:
        ans = input("保存先を「PDF と同じフォルダ」に戻しますか？ [y/N]: ").strip().lower()
        if ans in ("y", "yes"):
            set_config("out_dir", "")
            print(f"保存先: PDF と同じフォルダ（{CONFIG} に保存）")
        else:
            print("変更しませんでした")
        return
    d = str(Path(d))
    set_config("out_dir", d)
    print(f"保存先: {d}（{CONFIG} に保存）")


def open_file(p: Path):
    if hasattr(os, "startfile"):
        os.startfile(p)  # 既定のアプリで開く（PDF は PDF ビューアー、HTML はブラウザ）
    else:
        import webbrowser
        webbrowser.open(p.resolve().as_uri())


def main():
    for s in (sys.stdout, sys.stderr):  # cp932 のコンソールで表示できない文字があっても落ちないように
        s.reconfigure(errors="replace")
    ap = argparse.ArgumentParser(description="PLaMo 3 Translate で論文PDFを対訳HTML・日本語PDFにする")
    ap.add_argument("pdf", type=Path, nargs="*", help="翻訳するPDF（複数可）")
    ap.add_argument("--setup", action="store_true",
                    help="llama-server（無ければ自動で入れる）とモデルを用意して動作確認する")
    ap.add_argument("--choose-out-dir", action="store_true",
                    help="フォルダ選択画面で出力先を選び、config.ini に保存する")
    ap.add_argument("-o", "--out-dir", default=None, help="出力先フォルダ（既定は PDF と同じフォルダ）")
    ap.add_argument("--format", default="html,pdf",
                    help="出力形式 html / pdf / dual をカンマ区切りで（all で全部。既定 html,pdf）")
    ap.add_argument("--pdf-font", default=None, help="PDF に使う日本語フォントファイル（既定は自動）")
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
    ap.add_argument("--open", nargs="?", const="pdf", default="pdf", metavar="FORMATS",
                    help="終わったら開くファイル html / pdf / dual（カンマ区切り）または none（既定 pdf）")
    ap.add_argument("--no-open", dest="open", action="store_const", const="none", help="何も開かない")
    if not CONFIG.exists():
        # 設定ファイルのひな形を作っておく（出力形式・開くファイルなどを書き換えられるように）
        try:
            CONFIG.write_text(CONFIG_TEMPLATE, encoding="utf-8")
            print(f"設定ファイルを作りました: {CONFIG}")
        except OSError:
            pass
    load_config(ap)
    a = ap.parse_args()
    open_formats = parse_open(a.open)

    if a.choose_out_dir:
        choose_out_dir(a.out_dir)
        return
    if a.setup:
        setup(a)
        return
    if not a.pdf:
        ap.print_usage()
        sys.exit("翻訳するPDFを指定してください（Windows では PDF を plamo_pdf.bat にドラッグ＆ドロップ）。")
    for p in a.pdf:
        if not p.exists():
            sys.exit(f"ファイルがありません: {p}")
    formats = parse_formats(a.format)  # サーバー起動前に書式の誤りを知らせる
    fonts = PdfFonts(a.pdf_font) if {"pdf", "dual"} & set(formats) else None
    if fonts:
        print(f"PDF の日本語フォント: {fonts.name}")

    server = Server(a)
    try:
        for pdf in a.pdf:
            for fmt, out in translate_pdf(pdf, a, server.model, fonts):
                if fmt in open_formats:
                    open_file(out)
    except KeyboardInterrupt:
        print("\n中断しました（再実行すると続きから訳します）")
    except urllib.error.URLError as e:
        print(f"\nllama-server に接続できません（{a.url}）: {e}")
    finally:
        server.close()


if __name__ == "__main__":
    main()
