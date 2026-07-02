---
description: WAV音声から3分割並列でSRT字幕を高速生成する /srt の高速版。Whisper転写と意味区切り改行を3チャンク並列化し、SRT組み立てだけ全文一括で行う。日本語トーク動画用。
---

# WAV → SRT 高速生成 (/srt-fast・3分割並列)

`/srt`（メインループ逐次・v5）の**高速版**。重い工程（Whisper 転写・意味区切り改行）を
N チャンクに分けて並列実行し、最後の SRT 組み立てだけ全文を一度に行う。

実測（テスト.wav 653秒 / 2026-05-29 ベンチ）: 通常 `/srt` 593.5秒 に対し 3分割版は約337秒で **約1.8倍速**。

## 設計原則（ベンチで判明した欠陥への対策を内蔵）

```
[split]   silencedetect(強め d=0.7)で N 分割。各chunkは担当区間±オーバーラップで抽出   [約1秒]
[並列]    各chunk: Whisper(large-v3) → 意味区切り改行(lines.txt)                        [3並列で約3分]
[assemble] 全chunkの segments+lines を連結 → 行↔word を difflib 全体アライメントで時刻割当 → 最終SRT  [約10秒]
─────────────────────────────────────────────────────────
合計: 約5〜6分（音声長に依存）
```

- **累積ドリフト/オーバーラン対策（最重要・2026-05-30 修正）**: canonical の `--from-text`
  は使わない。あれは行頭6字を `find()` で前方検索し最初の一致へ飛ぶ「前方バイアスのある局所
  アンカー」で、3パス転写＋境界アーティファクトを持つ fast では繰り返し語（「こういう」等）や
  境界重複に吸着して `pos` が前方へラチェット蓄積 → テロップが単調に後ろ倒れ → 末尾が音声長を
  オーバーランした（word 時刻は /srt と ±0.02s で正しく、壊れていたのは行→文字位置の対応だけ）。
  対策: assemble で行連結文字列 ↔ word連結文字列を `difflib.SequenceMatcher` で全体アライメント
  し、各行スパンを word 文字位置へ単調写像して時刻割当（全体最適なので前方ラチェットが原理的に
  起きない）。境界の完全重複行は dedup。→ /srt との時刻差 median ±0.00s・オーバーランなし。
- **chunk末の幻聴対策（「ご視聴ありがとうございました」等）**: 各chunkを担当区間より数秒広く
  抽出し（chunk末が無音で終わらない）、helper 側で担当区間外を捨てる。さらに assemble で
  定番幻聴フレーズを除去する保険を入れる。
- **境界分断対策**: 分割点は浅い 0.4s ポーズでなく強い無音(d=0.7・文の切れ目)へスナップ。

## 重要ルール

- **`/srt` 本体（`scripts/whisper_to_srt.py` / `commands/srt.md`）は一切変更しない**。
  本スキルは canonical を import / 呼び出すだけ（読み取り専用）。
- **自律動作**: ユーザー確認不要。WAV を受けたら即実行する。
- **XML 非対応**: 本バージョンは WAV 単体専用（カット点同期 XML が必要なら `/srt` を使う）。
- 出力は `<stem>.fast.srt`（`/srt` の `<stem>.srt` と衝突しないよう別名）。

## 使い方

```
/srt-fast /path/to/audio.wav
```

分割数を変えたい場合は Workflow の args をオブジェクトで渡す（既定 3）。

## 実行手順

### Step 1: 入力確認

引数の WAV 絶対パスを確認する（存在しなければユーザーに確認）。パスは【】や空白を含み得るので
以降ダブルクオートで囲む。

### Step 2: Workflow を起動（これだけ。分割・転写・改行・組み立ては全て内部で実行）

```
Workflow({
  scriptPath: "/Users/kawamurafuushin/ClaudeCode/projects/常時運用/premiere-skills/scripts/chunk_tools/srt_fast_workflow.js",
  args: "<WAV の絶対パス>"
})
```

- args は WAV の絶対パス文字列。分割数を変えるなら `args: { wav: "...", n: 4 }`。
- フェーズ: `split`（setup_chunks.py）→ `transcribe-linebreak`（whisper_chunk.py→改行・N体並列）
  → `assemble`（assemble_chunks.py で全文連結 + difflib 全体アライメント時刻割当）。
- バックグラウンド実行。`<task-notification>` 完了で結果（srtPath / totalEntries / avgChars /
  over25 / under4 / maxGapSeconds 等）が返る。

### Step 3: 完了報告

1. 最終 SRT の絶対パス（`<stem>.fast.srt`）
2. 統計表（エントリ数・平均文字数・25字超・4字未満・最大空白秒）
3. 「Premiere Pro にインポートできます」

## 既知の限界（2026-05-30 テスト.wav 実測で確認・運用で留意）

幻聴・境界分断・**累積ドリフト/オーバーラン**は解消。残る品質差は 25字超 と境界欠落のギャップ:

| 指標 | /srt(A案) | /srt-fast(実測) | 備考 |
|---|---|---|---|
| エントリ数 | 326 | 290 | fast は粗い |
| 平均文字数 | 13.7 | 15.22 | fast は長め |
| 25字超 | 0 | 8 (約3%) | ★要注意（run変動） |
| /srt との時刻差 | — | median ±0.00s | difflib 全体アライメントで解消 |
| 末尾オーバーラン | なし | なし | 旧:+3.5s超過 → 修正で消滅 |
| 最大エントリ間空白 | 0 | 3.5秒(1箇所) | 境界欠落（下記） |
| 幻聴フレーズ | なし | なし | 解消 |

- **25字超が出やすい**: 各チャンクが音声の 1/N しか見ず密度較正できないため、長いテロップが
  残る。件数は chunk エージェントの改行 LLM ばらつきで run ごとに変動。
  読みやすさ最優先の本番は `/srt`、量産・下書き・速度優先は `/srt-fast`、と使い分ける。
- **境界での内容欠落（未解決・C案候補）**: チャンク境界をまたぐ発話が、両チャンクの owned
  区間外に落ちて転写から消えることがある（テスト.wav では 1 箇所・約3.5秒のテロップ無し区間）。
  ドリフトとは別問題（転写段階の欠落）。対策案=assemble で全 chunk segments(overlap 含む)を
  時間的 dedup マージし、欠落区間のセグメント/行を復元する。境界付近は目視推奨。
- `n=1` 由来の速度比（約1.8倍）は反復計測で確定すること（`project_srt_benchmark_handoff.md`）。

## 関連ファイル

- `scripts/chunk_tools/srt_fast_workflow.js` — 本スキルが起動する Workflow 本体
- `scripts/chunk_tools/setup_chunks.py` — 分割（オーバーラップ＋担当区間＋強い無音スナップ）
- `scripts/chunk_tools/whisper_chunk.py` — 1チャンク転写（担当区間トリム・転写paramはcanonical一致）
- `scripts/chunk_tools/assemble_chunks.py` — 全文連結 + 境界重複dedup + difflib全体アライメント時刻割当（前方バイアスのある--from-textアンカーは不使用・累積ドリフト回避）
- `scripts/whisper_to_srt.py` — canonical（`/srt` と共用・無改変で import）
- `memory/feedback_srt_grouping_rules.md` / `memory/telop_channel_patterns.md` — 改行・固有名詞ルール
```
