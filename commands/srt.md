---
description: WAV音声＋Premiere Pro XML からカット点同期SRT字幕を自動生成する。日本語トーク動画のテロップ用。並列Whisper→LLM意味区切り改行→difflib全体アライメントSRT の3ステップ構成（v6・速度と精度の両立）。
---

# WAV + XML → SRT 字幕自動生成 (v6)

## 設計原則

```
[機械]   transcribe_parallel.py: 並列Whisper → segments.json + fulltext.txt  [GPU: 約1〜2分 / CPU: 約3〜4分]
[LLM]    意味の区切りで改行したテキストを .txt に出力（ルール正典1ファイル参照）        [約3分]
[機械]   --from-text: difflib全体アライメントで時刻割当 → SRT + QAレポート               [約10秒]
─────────────────────────────────────────────────
合計: 約4〜5分（28分音声・GPU転写想定。v5 実測13分34秒 → 約1/3）
```

- **時刻割当は difflib 全体アライメント**（v6）。lines.txt 側の固有名詞修正が CORRECTIONS 辞書に
  未登録でも時刻はズレない（v5 の「辞書同期必須」制約は廃止。辞書追加は転写品質向上のための推奨事項）
- **転写エンジンは mlx-whisper (Apple GPU・large-v3-turbo) が既定**（2026-07-03 v6.1）。
  実測(M4 Max・180s): CPU large-v3 54.5s → GPU+gap補完 17.2s(3.2倍)。mlx は VAD 無しで
  無音明けの短い発話を落とすため、未カバー区間だけを CPU large-v3+VAD で補完転写する
  （gap rescue・whisper_to_srt.py 内蔵）。カバレッジは従来比で同等以上を実測確認済み。
  `SRT_WHISPER_ENGINE=cpu` で従来の faster-whisper (CPU) を強制、
  `SRT_WHISPER_MODEL` で mlx モデル差し替え。mlx-whisper 未導入時は自動フォールバック
- **転写は3プロセス並列**（LLM/エージェント不使用の機械工程。トークン消費ゼロ）。
  チャンク境界の欠落は overlap 転写からの自動復元で対策済み。
  ※GPU転写時は並列の伸びは小さい（GPUは単一資源のため。壊れはしない）
- **品質チェックはスクリプト内蔵**（Step 6 の出力に QA レポートが含まれる。LLM が SRT を読み直さない）

## 重要ルール

- **意味境界優先**: 文字数（5〜25字目安）は縛りすぎない
- **自律動作**: ユーザー確認不要。生成したら即 SRT 化する
- **改行・固有名詞・削除保持の全ルールは `references/srt_runtime_rules.md` が正典**

## 使い方

```
/user:srt /path/to/audio.wav /path/to/timeline.xml
```

XML は省略可能（ただし XML ありの方が時刻精度が高い）。

---

## 実行手順

### Step 0: ルール読み込み（必須・1ファイルだけ）

絶対パスで Read: `<このリポジトリのルート>/references/srt_runtime_rules.md`

（過去の memory 2ファイルの読込は不要になった。全ディレクティブは正典に蒸留済み。
新しい失敗パターンに出会ったら memory に経緯を追記し、正典にルールを反映する）

### Step 1: ファイル存在確認 + 出力ディレクトリ

```bash
VIDEO_BASENAME="$(basename '<wav>' .wav)"
SCRIPT="$HOME/.claude/scripts/whisper_to_srt.py"
REPO="$(cd "$(dirname "$(realpath "$SCRIPT")")/.." && pwd)"
OUTPUT_DIR="$REPO/output/srt/$VIDEO_BASENAME"
mkdir -p "$OUTPUT_DIR"
```

### Step 2: キャッシュ確認

`$OUTPUT_DIR/$VIDEO_BASENAME.segments.json` があれば Step 4 へ直行。なければ Step 3。

### Step 3: 並列 Whisper 転写（機械工程・トークン消費ゼロ）

