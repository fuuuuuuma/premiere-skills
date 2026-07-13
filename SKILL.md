---
name: premiere-skills
description: Premiere Pro 動画編集ワークフローを Claude Code で自動化するスキル集。/cut は Premiere Pro XML の無音・雑音区間をジェットカット、/srt は WAV+Premiere XML から日本語テロップ用 SRT を並列Whisper→LLM意味区切り→全体アライメントSRT の3ステップ(v6)で生成、/srt-fast は改行工程まで3チャンク並列化した高速版。faster-whisper / ffmpeg / Premiere Pro を要するローカル実行型。日本語トーク動画のショート / ロング編集向け。
---

# premiere-skills

Premiere Pro と Claude Code を組み合わせた動画編集自動化スキル集。`/cut` `/srt` `/srt-fast` の 3 コマンドを提供する。

## 提供コマンド

| コマンド | 用途 | 入力 | 出力 |
|---|---|---|---|
| `/cut` | Premiere Pro XML の無音・雑音区間をジェットカット | `.xml` | `output/cut/<basename>_カット済み.xml` |
| `/srt` | WAV + Premiere Pro XML から日本語テロップ用 SRT を生成 (並列Whisper→LLM改行→全体アライメント・v6) | `.wav` + `.xml` | `output/srt/<basename>/<basename>.srt` ほか中間 JSON |
| `/srt-fast` | /srt の高速版（v7）。単一パスGPU転写＋改行のみN並列＋QA自動修復。`--xml` 併用でカット点同期も可（canonical共通） | `.wav`（＋任意 `.xml`） | `output/srt/<basename>/<basename>.fast.srt` |

## 使い方

### /cut — 無音ジェットカット

Premiere Pro で対象シーケンスから「ファイル → 書き出し → Final Cut Pro XML」を出力し:

```
/cut /path/to/your.xml
```

### /srt — テロップ用 SRT 生成

WAV (16kHz / モノラル / 16bit 推奨) と XML を出力してから:

```
@/path/to/audio.wav @/path/to/timeline.xml /srt
```

### /srt-fast — 高速版 SRT 生成

WAV (16kHz / モノラル / 16bit 推奨) を出力してから:

```
/srt-fast /path/to/audio_or_video
```

**初回実行時のみ**、チャンネル名・目標文字数・固有名詞辞書などを尋ねるセットアップ対話が
入る（次回以降はスキップされる）。詳細は次の「初回セットアップ」を参照。

## 初回セットアップ（チャンネル設定）

このリポジトリには特定チャンネルの固有名詞・スタイル学習データは含まれない。
`/srt-fast` を初めて実行したとき、`config/channel_profile.md` が無ければチャンネル名・
目標文字数・固有名詞辞書・半角スペースの好みを質問し、回答を `config/channel_profile.md` と
`config/corrections.local.json`（いずれも gitignore 対象・個人設定）に保存する。2回目以降は
この設定を自動で使う。テンプレは `config/channel_profile.example.md` /
`config/corrections.example.json`。手動で作りたい場合はこれらをコピーして値を埋めればよい。

## 動作環境

- macOS / Linux (Premiere Pro 自体は別途必要)
- Python 3.9 以降
- `mlx-whisper` (Apple Silicon の既定転写エンジン・large-v3-turbo・GPU実行。2026-07-03 v6.1)
- `faster-whisper` (Whisper large-v3。gap補完・非Apple環境・フォールバックに必須)
- `ffmpeg`
- Claude Code CLI

```bash
pip3 install --user faster-whisper mlx-whisper
brew install ffmpeg
```

## ディレクトリ構成

- `commands/cut.md` / `commands/srt.md` / `commands/srt-fast.md` — スラッシュコマンド定義 (canonical)
- `scripts/silence_cut.py` / `scripts/whisper_to_srt.py` / `scripts/transcribe_parallel.py` — 実装スクリプト
- `scripts/chunk_tools/` — 並列転写・チャンク統合（/srt と /srt-fast が共用）
- `references/srt_runtime_rules.md` — テロップ改行の実行時ルール正典
- `memory/` — 日本語固有名詞辞書・SRT 切り分けルールの原典（履歴・根拠。配布時点では空テンプレ）
- `config/` — チャンネル固有設定（`channel_profile.md` / `corrections.local.json`。gitignore対象・
  セットアップ対話で自動生成。テンプレは `*.example.*`）
- `output/` — 成果物保存場所 (publish 対象外推奨)

詳細仕様は `commands/*.md` と `references/srt_runtime_rules.md` を参照。

## 配布モード

Premiere Pro / Whisper / ffmpeg などのローカル依存を持つため、Capafy では **Download モード** での配布を前提とする。クラウド実行は対象外。
