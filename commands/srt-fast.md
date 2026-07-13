---
description: WAV/動画音声を単一パスGPU転写（mlx-whisper）した後、fulltextを意味区切りでN分割しLLM改行だけを並列化してSRT字幕を高速生成する /srt の高速版。日本語トーク動画用。
---

# WAV → SRT 高速生成 (/srt-fast・テキスト分割型 v7)

`/srt`（メインループ逐次・v5）の**高速版**。v7 では転写は canonical と同一の
単一パスGPU転写（mlx-whisper・gap補完内蔵）で行い、並列化の軸を「音声(時間)分割」ではなく
「転写後テキスト(意味)分割」に置く。重いのは改行工程だけになったため、そこだけをN体並列にする。

**v7.1（2026-07-04）: オーケストレーションも疑って軽量化**。ベンチ分解の結果、
Workflow機構と assemble エージェントは純オーバーヘッドと判明（実作業1秒に約50秒の起動費）。
改行は**直Agent並列**（1メッセージでN体・model: sonnet）、組み立てとQA修復は
**メインループのbash直叩き**に変更。実測見込み: bench.wav 221秒素材で
v6 約175秒 → v7.0 約154秒 → **v7.1 約55秒**（prepare 26s＋並列改行 24s＋組立1s＋修復0〜2周）。
詳細は「既知の限界と実測」参照。

**2026-07-03 (v6.1)**: 転写エンジンが mlx-whisper (Apple GPU) 既定になり、転写自体が
CPU 比 3倍以上速くなった。GPU は単一資源のため「音声分割→並列転写」のメリットが消え、
**2026-07-04 (v7)** で転写を単一パスに戻し、並列化をテキスト分割＋改行のみに変更した。
`SRT_WHISPER_ENGINE=cpu` で従来エンジン（`transcribe_parallel.py --jobs 3`）に強制フォールバック。

## 設計原則（v7.1・テキスト分割型）

```
[prepare]   bash直: 単一パスGPU転写（mlx-whisper・gap補完内蔵。/srt と完全同一品質・境界なし）
            → fulltext をポーズ位置でN分割し <stem>.parts.json を出力            [~26s/221s素材]
[linebreak] 直Agent並列: 1メッセージでN体起動（model: sonnet・改行のみ担当）      [~25s・長さ非依存]
[assemble]  bash直: lines連結 → canonical --from-text（difflib全体アライメント）
            → QA_JSON をメインループが読み、基準超過行だけ Edit→再実行（最大2周） [~1s＋修復0〜60s]
─────────────────────────────────────────────────────────
```

**v7.1 で Workflow と assemble エージェントを廃止した根拠（2026-07-04 分解ベンチ・実測）**:

| 検証した仮説 | 判定 | 実測 |
|---|---|---|
| assemble はエージェントが必要 | 偽 | bash直叩き約1秒 vs エージェント約50秒（実作業は同一） |
| 改行の分割並列は有効 | 真 | 全文1体=95秒（自己検証の脇道で14ツール消費）vs 2分割並列=24秒（各3ツール）。**小さく渡すほどエージェントは脇道に逸れない** |
| Workflow機構が必要 | 偽 | 直Agent並列で同品質。args文字列化などの機構費・制約も消える |
| 改行出力のアンカー圧縮（さらなる高速化） | 保留 | 生成時間はもう支配項でない（起動+ルール読込~15秒が主）。lines側の固有名詞修正能力を失う損失が上回る |

- **転写は並列化しない**: GPU単一資源のため、音声分割・オーバーラップ抽出・境界dedup・
  幻聴復元といった v6 の機構は丸ごと不要になった（該当コードはアーカイブとして残置）。
- **canonical（`scripts/whisper_to_srt.py`）が時刻割当の単一実装**。`--from-text` に
  difflib全体アライメントで委譲する。
- **品質目標は /srt と共通**: 25字超 1%未満・平均14字前後（`references/srt_runtime_rules.md` 正典）。
- 出力は `<stem>.fast.srt`（`/srt` の `<stem>.srt` と衝突しないよう別名）。
- **音声分割系（setup_chunks.py 等）が生きるのは CPU フォールバック時のみ**
  （transcribe_parallel.py 経由・/srt と共用）。BGM/環境音が多い素材で分割点検出の
  `--noise` 調整（既定 -30dB、効きが悪ければ -40 等）が必要なのはこの経路の話であり、
  v7 の GPU 経路（テキスト分割・silencedetect不使用）では該当しない。

## 使い方

```
/srt-fast /path/to/audio_or_video
```

パート数を変えたい場合は `prepare_text_parts.py --n <数>`（既定0=文字数から自動、上限10）。
カット点同期 XML がある場合は `--xml "<XML>"` を付けると manifest に記録され、
Step 4 の `--from-text` に引き渡される（2026-07-04 追加・実走検証は未実施）。

## 実行手順

### Step 0: 初回セットアップ確認（`config/channel_profile.md` が無い場合のみ）

`config/channel_profile.md` の存在を確認する。**存在すれば何もせず Step 1 へ**。
**存在しなければ**、本処理に入る前にユーザーへ次を1回にまとめて質問する（全項目任意・
「わからない/後で」でも構わないと伝える）:

