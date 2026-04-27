# premiere-skills

[CHANNEL]の動画編集ワークフローを支援する Claude Code スキル集。Premiere Pro からの XML/WAV 書き出しを受け取り、無音カットやテロップ用 SRT 字幕を自動生成する。

---

## 🚀 受け取った人向け：Claude Code でのインストール

このリポジトリ URL ごと Claude Code に貼り付けて、以下のメッセージを送るだけで導入できます。

> **Claude へ送るメッセージ例**:
>
> ```
> https://github.com/fuuuuuuma/premiere-skills をインストールして。
> README の「受け取った人向け：Claude Code でのインストール」に従って、
> ~/ClaudeCode/projects/premiere-skills/ に clone し、
> ~/.claude/commands/ と ~/.claude/scripts/ に symlink を作成して。
> ```

Claude Code に手動で実行させたい場合の正確な手順は以下：

```bash
# 1. clone（任意の場所でOK。後でパスを書き換えるならどこでも良い）
mkdir -p ~/ClaudeCode/projects
git clone https://github.com/fuuuuuuma/premiere-skills.git ~/ClaudeCode/projects/premiere-skills

# 2. ~/.claude/ に symlink を張る（Claude Code がスキルを発見するため）
mkdir -p ~/.claude/commands ~/.claude/scripts
ln -sf ~/ClaudeCode/projects/premiere-skills/commands/cut.md ~/.claude/commands/cut.md
ln -sf ~/ClaudeCode/projects/premiere-skills/commands/srt.md ~/.claude/commands/srt.md
ln -sf ~/ClaudeCode/projects/premiere-skills/scripts/silence_cut.py ~/.claude/scripts/silence_cut.py
ln -sf ~/ClaudeCode/projects/premiere-skills/scripts/whisper_to_srt.py ~/.claude/scripts/whisper_to_srt.py

# 3. 依存インストール
pip3 install --user faster-whisper
brew install ffmpeg
```

導入後、Claude Code で `/cut` または `/srt` が使えるようになる。

### ⚠️ 自分用にカスタマイズしてください

`commands/cut.md` `commands/srt.md` 内の以下のパスは元オーナー（kawamurafuushin）の絶対パスにハードコードされています。**自分の環境に合わせて書き換えてください**：

- `$HOME/ClaudeCode/projects/常時運用/premiere-skills/` → 自分の clone 先（例: `$HOME/ClaudeCode/projects/premiere-skills/`）
- 出力先ディレクトリ（`output/cut`, `output/srt`）も同様
- `memory/telop_channel_patterns.md` の固有名詞辞書は [CHANNEL]ch 用なので、自分のチャンネル用に書き換える

書き換え対象ファイル：
- `commands/cut.md`
- `commands/srt.md`
- `CLAUDE.md`
- `scripts/whisper_to_srt.py` の `CORRECTIONS` 辞書（必要に応じて）

---


## 提供スキル

| スキル | 用途 | 入力 | 出力 |
|---|---|---|---|
| `/cut` | Premiere Pro XML の無音自動カット（ジェットカット） | XML | `output/cut/<basename>_カット済み.xml` |
| `/srt` | WAV + XML から日本語テロップ用 SRT を自動生成（v3、LLM主導QAループ） | WAV + XML | `output/srt/<basename>/<basename>.srt` ほか中間ファイル |

## ディレクトリ構造

```
premiere-skills/
├── README.md                    ← このファイル
├── CLAUDE.md                    ← プロジェクトローカル規約
├── commands/                    ← canonical スキル定義
│   ├── cut.md                   ← /cut スキル定義
│   └── srt.md                   ← /srt スキル定義（v3）
├── scripts/                     ← canonical Python スクリプト
│   ├── silence_cut.py           ← /cut の実装
│   └── whisper_to_srt.py        ← /srt の実装
├── memory/                      ← プロジェクト学習データ
│   ├── feedback_srt_grouping_rules.md  ← テロップ切り分けの絶対ルール
│   └── telop_channel_patterns.md       ← [CHANNEL]ch 固有の固有名詞辞書
└── output/                      ← 出力ファイル保存場所
    ├── srt/                     ← /srt の成果物 + 中間ファイル
    │   └── <basename>/
    │       ├── <basename>.srt           ← 最終出力（Premiere にインポート）
    │       ├── <basename>.segments.json ← Whisper キャッシュ（再実行時の高速モード用）
    │       ├── <basename>.words.json    ← 単語レベル timestamp + cut_before フラグ
    │       ├── <basename>.candidates.json  ← Step 3c 候補分割（過剰に細かい）
    │       └── <basename>.grouped.json     ← Step 3e LLM 修正結果
    └── cut/
        └── <basename>_カット済み.xml     ← /cut の成果物（Premiere にインポート）
```