```bash
python3 "$REPO/scripts/transcribe_parallel.py" "<wav>" --jobs 3
```

**Bash タイムアウト: 600000ms（10分）必須**

- segments.json / fulltext.txt / 16k mono wav キャッシュが `$OUTPUT_DIR` に生成される
- 短い音声では並列数が自動で絞られる。`⚠ 未解消の空白区間` が出たらその時刻帯を Step 7 で報告する

### Step 4: 全文テキスト確認

Step 3 が `$VIDEO_BASENAME.fulltext.txt` を出力済み。キャッシュ直行（Step 2→4）で
fulltext.txt が無い場合のみ生成する:

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
2. **`references/srt_runtime_rules.md` の全ルール**（絶対禁止8項・積極分割・削除/保持・
   固有名詞表記・半角スペース3類型・句読点禁止）に従い、全文を意味の区切りで改行する（各行が 1 テロップ）
3. `$OUTPUT_DIR/$VIDEO_BASENAME.lines.txt` に Write

**v6 の固有名詞ルール**: 残存する誤認識は文脈で正規表記に修正してよい（全体アライメントが
吸収するので時刻はズレない）。**チャンネル内で再登場する固有名詞**を新たに見つけたら、
`whisper_to_srt.py` の CORRECTIONS 辞書と `memory/telop_channel_patterns.md` に追記する
（次回以降の転写品質向上のため。今回の SRT 生成には必須ではない）。

### Step 6: SRT 生成（QA レポート内蔵）

```bash
python3 "$SCRIPT" \
  --from-text "$OUTPUT_DIR/$VIDEO_BASENAME.lines.txt" \
  --segments "$OUTPUT_DIR/$VIDEO_BASENAME.segments.json" \
  --xml "<xml>" \
  -o "$OUTPUT_DIR/$VIDEO_BASENAME.srt"
```

標準出力の末尾に「SRT 品質チェック（QA）」レポートが出る。
**25字超・文頭NG候補が指摘されたら、該当行だけ lines.txt を修正して Step 6 を再実行**
（QA が「✅ 要修正なし」になるか、意味的にこれ以上割れないと判断するまで）。

**exit 条件（無限ループ防止）**: QA 再実行は最大2回まで。2回目の再実行後も issue が0件に
ならない場合は、それ以上のリトライをせず理由を報告してそのまま Step 7 の完了報告に進む。

### Step 7: 完了報告

完了報告の前に、中間ファイルを削除する（絶対パスの rm。LLM 任せの判断にしない）:

```bash
rm -f "$OUTPUT_DIR/$VIDEO_BASENAME.fulltext.txt" "$OUTPUT_DIR"/chunk*.fulltext.txt
```

1. SRT の絶対パス
2. **Step 6 標準出力の QA レポートをそのまま転記**（SRT を Read し直して再集計しない）
3. 「Premiere Pro にインポートできます」

## ファイル管理

**削除する（タスク完了後）**:
- `$VIDEO_BASENAME.fulltext.txt`

**残す（次回高速起動用）**:
- `$VIDEO_BASENAME.segments.json`（Whisperキャッシュ）
- `$VIDEO_BASENAME.wav`（16k mono 変換キャッシュ）
- `$VIDEO_BASENAME.lines.txt`（改行テキスト本体）
- `$VIDEO_BASENAME.srt`（成果物）

（chunk 中間ファイルは transcribe_parallel.py が自動削除する）

## リファレンス

- **実行時ルール正典（Step 5 で必読）**: `references/srt_runtime_rules.md`
- ルールの原典・過去の失敗例と経緯: `memory/feedback_srt_grouping_rules.md`
- チャンネル固有名詞辞書（完全版）: `memory/telop_channel_patterns.md`
- 品質チェック: `--qa <srt>` フラグでいつでも単体実行可（旧手順は references/archive/srt_quality_check.md）