1. チャンネル名（自己紹介テロップ等で使う）
2. テロップの目標文字数（既定値: 平均14字前後・25字超1%未満。変更したい場合のみ数値を）
3. このチャンネルでよく出る固有名詞・製品名・人名で、Whisperが誤変換しそうなもの（言い間違いの
   パターンが今分かる範囲でよい。無ければ空でよく、生成のたびに追記していけると伝える）
4. 半角スペースの使い方に強いこだわりがあるか（無ければ既定ルールのまま進める）

回答を受けて:
- `config/channel_profile.example.md` の書式に沿って `config/channel_profile.md` を作成
- 固有名詞の回答があれば `config/corrections.local.json` に `{"誤認識文字列": "正規表記"}` の
  形式（`config/corrections.example.json` 参照）で保存
- 「この設定は次回以降も自動で使われます。追加・修正したくなったら `config/` 内のファイルを
  直接編集するか、生成のたびに気づいた誤認識を教えてください」と伝えてから Step 1 へ進む

### Step 1: 入力確認

引数の音声/動画の絶対パスを確認する（存在しなければユーザーに確認）。パスは【】や空白を含み得るので
以降ダブルクオートで囲む。

### Step 2: 前処理（bash・単一パス転写＋テキスト分割・agentゼロ）

```
python3 "/Users/kawamurafuushin/ClaudeCode/projects/常時運用/premiere-skills/scripts/chunk_tools/prepare_text_parts.py" "<入力の絶対パス>"
```

**Bash タイムアウト: 600000ms（10分）必須**（GPU転写は音声長の約1/8だが長尺に備える）。
CPUフォールバック環境（mlx なし / `SRT_WHISPER_ENGINE=cpu`）で10分超が見込まれる長尺は、
`run_in_background: true` で実行し完了通知を待ってから次へ進む。

標準出力の `MANIFEST: <path>` が `<stem>.parts.json` の絶対パス。

### Step 3: manifest を読み、改行エージェントをN体並列起動（直Agent・1メッセージ）

Read で `<stem>.parts.json` を取得し、**parts の数だけ Agent を同一メッセージで起動**する
（`model: sonnet` 必須・`subagent_type: general-purpose`）。各エージェントのプロンプトは
次のテンプレート（`{...}` を manifest の値で置換）:

```
あなたは日本語トーク動画SRTテロップの「意味区切り改行」担当エージェントです。
転写済み全文を{n}分割したパート {idx}/{n} を担当します。あなたの仕事は改行だけです。

## Step 1: ルール正典を読む（必須・全ルール厳守）
Read: /Users/kawamurafuushin/ClaudeCode/projects/常時運用/premiere-skills/references/srt_runtime_rules.md
Read: /Users/kawamurafuushin/ClaudeCode/projects/常時運用/premiere-skills/config/channel_profile.md
（存在すれば。無ければスキップしてよい。存在すればそこに書かれたチャンネル固有の表記・目標値を優先する）

## Step 2: 担当パート全文を読む
Read: {parts[i].path}

## Step 3: 意味区切り改行 → Write
パート全文をルール正典に従って意味の区切りで改行し（各行=1テロップ）、
{parts[i].lines_out} に Write する。

【鉄則】冒頭・末尾が中途半端に見えても削除・要約・言い換えをせず全文をカバー
（削除して良いのはルール正典の削除規定該当箇所のみ）。SRT・タイムコード・行番号は
書かない。25字を超えそうな行は積極分割ルールで割る（目標: 25字超1%未満・平均14字前後）。
書き終えたら再読・再検証はせず即終了する。

最終応答は「LINES=<非空行数>」だけを返す。
```

- **「書き終えたら再読・再検証せず即終了」は速度の要**（これが無いと自己検証の
  脇道に入り3〜4倍遅くなることを実測済み）。
- 完了は `<task-notification>` で通知される。全パート完了まで待つ。

### Step 4: 組み立て＋QA（bash直・エージェント不使用）

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
" && SRT_QA_JSON=1 python3 "/Users/kawamurafuushin/ClaudeCode/projects/常時運用/premiere-skills/scripts/whisper_to_srt.py" \
  --from-text "<stem>.fast.lines.txt" --segments "<stem>.segments.json" -o "<stem>.fast.srt"
