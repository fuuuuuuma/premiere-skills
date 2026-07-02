export const meta = {
  name: 'srt-fast',
  description: 'WAV1個から分割込みで3分割並列SRT生成（/srtの高速版・組み立ては全文1回でアンカー誤マッチ回避）',
  phases: [
    { title: 'split', detail: 'WAVをsilencedetectでN分割（オーバーラップ付き・担当区間を記録）' },
    { title: 'transcribe-linebreak', detail: '各chunk(owned区間)をWhisper転写→意味区切り改行（N体並列・--from-textはしない）' },
    { title: 'assemble', detail: '全chunkのsegments+linesを連結し --from-text を1回だけ実行' },
  ],
}

// ── 固定パス（repo内・動画非依存） ──
const REPO = "/Users/kawamurafuushin/ClaudeCode/projects/常時運用/premiere-skills"
const CANONICAL = `${REPO}/scripts/whisper_to_srt.py`
const TOOLS = `${REPO}/scripts/chunk_tools`
const SETUP = `${TOOLS}/setup_chunks.py`
const HELPER = `${TOOLS}/whisper_chunk.py`
const ASSEMBLE = `${TOOLS}/assemble_chunks.py`
const MEM1 = `${REPO}/memory/feedback_srt_grouping_rules.md`
const MEM2 = `${REPO}/memory/telop_channel_patterns.md`
const CPU_THREADS = 4

// ── 入力（args でWAV絶対パスを渡す。分割は本Workflow内で実施） ──
//   Workflow({ scriptPath: "...srt_fast_workflow.js", args: "/abs/path/to/audio.wav" })
//   または args: { wav: "...", n: 3 }
const WAV = typeof args === 'string' ? args : (args && args.wav)
const N = (args && typeof args === 'object' && args.n) || 3
if (!WAV) throw new Error("args に WAV の絶対パスを渡してください（例: args:\"/abs/audio.wav\"）")

const MANIFEST_SCHEMA = {
  type: "object",
  additionalProperties: false,
  properties: {
    stem: { type: "string" },
    out_dir: { type: "string" },
    duration: { type: "number" },
    chunks: {
      type: "array",
      items: {
        type: "object",
        additionalProperties: false,
        properties: {
          idx: { type: "integer" },
          wav: { type: "string" },
          offset: { type: "number" },
          owned_start: { type: "number" },
          owned_end: { type: "number" },
        },
        required: ["idx", "wav", "offset", "owned_start", "owned_end"],
      },
    },
  },
  required: ["stem", "out_dir", "chunks"],
}

const CHUNK_SCHEMA = {
  type: "object",
  additionalProperties: false,
  properties: {
    idx: { type: "integer" },
    segCount: { type: "integer" },
    lineCount: { type: "integer" },
    whisperSeconds: { type: "number" },
    linesPath: { type: "string" },
  },
  required: ["idx", "lineCount", "linesPath"],
}

const ASSEMBLE_SCHEMA = {
  type: "object",
  additionalProperties: false,
  properties: {
    srtPath: { type: "string" },
    totalEntries: { type: "integer" },
    avgChars: { type: "number" },
    over25: { type: "integer" },
    under4: { type: "integer" },
    firstTimecode: { type: "string" },
    lastTimecode: { type: "string" },
    maxGapSeconds: { type: "number" },
  },
  required: ["srtPath", "totalEntries"],
}

function splitPrompt() {
  return `WAVを silencedetect で ${N} 分割します。パスは【】や空白を含むのでbashではダブルクオート必須。

## Step 1: 分割（bash・1回だけ）
\`\`\`
T0=$(date +%s); python3 "${SETUP}" "${WAV}" "${REPO}" ${N}; T1=$(date +%s); echo "SPLIT_SECONDS=$((T1-T0))"
\`\`\`
setup_chunks.py は WAV→16k mono化→強めの無音(d=0.7)で${N}分割し、各chunkを担当区間±オーバーラップで切り出す。標準出力の先頭に "MANIFEST: <path>" を出す。

## Step 2: manifest を読む
"MANIFEST:" の後ろの chunks.json を Read し、stem / out_dir / duration / chunks(各 idx, wav, offset, owned_start, owned_end) を取り出す。

## 返却(StructuredOutput)
stem, out_dir, duration, chunks[{idx,wav,offset,owned_start,owned_end}] を返す。`
}

