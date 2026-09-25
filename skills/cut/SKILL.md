---
name: cut
description: Premiere Pro XML の無音・雑音を自動カットする。XMLファイルを渡されたとき、無音カット・ジェットカット・カット編集を頼まれたとき、「/cut」で即実行。
---

# Premiere Pro XML 無音・雑音カット (/cut)

Premiere Pro から書き出された Final Cut Pro XML（`.xml`）を解析し、無音区間を自動カットした編集済み XML を出力するスキル。

## Gotchas（エージェントがハマりやすいポイント）

- **`--tracks` にBGMや環境音が常時鳴っているトラックを含めてしまう** → そのトラックはほぼ無音にならないため、積集合判定でカットが一切発生しなくなる。`--tracks` には人の声が入っているトラックだけを指定する
- **A1自体にBGM/環境音が常時混入している素材** → 既定の -48dB では無音が検出できず「検出無音: 0箇所」で実質何もカットされない。まずBGM混入を疑い、`--threshold -35`〜`-40` を試す
- **パディングを大きくしすぎる** → 前後に残す量の合計 ≥ min-silence だと全無音がパディングに食われて1フレームもカットされない（スクリプトが警告を出す）。合計を min-silence 未満にする。前後を別々にしたいときは `--padding-after`（直前の発話の後ろ＝語尾の余韻）/ `--padding-before`（次の発話の前＝出だしの間）を使う。どちらも未指定なら `--padding` の値が前後同量で入る
- **カット0件を「成功」と読み違える** → カット箇所0件は `exit 3`、選択したトラックの音声を1本でも取り出せなかった場合は `exit 4` で**XMLを書かずに停止**する。`[診断]` 行に実測 peak/RMS と**推奨閾値**が出るので、それを `--threshold` に渡して再実行する（旧挙動＝未カットXMLを出す、が必要な場合だけ `--allow-no-cut`）
- **全トラックに同じin/outを適用してしまう** → トラックごとにソースオフセットが異なる。`offset = in_frame - tl_start` を個別に保持すること
- **タイムアウトを指定し忘れる** → 処理時間が長くなる場合があるため、タイムアウトは長め（10分・600000ms）を指定

## 入力

コマンド引数または会話内で指定された XML ファイルの絶対パス。
見つからなければユーザーに確認する。

## 重要ルール（絶対に守ること）

1. **無音検出は既定でA1トラックの音声のみで行う**（A1=メインの声が入っているトラック）。複数話者をピンマイクで別トラックに個別収録している場合は、`--tracks A1,A2` のように対象トラックを指定できる（指定した全トラックが同時に無音の区間だけをカットする）。
2. **全トラック同期で編集点を入れる**: V1, V2, A1, A2 等、全てのトラックに同じタイムライン位置でカットを入れる。特定のトラックだけ動かしたり、勝手に同期しようとしない。
3. **各トラックのソースオフセットを個別に保持する**: トラックごとにin/outの開始位置（offset = in_frame - tl_start）が異なる場合がある。カット後のサブクリップのin/outは `タイムラインフレーム + そのトラック固有のoffset` で算出する。全トラックに同じin/outを適用してはいけない。

## 実行前チェック

```bash
command -v ffmpeg >/dev/null 2>&1 && echo "ffmpeg: OK" || echo "ffmpeg: 未導入"
```

## スクリプト解決と実行

```bash
# プラグイン本体の場所を決める（この順で探し、最初に見つかったものを使う）
# Claude Code は ${CLAUDE_PLUGIN_ROOT} をプラグインの実パスに展開する。Codex / Antigravity は展開しない。
ROOT="${CLAUDE_PLUGIN_ROOT:-/nonexistent}"
if [ ! -f "$ROOT/scripts/silence_cut.py" ]; then
  # 同じプラグインの複数版がキャッシュに残ることがあるので、更新の新しいものを選ぶ
  HIT="$(find "$HOME/.claude/plugins" "$HOME/.codex/plugins/cache" "$HOME/.gemini/config/plugins" \
         -maxdepth 8 -type f -name silence_cut.py -path '*premiere-skills*' -print0 2>/dev/null \
         | xargs -0 ls -t 2>/dev/null | head -1)"
  [ -n "$HIT" ] && ROOT="$(cd "$(dirname "$HIT")/.." && pwd)"
fi

SCRIPT="$ROOT/scripts/silence_cut.py"
[ -f "$SCRIPT" ] || SCRIPT="$HOME/.claude/scripts/silence_cut.py"   # 手で配置した既存環境
[ -f "$SCRIPT" ] || { echo "silence_cut.py が見つかりません。プラグインを入れ直してください"; exit 1; }
echo "silence_cut: $SCRIPT"

XML_PATH="<XMLファイルの絶対パス>"
OUT_DIR="$(dirname "$XML_PATH")/output/cut"
mkdir -p "$OUT_DIR"

python3 "$SCRIPT" "$XML_PATH" --output-dir "$OUT_DIR"
```

ピンマイク2人収録の場合:
```bash
python3 "$SCRIPT" "$XML_PATH" --output-dir "$OUT_DIR" --tracks A1,A2
```

## 結果報告

スクリプト完了後、出力の数値を使い以下のフォーマットで報告する:

```
■ 無音カット結果
─────────────────────────
元の長さ:     XX分XX秒
カット後:     XX分XX秒
カットした無音: XX分XX秒
削減率:       XX.X%
出力ファイル:  <絶対パス>
─────────────────────────
```

出力ファイルの絶対パス（`<basename>_カット済み.xml`）を明記し、Premiere Pro で「ファイル > 読み込み」から読み込める旨を伝える。