```

manifest の `xml` が null でなければ `--from-text` コマンドに `--xml "<manifestのxml>"` を追加する。
標準出力末尾の `QA_JSON: {...}` を読む。

### Step 5: QA修復（メインループが直接・最大2周）

`over25 > max(1, total×1%)` または `head_ng > 0` の場合のみ:
`QA_JSON` の `over25_items` / `head_ng_items` の各テキストは `<stem>.fast.lines.txt` の1行に
一致する。**該当行だけ**を Edit で修正し（25字超→ルール正典の積極分割で2行に / 文頭NG→
区切りを前行側へ移動。行の削除・要約・語の追加はしない）、Step 4 の `--from-text` コマンド
だけを再実行（約1秒）。2周やっても改善しなければ打ち切って現状を報告する。

### Step 6: 掃除と完了報告

```bash
rm -f "<out_dir>/<stem>".part*.txt "<out_dir>/<stem>".part*.lines.txt
```

1. 最終 SRT の絶対パス（`<stem>.fast.srt`）
2. 統計表（エントリ数・平均文字数・25字超・4字未満・最大空白秒・QA修復周回数）
3. 所要時間（prepare / 並列改行 / 組立・修復）
4. 「Premiere Pro にインポートできます」

## 既知の限界と実測

### 実測（bench.wav 221秒・2人会話 / 2026-07-04）

| 世代 | 構成 | 全体時間 | 25字超 |
|---|---|---|---|
| v6 | 音声3分割Workflow（agent5体・GPU競合転写） | 約175秒 | 9/60（**15%**） |
| v7.0 | テキスト分割Workflow（agent N+1体） | 約154秒（prepare26＋WF128） | 0%（このrunでは） |
| **v7.1** | prepare(bash)＋直Agent N体＋bash組立＋メイン修復 | **約55秒**（26＋24＋1＋修復0〜2周） | run変動0〜5.6%→**修復で1%未満に収束** |

- v7.1 の時間は同一 bench データでの工程別実測の合算（prepare 26.1s / 2体並列改行 24.4s /
  組立・QA 約1s）。長尺でも並列改行は約25〜30秒/パートのまま（6643字・6分割で実測25.7〜30.0s/体）。
- **25字超は改行LLMのrun変動が大きい**（同一入力で 0% と 5.6% を実測）。だから QA修復ループは
  任意機能ではなく必須部品。修復は `<stem>.fast.lines.txt` の該当行を直して
  `SRT_QA_JSON=1 python3 scripts/whisper_to_srt.py --from-text <lines> --segments <segments> -o <srt>`
  を再実行（実測約1秒）。
- エントリ数/平均文字数の代表値: 81行 / 12.9字（目標14字前後）。文頭NG・0ms・時間重複は 0。
  境界重複/欠落は**機構ごと消滅**（転写に境界なし。部境界118.9s の接続を目視確認済み）。
- 転写品質は /srt と完全同一（単一パス＋gap補完）。
- 新出の固有名詞誤認識を見つけたら `config/corrections.local.json`＋`memory/telop_channel_patterns.md` に
  追記し、上記 --from-text を再実行すれば表示行にも即反映される（実データ検証で実証済み）。

### v6実測（参考値・アーカイブ経路・2026-05-30 テスト.wav / 2026-07-03 実写E2E）

| 指標 | /srt(A案) | /srt-fast v6(テスト.wav) | /srt-fast v6(実写Workflow・221s会話) | 備考 |
|---|---|---|---|---|
| エントリ数 | 326 | 290 | 60 | |
| 平均文字数 | 13.7 | 15.22 | 16.6 | fast は長め |
| 25字超 | 0 | 8 (約3%) | 9 (**15%**) | ★2人会話は特に高め。**共通目標は1%未満**（runtime_rules正典） |
| /srt との時刻差 | — | median ±0.00s | 未計測 | difflib 全体アライメントで解消 |
| 実測ワークフロー全体時間 | — | — | 約175秒 | 3並列agent（split→transcribe×3→assemble）の合計wall time |

- v6 で 25字超が会話動画で多発した原因（各チャンクが音声の1/Nしか見ず密度較正できない）は、
  v7 のテキスト分割＋QA自動修復で実測 0% に解消（上表）。
  読みやすさ最優先の本番は `/srt`、量産・下書き・速度優先は `/srt-fast`、の使い分けは継続。
- v6 の教訓「境界系の検証は必ず実際の Workflow 実行で行う」は v7 でも維持
  （`memory/feedback_srt_grouping_rules.md`）。v7 の E2E も実 Workflow で実施済み。

## 関連ファイル（v7.1）

- `scripts/chunk_tools/prepare_text_parts.py` — 前処理（単一パスGPU転写＋fulltextのテキスト分割・manifest出力）
- `scripts/whisper_to_srt.py` — canonical（時刻割当の単一実装・`run_whisper`/`--from-text`/QAレポート・`SRT_QA_JSON=1`で機械可読出力）
- `references/srt_runtime_rules.md` — 改行・固有名詞・品質目標の実行時ルール正典（改行エージェントが読む）
- 旧経路の廃止記録: v7.0 の `srt_fast_workflow.js` と v6 の `assemble_chunks.py` は
  2026-07-04 に削除（実測で直Agent方式に劣後・二重管理解消。必要なら git 履歴から復元可）

## CPUフォールバックが使う音声分割系（transcribe_parallel.py 経由・/srt と共用）

- `scripts/chunk_tools/setup_chunks.py` — 音声を時間分割（オーバーラップ＋担当区間＋強い無音スナップ＋fallback警告）
- `scripts/chunk_tools/whisper_chunk.py` — 1チャンク転写（中点所有判定・overlap退避）
- `scripts/chunk_tools/merge_segments.py` — owned+overlap統合と境界欠落復元