function chunkPrompt(m, c) {
  const seg = `${m.out_dir}/${m.stem}.chunk${c.idx}.segments.json`
  const full = `${m.out_dir}/${m.stem}.chunk${c.idx}.fulltext.txt`
  const lines = `${m.out_dir}/${m.stem}.chunk${c.idx}.lines.txt`
  return `あなたは日本語トーク動画のSRTテロップ生成パイプラインのチャンク担当エージェントです。長いトークを時間で${m.chunks.length}分割した ${c.idx}/${m.chunks.length} 区間(担当グローバル区間 ${c.owned_start}s〜${c.owned_end}s)を担当します。自分の担当区間だけを処理。パスは【】や空白を含むのでbashでは必ずダブルクオートで囲む。

## Step A: Whisper転写（bash・1回だけ）
\`\`\`
T0=$(date +%s); python3 "${HELPER}" --audio "${c.wav}" --offset ${c.offset} --owned-start ${c.owned_start} --owned-end ${c.owned_end} --cpu-threads ${CPU_THREADS} --out "${seg}" --fulltext "${full}" --script "${CANONICAL}"; T1=$(date +%s); echo "WHISPER_SECONDS=$((T1-T0))"
\`\`\`
完了で ${seg}(担当区間のsegments.json・グローバル時刻baked) と ${full}(担当区間の修正済み全文) が生成。オーバーラップ食い込み分は helper 側で除去済み。固有名詞は既に正規化済み。WHISPER_SECONDS を控える。

## Step B: ルール読込（必須）
Readで次2ファイルを読む(全ルール厳守): ${MEM1} / ${MEM2}

## Step C: 全文を読む
Readで ${full} を読む。

## Step D: 意味区切り改行 → lines.txt を Write
${full} を意味の区切りで改行し各行=1テロップにして ${lines} に Write。要点:
- 意味の区切りで切る(平均14字前後・5〜25字目安だが文字数より意味優先)
- 複合動詞句は分断しない(〜ている/〜ていく/〜てくる/〜てみる/〜ておく/〜てしまう 等は1行)
- 副詞句(徹底的に/本当に/しっかりと)は次行の先頭
- 話題転換(説明→CTA・概念→具体例・肯定→疑問)で切る
- 「〜おり/ており」「〜ですけども」「〜なんですけど」で切る
- 文頭禁止: ます/まし/ない/とき/こと/助詞単独 等で始めない(前行に結合)
- フィラー「こう」「ちょっと」は文脈判断で残す(全削除禁止)
- メタコメント/時間稼ぎ/主観の冗長形は削除候補。句読点(、。)は使わない(中黒・半角スペースは可)
- 25字超は1%未満・4字未満も最小限
【最重要】固有名詞は ${full} の表記をそのまま使う(英字↔カタカナ変換しない＝累積ズレ防止)。基本は改行を入れるだけ。フィラー/メタの軽い削除のみ可。
【重要】この工程では --from-text や SRT 生成は実行しない。改行テキスト(${lines})を書くだけ。SRT 組み立ては最終フェーズで全文を一度に行う。

## 返却(StructuredOutput)
idx=${c.idx}, segCount(=${seg}配列長), lineCount(=${lines}非空行数), whisperSeconds, linesPath="${lines}" を返す。`
}

function assemblePrompt(m) {
  const out = `${m.out_dir}/${m.stem}.fast.srt`
  return `${m.chunks.length}個のchunkの segments と lines を全文連結し、行→時刻を difflib 全体アライメントで割り当てて最終SRTを組みます。パスは【】や空白を含むのでダブルクオート必須。

## Step 1: 組み立て（bash・1回だけ）
\`\`\`
python3 "${ASSEMBLE}" "${m.out_dir}/${m.stem}.chunks.json" "${CANONICAL}" "${out}"
\`\`\`
assemble_chunks.py が「owned segments を時刻順連結 → lines を連結(末尾幻聴フレーズ除去・境界重複行dedup) → 行↔word を difflib 全体アライメントで対応付け時刻割当 → refine_timing」して ${out} を生成する。canonical の前方バイアスのある局所アンカー(--from-text)は使わない(累積ドリフト/オーバーラン防止)。

## Step 2: 品質統計
${out} を読み、totalEntries / 本文1行平均文字数 avgChars / 25字超 over25 / 4字未満 under4 / 最初の "-->" 行 firstTimecode / 最後の "-->" 行 lastTimecode / 連続エントリ間の最大空白秒 maxGapSeconds を算出。

## 返却(StructuredOutput)
srtPath="${out}", totalEntries, avgChars, over25, under4, firstTimecode, lastTimecode, maxGapSeconds を返す。`
}

// ── 実行 ──
phase('split')
const manifest = await agent(splitPrompt(), { label: 'split', phase: 'split', schema: MANIFEST_SCHEMA, model: 'sonnet' })

phase('transcribe-linebreak')
const chunkResults = await parallel(
  manifest.chunks.map((c) => () =>
    agent(chunkPrompt(manifest, c), { label: `chunk${c.idx}`, phase: 'transcribe-linebreak', schema: CHUNK_SCHEMA, model: 'sonnet' })
  )
)
log(`chunks done: ${chunkResults.filter(Boolean).length}/${manifest.chunks.length}`)

phase('assemble')
const assembled = await agent(assemblePrompt(manifest), { label: 'assemble', phase: 'assemble', schema: ASSEMBLE_SCHEMA, model: 'sonnet' })

return { manifest, chunks: chunkResults, assembled }