## ファイル管理の方針

### canonical location
全てのスキル定義・スクリプト・メモリの **canonical（正本）** はこのプロジェクトフォルダ内にある。編集はここで行う。

### Claude Code 側 (`~/.claude/`)
スキル発見のため、以下のシンボリックリンクを `~/.claude/` 内に配置している:

```
~/.claude/commands/srt.md            → premiere-skills/commands/srt.md
~/.claude/commands/cut.md            → premiere-skills/commands/cut.md
~/.claude/scripts/whisper_to_srt.py  → premiere-skills/scripts/whisper_to_srt.py
~/.claude/scripts/silence_cut.py     → premiere-skills/scripts/silence_cut.py
```

Claude Code のスキル発見は `~/.claude/commands/` を見るので、symlink で十分機能する。**編集は canonical 側（ここ）で行う**。

### 自動メモリ連携
`~/.claude/projects/-Users-kawamurafuushin-ClaudeCode-projects-premiere-skills/memory/` 配下に canonical memory への symlink を配置しており、このディレクトリで作業する際 Claude Code が自動的に `memory/feedback_srt_grouping_rules.md` と `memory/telop_channel_patterns.md` を参照する。

## 使い方

### `/cut`: 無音自動カット

Premiere Pro で対象シーケンスを開き、「ファイル → 書き出し → Final Cut Pro XML」で XML を書き出してから:

```
/cut /path/to/your_file.xml
```

出力: `~/ClaudeCode/projects/premiere-skills/output/cut/<your_file>_カット済み.xml`

### `/srt`: テロップ用 SRT 生成（v3）

Premiere のカット後タイムラインから WAV（16kHz/モノラル/16bit推奨）と XML を書き出してから:

```
@/path/to/audio.wav @/path/to/timeline.xml /srt
```

パイプライン:
1. **Whisper 転写**（機械）— `faster-whisper large-v3` で word-level timestamps 付き転写
2. **候補抽出**（機械）— Whisper segment + XML カット点 + 0.3秒超の gap で切れ目候補を抽出（800-1000個）
3. **違和感検出**（LLM）— Claude が全候補を 14 項目チェックリストで評価
4. **修正**（LLM）— merge / split / text fix を適用して grouped.json を生成
5. **最終チェック**（LLM）— 通過するまで iterate
6. **SRT アセンブル**（機械）— pre-roll / post-roll / cut-point snap / UTF-8 BOM + CRLF で最終 SRT 生成

出力先: `~/ClaudeCode/projects/premiere-skills/output/srt/<basename>/`

最終 SRT を Premiere Pro で「ファイル → 読み込み」からインポートしてキャプショントラックに追加できる。

## 依存

- Python 3.9 以降
- `faster-whisper` (Whisper モデル)
- `ffmpeg` (音声解析)
- Claude Code CLI

インストール:
```bash
pip3 install --user faster-whisper
brew install ffmpeg
```

## 学習サイクル

SRT の品質を継続的に改善するため:

1. `/srt` で生成
2. Premiere Pro でテロップを手動微調整
3. 修正済み SRT と元の WAV/XML を Claude に渡して学習
4. 差分分析 → `memory/feedback_srt_grouping_rules.md` + `memory/telop_channel_patterns.md` に蓄積
5. 次回の生成に反映

## 関連プロジェクト（別管理）

- `~/ClaudeCode/projects/Premiere プラグイン/` — Adobe CEP 拡張（JavaScript 実装。本プロジェクトとは別物）
  - `silence-cut-plugin/` — `/cut` と同じ機能だが Premiere UI 内で動作する版
  - `effect-shortcut-plugin/` — エフェクトショートカット
  - `clip-clipboard-plugin/` — クリップクリップボード

## 履歴

- **v3** (2026-04-11): 機械と LLM の責任分担を明確化。rule-based 分割（fugashi + Tier ルール）を廃止し、LLM 主導の QA ループに移行。`emit-candidates` モード追加。`--output-dir` 対応。
- **v2** (2026-03頃): LLM セマンティック分割（fugashi ベース）。
- **v1** (2026-03 以前): BudouX 文字数分割。
