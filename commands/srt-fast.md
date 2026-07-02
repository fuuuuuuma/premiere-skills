---
description: WAV音声から3分割並列でSRT字幕を高速生成する /srt の高速版。Whisper転写と意味区切り改行を3チャンク並列化し、SRT組み立てだけ全文一括で行う。日本語トーク動画用。
---

# WAV → SRT 高速生成 (/srt-fast・3分割並列)

`/srt`（メインループ逐次・v5）の**高速版**。重い工程（Whisper 転写・意味区切り改行）を
N チャンクに分けて並列実行し、最後の SRT 組み立てだけ全文を一度に行う。

実測（テスト.wav 653秒 / 2026-05-29 ベンチ）: 通常 `/srt` 593.5秒 に対し 3分割版は約337秒で **約1.8倍速**。

## 設計原則（ベンチで判明した欠陥への対策を内蔵・v6 で canonical と実装統合）

```
[split]   silencedetect(強め d=0.7)で N 分割。各chunkは担当区間±オーバーラップで抽出   [約1秒]
[並列]    各chunk: Whisper(large-v3) → 意味区切り改行(lines.txt)                        [3並列で約3分]
[assemble] segments統合(境界欠落をoverlapから復元) + lines連結(dedup) → canonical
           assemble_from_text(difflib全体アライメント+QA) → 最終SRT                     [約10秒]
─────────────────────────────────────────────────────────
合計: 約5〜6分（音声長に依存）
```

- **累積ドリフト/オーバーラン対策（2026-05-30 に本スキル側で実証 → 2026-07-02 に canonical v6 へ移植）**:
  行連結文字列 ↔ word連結文字列を `difflib.SequenceMatcher` で全体アライメントし、各行スパンを
  word 文字位置へ単調写像して時刻割当（全体最適なので前方ラチェットが原理的に起きない）。
  → /srt との時刻差 median ±0.00s・オーバーランなし。現在は canonical `assemble_from_text`
  が同アルゴリズムを実装しており、assemble_chunks.py はそれを呼び出すだけ（二重管理を解消）。
- **chunk末の幻聴対策（「ご視聴ありがとうございました」等）**: 各chunkを担当区間より数秒広く
  抽出し（chunk末が無音で終わらない）、assemble で定番幻聴フレーズを除去する保険を入れる。
- **境界分断対策**: 分割点は浅い 0.4s ポーズでなく強い無音(d=0.7・文の切れ目)へスナップ。
  無音候補が見つからず等分点分割になった境界は manifest の `bounds_fallback` に記録され
  警告が出る（境界付近は目視推奨）。
- **境界欠落対策（2026-07-02 解決）**: 所有判定をセグメント開始基準→中点(midpoint)基準に変更し、
  owned 外セグメントも `.overlap.json` に退避。assemble が owned 連結の時間カバレッジを検査し、
  1.5s 超の空白区間に重なる overlap セグメントを復元してテロップ行にも挿入する。

## 重要ルール

- **canonical（`scripts/whisper_to_srt.py`）が時刻割当の単一実装**。chunk_tools は
  分割・並列転写・統合のみを担い、アライメントは canonical に委譲する。
- **自律動作**: ユーザー確認不要。WAV を受けたら即実行する。
- **XML 非対応**: 本バージョンは WAV 単体専用（カット点同期 XML が必要なら `/srt` を使う）。
- 出力は `<stem>.fast.srt`（`/srt` の `<stem>.srt` と衝突しないよう別名）。
- **品質目標は /srt と共通**: 25字超 1%未満・平均14字前後（`references/srt_runtime_rules.md` 正典）。

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

## 既知の限界（2026-05-30 テスト.wav 実測 / 2026-07-03 実写E2E再計測）

幻聴・境界分断・**累積ドリフト/オーバーラン**・**境界重複**（2026-07-03 修正）は解消。
残る品質差は 25字超（chunkエージェントのLLMばらつきに起因）:

| 指標 | /srt(A案) | /srt-fast(テスト.wav) | /srt-fast(実写Workflow・221s会話) | 備考 |
|---|---|---|---|---|
| エントリ数 | 326 | 290 | 60 | |
| 平均文字数 | 13.7 | 15.22 | 16.6 | fast は長め |
| 25字超 | 0 | 8 (約3%) | 9 (**15%**) | ★2人会話は特に高め。**共通目標は1%未満**（runtime_rules正典） |
| /srt との時刻差 | — | median ±0.00s | 未計測 | difflib 全体アライメントで解消 |
| 境界重複/欠落 | — | なし | なし(修正後) | 2026-07-03: 実写Workflowで重複バグ発見→修正（下記） |
| 実測ワークフロー全体時間 | — | — | 約175秒 | 3並列agent（split→transcribe×3→assemble）の合計wall time |

- **25字超は会話動画で特に出やすい**: 2026-07-03 の実写E2E（2人会話・221秒）では 15%
  （9/60）と、モノローグ台本読みのテスト.wav実測(3%)より大幅に高かった。各チャンクが
  音声の 1/N しか見ず密度較正できない上、会話は文の区切りが台本より曖昧なため。
  件数は chunk エージェントの改行 LLM ばらつきで run ごとに変動する。
  読みやすさ最優先の本番は `/srt`、量産・下書き・速度優先は `/srt-fast`、と使い分ける。
  assemble 出力の QA レポートで 25字超が多い場合は `<stem>.fast.lines.txt` を修正して
  assemble_chunks.py を再実行すればよい。
- **境界重複バグ（2026-07-03 発見・修正）**: 実際の Workflow 実行（合成データではなく本物の
  chunk エージェント出力）でのみ再現する境界系バグを発見。詳細は
  `memory/feedback_srt_grouping_rules.md` を参照。教訓: 境界系の検証は必ず実際の
  Workflow 実行で行うこと（手作りデータでは再現しない）。
- `n=1` 由来の速度比（約1.8倍）は反復計測で確定すること（`project_srt_benchmark_handoff.md`）。

## 関連ファイル

- `scripts/chunk_tools/srt_fast_workflow.js` — 本スキルが起動する Workflow 本体
- `scripts/chunk_tools/setup_chunks.py` — 分割（オーバーラップ＋担当区間＋強い無音スナップ＋fallback警告）
- `scripts/chunk_tools/whisper_chunk.py` — 1チャンク転写（中点所有判定・overlap退避・転写paramはcanonical一致）
- `scripts/chunk_tools/merge_segments.py` — owned+overlap統合と境界欠落復元（/srt並列転写と共用）
- `scripts/chunk_tools/assemble_chunks.py` — 統合＋dedup＋canonical assemble_from_text 呼び出し
- `scripts/whisper_to_srt.py` — canonical（時刻割当の単一実装・QAレポート内蔵）
- `references/srt_runtime_rules.md` — 改行・固有名詞・品質目標の実行時ルール正典（chunkエージェントが読む）
```
