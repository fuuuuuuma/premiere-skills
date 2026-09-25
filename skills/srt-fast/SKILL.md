---
name: srt-fast
description: WAV/動画音声を単一パスGPU転写（mlx-whisper）した後、fulltextを意味区切りでN分割しLLM改行だけを並列化してSRT字幕を高速生成する /srt の高速版。日本語トーク動画用。「/srt-fast」で実行。
---

# WAV → SRT 高速生成 (/srt-fast・テキスト分割型 v7.1)

単一パスGPU転写（mlx-whisper・Apple Silicon対応）で高品質文字起こしを行い、取得した fulltext をポーズ位置で意味的にN分割し、サブエージェント等で改行工程のみを並列処理して高速にSRT字幕を生成するスキル。

## 設計原則

1. **転写は並列化しない**: 単一パスGPU転写（mlx-whisper）。gap補完内蔵で境界なし・高品質。
2. **改行工程のみ並列化**: テキストをポーズ位置でN分割し、サブエージェントで並列改行（25字超1%未満・平均14字前後）。
3. **高速な全体アライメントとQA修復**: difflibによる全体アライメントで時刻を割り当て、基準超過行（25字超や文頭NG）のみをピンポイントで修復。

## 使い方

```
/srt-fast <音声または動画ファイルのパス>
```

カット点同期XMLがある場合は、アライメント時に `--xml "<XMLのパス>"` を渡すことでカット後のタイムラインと完全同期可能。

## 実行手順

### Step 1: 環境とスクリプト解決

```bash
# プラグイン本体の場所を決める（この順で探し、最初に見つかったものを使う）
# Claude Code は ${CLAUDE_PLUGIN_ROOT} をプラグインの実パスに展開する。Codex / Antigravity は展開しない。
ROOT="${CLAUDE_PLUGIN_ROOT:-/nonexistent}"
if [ ! -f "$ROOT/scripts/whisper_to_srt.py" ]; then
  # 同じプラグインの複数版がキャッシュに残ることがあるので、更新の新しいものを選ぶ
  HIT="$(find "$HOME/.claude/plugins" "$HOME/.codex/plugins/cache" "$HOME/.gemini/config/plugins" \
         -maxdepth 8 -type f -name whisper_to_srt.py -path '*premiere-skills*' -print0 2>/dev/null \
         | xargs -0 ls -t 2>/dev/null | head -1)"
  [ -n "$HIT" ] && ROOT="$(cd "$(dirname "$HIT")/.." && pwd)"
fi
[ -f "$ROOT/scripts/whisper_to_srt.py" ] || { echo "premiere-skills のスクリプトが見つかりません。プラグインを入れ直してください"; exit 1; }

SCRIPTS_DIR="$ROOT/scripts"
PREPARE_SCRIPT="$SCRIPTS_DIR/chunk_tools/prepare_text_parts.py"
WHISPER_SCRIPT="$SCRIPTS_DIR/whisper_to_srt.py"
RULES_FILE="$ROOT/references/srt_runtime_rules.md"
echo "premiere-skills: $ROOT"

OUT_ROOT="$(dirname "<入力ファイルの絶対パス>")"
```

### Step 2: 依存チェック

- `ffmpeg`（導入済み確認: `command -v ffmpeg`）
- `mlx-whisper` または `faster-whisper`

### Step 3: 前処理（単一パスGPU転写＋テキスト分割）

```bash
python3 "$PREPARE_SCRIPT" "<入力ファイルの絶対パス>" --repo "$OUT_ROOT"
```

実行後、標準出力に `MANIFEST: <path>` として `<stem>.parts.json` が出力される。

### Step 4: 並列改行（サブエージェント / LLM）

`<stem>.parts.json` の内容を読み込み、各パート（`parts[i]`）に対して改行タスクを実行する。
パート数 `n > 1` の場合はサブエージェント（`invoke_subagent`）を同一ターンでN体並列起動する。
パート数が1（`n == 1`）の場合は、起動オーバーヘッドを避けるためメインエージェントが直接改行して書き込む。

#### サブエージェント用プロンプト要約:
- 1行＝1テロップ
- 目標文字数: 平均14字前後・25字超1%未満
- 削除・要約・言い換えは禁止（全文をカバー）
- SRT番号やタイムコードは含めない（純粋なテキスト行のみ）
- 「書き終えたら再読・自己検証はせず即終了する」「最終応答は LINES=<非空行数> を返す」

### Step 5: 組み立てと時刻アライメント（bash直叩き）

全パートの `lines_out` が揃ったら、結合してアライメントを実行する:

```bash
cd "<out_dir>" && python3 -c "
from pathlib import Path
import json
m = json.loads(Path('<stem>.parts.json').read_text())
lines = []
for p in m['parts']:
    lines += [l.strip() for l in Path(p['lines_out']).read_text().splitlines() if l.strip()]
Path(m['lines_out']).write_text('\n'.join(lines)+'\n')
print(len(lines), 'lines')
" && SRT_QA_JSON=1 python3 "$WHISPER_SCRIPT" \
  --from-text "<stem>.fast.lines.txt" --segments "<stem>.segments.json" -o "<stem>.fast.srt"
```

※カットXMLがある場合は `--xml "<XMLパス>"` を付与。

### Step 6: QA自動修復

出力の `QA_JSON` で `over25`（25字超）や `head_ng`（文頭助詞等）が検出された場合、`<stem>.fast.lines.txt` の該当行をルールに従って分割・調整し、`whisper_to_srt.py --from-text ...` を再実行（約0.1〜1秒で再アライメント完了）。

### Step 7: 掃除と完了報告

不要になった一時パートファイルを削除:
```bash
rm -f "<out_dir>/<stem>".part*.txt "<out_dir>/<stem>".part*.lines.txt
```

報告フォーマット:
- 出力SRTの絶対パス（`<stem>.fast.srt`）
- 統計情報（行数、平均文字数、25字超件数、修復回数など）
- Premiere Pro に「ファイル > 読み込み」でインポート可能な旨
