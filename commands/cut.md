---
description: Premiere Pro XMLの無音・雑音を自動カットする。XMLファイルを渡されたとき、無音カット・ジェットカット・カット編集を頼まれたとき、「/cut」で即実行。
---

# Premiere Pro XML 無音・雑音カット

## Gotchas（Claudeがハマりやすいポイント）

- **`--tracks` にBGMや環境音が常時鳴っているトラックを含めてしまう** → そのトラックはほぼ無音にならないため、積集合判定でカットが一切発生しなくなる。`--tracks` には人の声が入っているトラックだけを指定する
- **A1自体にBGM/環境音が常時混入している素材** → 既定の -48dB では無音が検出できず「検出無音: 0箇所」で実質何もカットされない。まずBGM混入を疑い、`--threshold -35`〜`-40` を試す
- **パディングを大きくしすぎる** → 前後に残す量の合計 ≥ min-silence だと全無音がパディングに食われて1フレームもカットされない（スクリプトが警告を出す）。合計を min-silence 未満にする。前後を別々にしたいときは `--padding-after`（直前の発話の後ろ＝語尾の余韻）/ `--padding-before`（次の発話の前＝出だしの間）を使う。どちらも未指定なら `--padding` の値が前後同量で入る
- **カット0件を「成功」と読み違える** → 2026-07-25以降、カット箇所0件は `exit 3`、選択したトラックの音声を1本でも取り出せなかった場合は `exit 4` で**XMLを書かずに停止**する。`[診断]` 行に実測 peak/RMS と**推奨閾値**が出るので、それを `--threshold` に渡して再実行する（旧挙動＝未カットXMLを出す、が必要な場合だけ `--allow-no-cut`）
- **全トラックに同じin/outを適用してしまう** → トラックごとにソースオフセットが異なる。`offset = in_frame - tl_start` を個別に保持すること
- **Bashタイムアウトを指定し忘れる** → デフォルト2分で切れる。必ず `600000`（10分）を指定

## 入力

`$ARGUMENTS` にXMLファイルのパスが入る。

- 引数がある場合: そのパスをそのまま使う
- 引数がない場合: 会話の中で添付・言及されたXMLファイルのパスを特定する。見つからなければユーザーに聞く

## 重要ルール（絶対に守ること）

1. **無音検出は既定でA1トラックの音声のみで行う**（A1=メインの声が入っているトラック）。複数話者をピンマイクで別トラックに個別収録している場合は、`--tracks A1,A2` のように対象トラックを指定できる（指定した全トラックが同時に無音の区間だけをカットする）。
2. **全トラック同期で編集点を入れる**: V1, V2, A1, A2 等、全てのトラックに同じタイムライン位置でカットを入れる。特定のトラックだけ動かしたり、勝手に同期しようとしない。
3. **各トラックのソースオフセットを個別に保持する**: トラックごとにin/outの開始位置（offset = in_frame - tl_start）が異なる場合がある。カット後のサブクリップのin/outは `タイムラインフレーム + そのトラック固有のoffset` で算出する。全トラックに同じin/outを適用してはいけない。

## 出力先

**Claude Code / Codex plugin として配布された場合**（プラグイン同梱の `scripts/silence_cut.py` が見つかる場合）は、**入力XMLと同じディレクトリの `output/cut/`** に出力する。プラグインの install 先はアップデートで差し替えられるため、そこに書き込んではいけない。

**premiere-skills リポジトリを直接使っている場合**（河村さんの開発環境）は、これまで通り**必ず premiere-skills リポジトリの `output/cut/` に出力すること**。worktree やカレントリポジトリのルートに出してはいけない（worktree から実行するとルートが worktree パスに解決され、過去のカット済みファイルと分離してしまう）。出力先は `$HOME/.claude/scripts/silence_cut.py`（premiere-skills への symlink）を `realpath` で解決して導出する。`/Users/...` のような絶対パスはハードコードしない（配布版が壊れるため）。

出力ファイル名: `<入力ファイル名>_カット済み.xml`

## 実行前チェック（依存が無ければ確認してから導入）

```bash
command -v ffmpeg >/dev/null 2>&1 && echo "ffmpeg: OK" || echo "ffmpeg: 未導入"
```

「未導入」の場合は無音カットを実行せず、`brew install ffmpeg` が必要である旨をユーザーに伝え、
今すぐ実行してよいか確認してから進める（無断で実行しない）。

## 実行

確認不要。即実行する。Bashのタイムアウトは必ず **600000** を指定すること。

```bash
if [ -f "${CLAUDE_PLUGIN_ROOT}/scripts/silence_cut.py" ]; then
  SCRIPT="${CLAUDE_PLUGIN_ROOT}/scripts/silence_cut.py"
  OUT_DIR="$(dirname "<XMLファイルの絶対パス>")/output/cut"
elif SCRIPT="$(find "$HOME/.codex/plugins/cache" -maxdepth 5 -type f -name silence_cut.py -path "*/premiere-skills/*" 2>/dev/null | head -1)" && [ -n "$SCRIPT" ]; then
  OUT_DIR="$(dirname "<XMLファイルの絶対パス>")/output/cut"
else
  SCRIPT="$HOME/.claude/scripts/silence_cut.py"
  OUT_DIR="$(cd "$(dirname "$(realpath "$SCRIPT")")/.." && pwd)/output/cut"
fi
python3 "$SCRIPT" \
  "<XMLファイルの絶対パス>" \
  --output-dir "$OUT_DIR"
```

（`${CLAUDE_PLUGIN_ROOT}` は Claude Code plugin のときだけ実パスに展開される。Codex plugin では
展開されないため、Codex のプラグインキャッシュ（`~/.codex/plugins/cache/`）内を探す2番目の分岐で
見つける。河村さんの開発環境ではどちらも該当せず、既存の `$HOME/.claude/scripts/silence_cut.py`
symlink 経由になる）

（既定はA1のみで無音判定。ピンマイクで2人を別トラックに個別収録している場合は `--tracks A1,A2` を追加する）

```bash
python3 "$SCRIPT" \
  "<XMLファイルの絶対パス>" \
  --output-dir "$OUT_DIR" \
  --tracks A1,A2
```

## 結果報告（検証より先に出す）

スクリプトが完了したら、**検証を待たずにまず完成報告を出す**。スクリプト出力の数値を使い、以下のフォーマットで報告する:

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

出力ファイルの絶対パス（`$OUT_DIR/<basename>_カット済み.xml` の実値）を必ず含め、Premiere Proで「ファイル > 読み込み」で読み込める旨を添える。

## 検証（必須・報告の後に実行）

完成報告を出した**後**に、出力した XML を `silence-cut-reviewer` agent（`model: sonnet`）に渡し、
過剰カット・取りこぼしを検証させる。渡すのは出力 XML の絶対パス（`$OUT_DIR/<basename>_カット済み.xml`）。

検証結果は、先に出した完成報告に続けて追記で伝える（報告をブロックしない）。
BLOCKER 相当（明らかな過剰カット・明らかな取りこぼし・結合ミス）が報告された場合は、その内容を明示する。
軽微な指摘のみの場合は注意点として簡潔に添える。
