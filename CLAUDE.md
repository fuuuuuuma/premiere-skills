# premiere-skills プロジェクトローカル規約

`~/ClaudeCode/CLAUDE.md`（グローバルルール）に加えて、このプロジェクト固有の規約を記載する。

## プロジェクト目的

Premiere Pro ベースの動画編集ワークフローを Claude Code スキルで自動化する。具体的には:

- `/cut` — 無音自動カット
- `/srt` — テロップ用 SRT 字幕生成

## 鉄則

### 1. LLM 判断を機械ロジックに置き換えない（最重要）

日本語テキスト処理で `fugashi` / `MeCab` / `Ginza` 等の形態素解析をメインの判定に使わない。これらは候補抽出の補助にのみ利用する。**最終的な意味判断は LLM が文脈を読んで行う**。

**なぜ**: 2026-04-11 の No.801 SRT 生成で fugashi + Tier ルールを採用した結果、「思って / いるので」「検証して / いきたい」のような複合動詞句の分断が頻発。ルールベースは日本語の「て形+補助動詞」のような文脈依存パターンに追いつけない。

詳細は `memory/feedback_srt_grouping_rules.md` を参照。

### 2. スキルのステップを省略しない

`commands/srt.md` の全ステップは必須。特に Step 5（LLM 改行テキスト生成）は意味区切りルールと固有名詞辞書を適用しながら慎重に行う。時短のために省略してはいけない。

**CORRECTIONS 辞書との同期必須**: lines.txt で固有名詞を正規化する場合、`whisper_to_srt.py` の CORRECTIONS 辞書にも必ず同じマッピングを追加してから Step 6 を実行する。これを怠ると累積タイムズレが発生する。

### 3. 出力は必ず `output/` 配下に配置

`/srt` と `/cut` の成果物は必ず以下に配置する:

- `/srt` の出力: `output/srt/<video-basename>/`
- `/cut` の出力: `output/cut/`

入力ファイル（WAV, XML）は Downloads など元の場所に残してよい。

### 4. 学習結果を memory に残す

スキル実行中に新しい失敗パターンやユーザー修正例が発生したら、対応する `memory/*.md` に追記する。次回以降のセッションで参照される。

### 5. canonical ファイルはこのプロジェクトフォルダ内

`~/.claude/commands/*.md` と `~/.claude/scripts/*.py` は symlink。**編集は必ずこのプロジェクトフォルダ内で行う**。

## メモリファイル

| ファイル | 内容 | 更新タイミング |
|---|---|---|
| `memory/feedback_srt_grouping_rules.md` | SRT 切り分けの絶対ルール・失敗例・正解例 | ユーザー指摘時、新しい失敗パターン発見時 |
| `memory/telop_channel_patterns.md` | [CHANNEL]ch 固有の固有名詞辞書・スタイル | 新しい固有名詞・表記揺れ発見時 |

## 依存関係の前提

- Python 3.9+（`/usr/bin/python3` または `python3`）
- `faster-whisper`（`pip3 install --user faster-whisper`）
- `ffmpeg`（`brew install ffmpeg`）
- Claude Code CLI
