---
description: WAV音声＋Premiere Pro XML からカット点同期SRT字幕を自動生成する。日本語トーク動画のテロップ用。Whisper→LLM意味区切り改行→SRT の3ステップ構成（v5・スピード重視）。
---

# WAV + XML → SRT 字幕自動生成 (v5)

## 設計原則

```
[機械]   Whisper(large-v3, beam=1, VAD) → segments.json        [約7分]
[LLM]    意味の区切りで改行したテキストを .txt に出力         [約3分]
[機械]   --from-text で改行テキスト + 単語タイミング → SRT   [約0秒]
─────────────────────────────────────────────────────────
合計: 約10〜15分（実測: 28分音声で13分34秒）
```

## 重要ルール

- **意味境界優先**: 文字数（5〜25字目安）は縛りすぎない
- **自律動作**: ユーザー確認不要。生成したら即 SRT 化する
- **固有名詞**: `memory/telop_channel_patterns.md` の辞書を参照して修正

## 使い方

```
/user:srt /path/to/audio.wav /path/to/timeline.xml
```

XML は省略可能（ただし XML ありの方が時刻精度が高い）。

---

## 実行手順

### Step 0: メモリ読み込み（必須）

絶対パスで Read:

1. `~/ClaudeCode/projects/常時運用/premiere-skills/memory/feedback_srt_grouping_rules.md`
2. `~/ClaudeCode/projects/常時運用/premiere-skills/memory/telop_channel_patterns.md`

### Step 1: ファイル存在確認 + 出力ディレクトリ

```bash
VIDEO_BASENAME="$(basename '<wav>' .wav)"
OUTPUT_DIR="$HOME/ClaudeCode/projects/常時運用/premiere-skills/output/srt/$VIDEO_BASENAME"
mkdir -p "$OUTPUT_DIR"
```

### Step 2: キャッシュ確認

`$OUTPUT_DIR/$VIDEO_BASENAME.segments.json` があれば Step 4 へ直行。なければ Step 3。

### Step 3: Whisper 転写（高速モード）

```bash
python3 "$HOME/.claude/scripts/whisper_to_srt.py" "<wav>" --xml "<xml>" --output-dir "$OUTPUT_DIR"
```

**Bash タイムアウト: 600000ms（10分）必須**

### Step 4: 全文テキスト抽出

```bash
python3 -c "
import json, sys
with open(sys.argv[1]) as f:
    data = json.load(f)
print(''.join(s.get('text', '') for s in data))
" "$OUTPUT_DIR/$VIDEO_BASENAME.segments.json" > "$OUTPUT_DIR/$VIDEO_BASENAME.fulltext.txt"
```

### Step 5: 意味区切り改行テキストを生成（LLM が直接ファイル書き込み）

1. `fulltext.txt` を Read する
2. 全文を意味の区切りで改行する（各行が 1 テロップ）
3. 固有名詞を `memory/telop_channel_patterns.md` の辞書に従って修正
4. `$OUTPUT_DIR/$VIDEO_BASENAME.lines.txt` に Write

**改行のルール（必須）**:

- **意味の区切りで切る**（文字数より意味優先、5〜25字目安）
- **複合動詞句は分断しない**: 「〜ている」「〜ていく」「〜てくる」「〜てみる」「〜ておく」「〜てしまう」等は 1 行
- **副詞句は後続動詞と同じ行**: 「徹底的に」「本当に」「しっかりと」は次の行の先頭
- **話題転換で切る**: 説明→CTA、概念→具体例、肯定→疑問
- **「〜おり/ており」「〜ですけども」「〜なんですけど」で切る**
- **文頭禁止パターン**: 「ます」「まし」「ない」「とき」「こと」等で始めない（前の行に結合）
- **フィラー**: 「こう」「ちょっと」は文脈判断で残す（全削除禁止）
- **空行は段落区切り**（SRT には反映されない）
- **句読点（、。）は使わない**

**⚠️ lines.txt に書く固有名詞は CORRECTIONS 辞書登録済みのもののみ**
LLM が Whisper のカタカナを英語に修正する場合、スクリプトの CORRECTIONS 辞書にも同じマッピングが必要。未登録のまま lines.txt だけ変えると、アンカー検索の誤マッチ＋時刻ズレが発生する。修正内容が辞書にない場合は Whisper 表記のまま残す（または辞書に追加してから修正する）。

### Step 6: SRT 生成

```bash
python3 "$HOME/.claude/scripts/whisper_to_srt.py" \
  --from-text "$OUTPUT_DIR/$VIDEO_BASENAME.lines.txt" \
  --segments "$OUTPUT_DIR/$VIDEO_BASENAME.segments.json" \
  --xml "<xml>" \
  -o "$OUTPUT_DIR/$VIDEO_BASENAME.srt"
```

### Step 7: 完了報告

1. SRT の絶対パス
2. 統計表（エントリ数・平均文字数・25字超件数・4字未満件数）
3. 「Premiere Pro にインポートできます」

## ファイル管理

**削除する（タスク完了後）**:
- `$VIDEO_BASENAME.fulltext.txt`

**残す（次回高速起動用）**:
- `$VIDEO_BASENAME.segments.json`（Whisperキャッシュ）
- `$VIDEO_BASENAME.words.json`
- `$VIDEO_BASENAME.lines.txt`（改行テキスト本体）
- `$VIDEO_BASENAME.srt`（成果物）

## リファレンス

- **改行・品質ルール**: `references/srt_rules.md`
- **品質チェックコード**: `references/srt_quality_check.md`
- **過去の失敗例・正解例**: `memory/feedback_srt_grouping_rules.md`
- **チャンネル固有名詞辞書**: `memory/telop_channel_patterns.md`
