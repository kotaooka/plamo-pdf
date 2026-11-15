# plamo-pdf

論文PDFを、ローカルで動かした [PLaMo 3 Translate](https://huggingface.co/pfnet/plamo-3-translate-8b)（llama.cpp の llama-server）で翻訳し、対訳HTMLにするツール。翻訳はすべて手元のPCで行い、外部サービスには送らない。

## 特長

- 行末ハイフネーションの結合、ヘッダー・フッター・ページ番号の除去
- 数式・数値だけの段落は原文のまま残す
- 参考文献以降は既定で翻訳しない（`--keep-refs` で翻訳）
- 直前段落を文脈として渡し、訳語のぶれを抑える
- 中断しても再実行で続きから（`*.ja.cache.json`）
- 出力HTMLはPCでは左右対訳、スマホでは訳文→原文の縦並び。原文の表示切替あり

## 動作環境

- Windows 11 / PowerShell（他OSでも動く想定）
- Python 3.10 以上、`pymupdf`
- llama.cpp（CUDA版推奨）
- 8B Q4_K_M は約 5.3 GB。VRAM 8 GB の GPU で全層オフロードできる

## 使い方

### 1. llama.cpp を入れる

[Releases](https://github.com/ggml-org/llama.cpp/releases) から `llama-*-bin-win-cuda-*-x64.zip` と対応する `cudart-*.zip` を取得し、同じフォルダに展開する。

### 2. llama-server を起動する

初回はモデルを自動ダウンロードする。

```powershell
.\llama-server.exe -hf pfnet/plamo-3-translate-8b-GGUF:Q4_K_M -ngl 99 -c 8192 --port 8000 --jinja --no-prefill-assistant
```

`--jinja` は必須（翻訳先言語などを `chat_template_kwargs` で渡すため）。

### 3. 翻訳する

```powershell
pip install pymupdf
python plamo_pdf.py paper.pdf                  # である調
python plamo_pdf.py paper.pdf --form polite    # です・ます調
```

`paper.ja.html` が出力される。

### オプション

| オプション | 既定 | 内容 |
|---|---|---|
| `--url` | `http://127.0.0.1:8000` | llama-server のURL |
| `--to` | `ja` | 翻訳先言語 |
| `--from` | `en` | 原文の言語（`""` で自動検出） |
| `--form` | `plain` | `plain`＝である調 / `polite`＝です・ます調（日本語訳のみ） |
| `--no-context` | なし | 直前段落を文脈として渡さない |
| `--keep-refs` | なし | 参考文献も翻訳する |
| `--timeout` | `600` | 1段落あたりのタイムアウト [s] |

## 状態

- 模擬サーバーでの動作確認のみ。実モデルでの検証はこれから。

## モデルのライセンス

PLaMo 3 Translate は PLaMo コミュニティライセンス。個人・非商用は登録不要で無償。詳細は [モデルカード](https://huggingface.co/pfnet/plamo-3-translate-8b) を参照。
