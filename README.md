# plamo-pdf

論文PDFを、ローカルで動かした [PLaMo 3 Translate](https://huggingface.co/pfnet/plamo-3-translate-8b)（llama.cpp の llama-server）で翻訳し、対訳HTMLと、元のページのレイアウトのまま訳文に置き換えたPDFにするツール。翻訳はすべて手元のPCで行い、外部サービスには送らない。

## 特長

- llama-server の起動・停止とモデルのダウンロードを自動で行う。モデルは 8B / 0.8B から選べる
- 2段組の論文も 左段→右段 の読み順で取り出す
- 行末ハイフネーションの結合、合字（ﬁ ﬂ）や分離アクセント（V´aclav）の正規化、ヘッダー・フッター・ページ番号の除去
- 数式・数値だけの段落は原文のまま残す
- 参考文献以降は既定で翻訳しない（見出しのない `[1] A. Author, ...` 形式も検出。`--keep-refs` で翻訳）
- 直前段落を文脈として渡し、訳語のぶれを抑える
- 中断しても再実行で続きから（`*.ja.cache.json`。モデルを変えると訳し直す）
- 出力HTMLはPCでは左右対訳、スマホでは訳文→原文の縦並び。原文の表示切替あり
- **レイアウト維持PDF**：元PDFの本文だけを訳文に差し替える。図・表・数式・参考文献・ヘッダーは元のまま。原文｜訳文の見開きPDFも出せる
- **出力先フォルダを変更できる**（`set_outdir.bat` でフォルダを選ぶ / `--out-dir`）

## モデル

| `--model` | ファイル | 大きさ | VRAM 目安 | 7ページの論文の処理時間（RTX 3070） |
|---|---|---|---|---|
| `8b`（既定） | `plamo-3-translate-8b-Q4_K_M.gguf` | 4.9 GB | 約 6.5 GB | 約 110 秒 |
| `0.8b` | `plamo-3-translate-0.8b-Q4_K_M.gguf` | 0.6 GB | 約 1 GB | 約 20 秒 |

8B のほうが原文に忠実で自然な訳になる。VRAM が足りないときは `0.8b` を使うか、`--gpu-layers` を減らして一部を CPU で動かす（遅くなる）。手元の `.gguf` のパスを `--model` に直接渡してもよい。

モデルは次の順に探し、無ければ Hugging Face（pfnet）からダウンロードする。

1. `%USERPROFILE%\.cache\plamo-pdf\`（環境変数 `PLAMO_PDF_MODELS` で変更可。ダウンロード先もここ）
2. LM Studio のモデルフォルダ `%USERPROFILE%\.lmstudio\models\pfnet\...`

## 動作環境

- Windows 11（他OSでも動く想定）
- Python 3.10 以上、`pymupdf`
- llama.cpp の `llama-server`（CUDA版推奨）

## 使い方

### 1. セットアップ（初回のみ）

[Python 3.10 以上](https://www.python.org/downloads/)を入れてから、**`setup.bat` をダブルクリック**し、使うモデル（8b / 0.8b / 両方）を選ぶ。次のことを自動で行い、最後に1文だけ翻訳して動作を確かめる。

1. `.venv` を作って `pymupdf` を入れる
2. llama-server を探し、無ければ [llama.cpp の公式リリース](https://github.com/ggml-org/llama.cpp/releases)から Windows 版を取ってきて `%USERPROFILE%\.cache\plamo-pdf\llama.cpp\` に展開する（NVIDIA GPU があれば CUDA 12.4 版＋cudart、約 710 MB。無ければ Vulkan 版）
3. 選んだモデルをダウンロードする（既にあれば使う）
4. 選んだモデルを `model.txt` に保存する（`plamo_pdf.bat` が使う。両方選んだときは 8b）

`setup.bat 0.8b` のように引数でモデルを渡すと、選択画面を出さずに進む。

llama-server は次の順に探す。

1. `--server` / 環境変数 `LLAMA_SERVER`
2. `PATH` 上の `llama-server`
3. `setup.bat` が入れた llama.cpp（`%USERPROFILE%\.cache\plamo-pdf\llama.cpp\`）
4. LM Studio に同梱の llama.cpp（`%USERPROFILE%\.lmstudio\extensions\backends\`。CUDA版・新しい版を優先）

Windows 以外では `pip install -r requirements.txt` を実行し、llama.cpp を入れて `--server` で指定する。

### 2. 翻訳する

**いちばん簡単な方法**：PDF を `plamo_pdf.bat` にドラッグ＆ドロップする（複数可）。終わると対訳HTMLとレイアウト維持PDFが開く。出力先は `set_outdir.bat` で、出力形式は `config.ini` の `format` で変えられる。モデルを変えるときは `model.txt` を `8b` か `0.8b` に書き換えるか、`setup.bat` をもう一度実行する。

コマンドから使う場合：

```powershell
python plamo_pdf.py paper.pdf                       # 8B・である調
python plamo_pdf.py paper.pdf --model 0.8b          # 軽量モデル
python plamo_pdf.py a.pdf b.pdf --form polite --open
python plamo_pdf.py paper.pdf -o D:\翻訳 --format pdf,dual   # 保存先と出力形式を指定
```

`paper.ja.html` と `paper.ja.pdf` が出力される。llama-server はこのスクリプトが起動し、終わったら止める。指定した URL で既に llama-server が動いていればそれを使う（`--model` と違うモデルが動いているときはエラーにする）。

### 出力ファイル

| `--format` | ファイル | 内容 |
|---|---|---|
| `html` | `paper.ja.html` | 対訳HTML（段落ごとに原文と訳文を並べる） |
| `pdf` | `paper.ja.pdf` | 元のレイアウトのまま、本文を訳文に置き換えたPDF |
| `dual` | `paper.ja.dual.pdf` | 左に原文ページ、右に置き換えたページを並べた見開きPDF |

既定は `html,pdf`。カンマ区切りで組み合わせる（`all` で3つとも）。途中再開用の `paper.ja.cache.json` も同じフォルダにできる。形式を変えても翻訳はキャッシュから再利用するので、`--format` だけ変えて再実行すれば訳し直さずに作れる。

#### レイアウト維持PDFのしくみ

1. PDF の各テキストブロック（段落）の位置・文字サイズ・色・太字を読み取る
2. 訳した段落の文字だけを消す（画像・線画・背景色は消さない）
3. 同じ枠に訳文を流し込む。収まらないときは、下の空き（次の段落・図・罫線の手前まで）へ枠を伸ばし、それでも足りなければ文字を縮める。75% 未満に縮めた段落の数は実行時に表示する
4. 数式・数値だけの段落、参考文献、ヘッダー・フッター、回転した文字（図の軸ラベルなど）は原文のまま残す

日本語フォントは Windows 標準の **BIZ UD明朝**（本文）と **BIZ UDゴシック**（太字の見出し）を使う。無ければ 游明朝 / ＭＳ 明朝、それも無ければ PyMuPDF 内蔵のフォントを使う。英数字は Times New Roman（太字の見出しは Arial Bold）で組む（BIZ UD明朝の英数字は等幅で間延びするため）。`--pdf-font` で任意のフォントファイル（`.ttf` / `.ttc` / `.otf`）を指定できる。PDF には使った文字だけを埋め込む。

### 出力先フォルダ

既定は PDF と同じフォルダ。変えるには次のどれか。

- **`set_outdir.bat` をダブルクリック**し、フォルダを選ぶ（`config.ini` に保存され、以後 `plamo_pdf.bat` でも使われる）。キャンセルすると「PDF と同じフォルダ」に戻すか聞かれる
- `config.ini` の `out_dir =` を書き換える（`%USERPROFILE%` や `~` も使える）
- コマンドで `--out-dir`（`-o`）を指定する

### 設定ファイル（config.ini）

`plamo_pdf.py` と同じフォルダの `config.ini` に書いた値がオプションの既定値になる（コマンドラインの指定が優先）。`setup.bat` か `set_outdir.bat` を実行すると、説明付きのひな形ができる。キーはオプション名から `--` を除き、`-` を `_` にしたもの。

```ini
[plamo-pdf]
out_dir = %USERPROFILE%\Documents\翻訳
format = html,pdf,dual
form = polite
pdf_font =
```

### オプション

| オプション | 既定 | 内容 |
|---|---|---|
| `--setup` | なし | llama-server（無ければ自動で入れる）とモデルを用意し、1文訳して動作確認する |
| `--model` | `8b` | `8b` / `0.8b` / `.gguf` ファイルのパス |
| `--server` | 自動検出 | llama-server 実行ファイル（またはそのフォルダ）のパス |
| `--gpu-layers` | `99` | GPU に載せる層数（VRAM 不足なら減らす） |
| `--ctx` | `8192` | コンテキスト長 |
| `--url` | `http://127.0.0.1:8000` | llama-server のURL（自動起動時はこのポートを使う） |
| `--to` | `ja` | 翻訳先言語 |
| `--from` | `en` | 原文の言語（`""` で自動検出） |
| `--form` | `plain` | `plain`＝である調 / `polite`＝です・ます調（日本語訳のみ） |
| `--no-context` | なし | 直前段落を文脈として渡さない |
| `--keep-refs` | なし | 参考文献も翻訳する |
| `--timeout` | `600` | 1段落あたりのタイムアウト [s] |
| `--open` | なし | 終わったら出力ファイルを開く（PDF は既定の PDF ビューアー） |
| `-o`, `--out-dir` | PDF と同じフォルダ | 出力先フォルダ（無ければ作る） |
| `--format` | `html,pdf` | 出力形式 `html` / `pdf` / `dual`（カンマ区切り、`all` で全部） |
| `--pdf-font` | 自動 | レイアウト維持PDFに使う日本語フォントファイル |
| `--choose-out-dir` | なし | フォルダ選択画面で出力先を選び `config.ini` に保存する |

自動起動した llama-server のログは `%USERPROFILE%\.cache\plamo-pdf\llama-server.log` に残る。

## 状態

- Physical Review E の2段組論文（7ページ）で、8B・0.8B ともに実モデルで動作確認済み（RTX 3070 8 GB）
- `setup.bat` は、llama.cpp もモデルも無い状態から（llama.cpp b12407 CUDA 版・0.8B）セットアップ→翻訳まで確認済み
- レイアウト維持PDFは、合成した2段組PDF（図・数式・参考文献・回転ページ入り）とダミーの翻訳サーバーに加え、実際の論文（arXiv 1706.03762、1段組・表入り）を 0.8B・Windows のフォントで確認済み
- 表の見出し行・データ行（同じ高さに4つ以上のセルが並ぶブロック）は訳さず原文のまま残す
- 既知の制限：段・ページをまたぐ段落は2つに分かれて訳される
- 既知の制限（レイアウト維持PDF）：段落内の上付き・下付き・斜体・インライン数式の書式は保たれない（訳文はプレーンテキスト）。スキャンPDF（文字が画像）は置き換えられない

## モデルのライセンス

PLaMo 3 Translate は PLaMo コミュニティライセンス。個人・非商用は登録不要で無償。詳細は [モデルカード](https://huggingface.co/pfnet/plamo-3-translate-8b) を参照。

## ライセンス

このリポジトリのコードは [MIT License](LICENSE)。

PDF の読み取りに使う [PyMuPDF](https://github.com/pymupdf/PyMuPDF) は AGPL-3.0（または商用ライセンス）。このリポジトリには同梱せず、`setup.bat` で各自の環境に入れる。
