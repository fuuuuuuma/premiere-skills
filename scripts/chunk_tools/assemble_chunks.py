#!/usr/bin/env python3
"""全チャンクの「担当区間(owned)」セグメントと改行テキスト(lines)を連結し、
行→時刻の割り当てを difflib 全体アライメントで行って最終 SRT を組む。

/srt-fast の最終段。設計の変遷:
  - ベンチ版 merge_chunks.py: 各 chunk が個別に --from-text → 短い文脈でアンカー誤マッチ。
  - 旧 assemble(v1): segments+lines を全文連結してから canonical --from-text を 1 回。
      → しかし canonical の _local_anchor は「行頭 prefix を pos±60 で find() し最初の一致へ
        前方ジャンプ・min_pos で後戻り禁止」という貪欲・前方バイアスの局所同期。
        単一転写の /srt では lines と word 列がほぼ完全一致するため発火しないが、
        3 パス転写 + 境界アーティファクト(重複/欠落) を持つ /srt-fast では
        繰り返し語や境界重複に吸着して pos が前方へラチェット蓄積 → テロップが
        単調に後ろ倒れ → 末尾が音声長をオーバーラン、という累積ドリフトを起こした。
  - 現行 assemble(v2・本ファイル): word タイムスタンプは /srt と ±0.02s で一致する
      （= 転写時刻は正しい）ことを確認済み。よって壊れているのは「行→文字位置」の
      対応付けだけ。canonical 本体(whisper_to_srt.py / /srt)は不変更のまま、本ファイル内で
      行連結文字列 ↔ word 連結文字列を difflib.SequenceMatcher で全体アライメントし、
      各行の文字スパンを word 列の文字位置へ単調・双方向に写像して時刻を割り当てる。
      全体最適なので前方バイアスのラチェットが原理的に起きない。

加えて 2 つの境界アーティファクトに対処する:
  1) 末尾幻聴フレーズ（「ご視聴ありがとうございました」等）を lines から除去（保険）。
  2) チャンク境界の重複テロップ: chunk i 末尾と chunk i+1 先頭が同一テキストになる
     （chunk i の末尾セグメントが owned 境界を跨いで残り、chunk i+1 が同じ発話を再転写する
     ため）。chunk i+1 の先頭で重複する行を落とす。

canonical からは apply_corrections / _normalize_for_match / refine_timing / to_srt_time /
FPS を import して再利用する（読み取り専用・改変しない）。

usage:
  assemble_chunks.py <chunks.json> <canonical_whisper_to_srt.py> <out.srt>
"""
import sys
import json
import bisect
import difflib
import importlib.util
from pathlib import Path

manifest_path = sys.argv[1]
canonical = sys.argv[2]
out_srt = sys.argv[3]

# ── canonical を import（ヘルパー再利用・無改変） ──
spec = importlib.util.spec_from_file_location("w2s", canonical)
w2s = importlib.util.module_from_spec(spec)
spec.loader.exec_module(w2s)
apply_corrections = w2s.apply_corrections
norm = w2s._normalize_for_match
refine_timing = w2s.refine_timing
to_srt_time = w2s.to_srt_time
FPS = w2s.FPS

m = json.loads(Path(manifest_path).read_text())
out_dir = Path(m["out_dir"])
stem = m["stem"]
chunks = sorted(m["chunks"], key=lambda c: c["idx"])

# 定番の末尾幻聴フレーズ（無音に対し Whisper が捏造しがち）
HALLUCINATION_LINES = {
    "ご視聴ありがとうございました",
    "ご清聴ありがとうございました",
    "最後までご視聴いただきありがとうございました",
    "ご視聴いただきありがとうございました",
    "チャンネル登録お願いします",
    "チャンネル登録よろしくお願いします",
    "おわり",
    "終わり",
}

# ── 1) segments を全文連結（owned-only・既にグローバル時刻）→ 時刻順ソート ──
all_segs = []
for c in chunks:
    seg_path = out_dir / f"{stem}.chunk{c['idx']}.segments.json"
    if seg_path.exists():
        all_segs.extend(json.loads(seg_path.read_text()))
all_segs.sort(key=lambda s: s.get("start", 0.0))
concat_seg = out_dir / f"{stem}.fast.segments.json"
concat_seg.write_text(json.dumps(all_segs, ensure_ascii=False, indent=2))

# ── 2) lines をチャンク単位で読み（幻聴除去）、境界の重複行を dedup して連結 ──
def _normkey(s: str) -> str:
    return norm(apply_corrections(s))

chunk_lines: list[list[str]] = []
dropped_halluc = 0
for c in chunks:
    lines_path = out_dir / f"{stem}.chunk{c['idx']}.lines.txt"
    cl: list[str] = []
    if lines_path.exists():
        for ln in lines_path.read_text().splitlines():
            s = ln.strip()
            if not s:
                continue
            if s in HALLUCINATION_LINES:
                dropped_halluc += 1
                continue
            cl.append(s)
    chunk_lines.append(cl)

# 境界 dedup: chunk i+1 の先頭が chunk i の末尾と同一テキストなら落とす（最大 3 行）
MAX_DEDUP = 3
dropped_dup = 0
all_lines: list[str] = []
prev_tail_keys: list[str] = []
for ci, cl in enumerate(chunk_lines):
    drop_k = 0
    if ci > 0 and prev_tail_keys:
        head_keys = [_normkey(x) for x in cl[:MAX_DEDUP]]
        kmax = min(MAX_DEDUP, len(prev_tail_keys), len(head_keys))
        for k in range(kmax, 0, -1):
            if prev_tail_keys[-k:] == head_keys[:k]:
                drop_k = k
                break
    if drop_k:
        dropped_dup += drop_k
    kept = cl[drop_k:]
    all_lines.extend(kept)
    prev_tail_keys = [_normkey(x) for x in cl[-MAX_DEDUP:]]  # 自身の dedup 前 tail で比較

concat_lines = out_dir / f"{stem}.fast.lines.txt"
concat_lines.write_text("\n".join(all_lines) + "\n")

print(f"[assemble] segments={len(all_segs)} lines={len(all_lines)} "
      f"dropped_hallucination={dropped_halluc} dropped_boundary_dup={dropped_dup}")

# ── 3) word 列を構築（canonical assemble_from_text と同一規則・XML offset なし） ──
def build_word_stream(seg_list):
    words = []
    for seg in seg_list:
        seg_words = seg.get("words", [])
        if not seg_words:
            text = apply_corrections(seg["text"])
            if text:
                words.append({"word": text, "start": seg["start"], "end": seg["end"]})
            continue
        raw_concat = "".join(w["word"] for w in seg_words)
        corrected = apply_corrections(raw_concat)
        if raw_concat == corrected:
            for w in seg_words:
                if w["word"]:
                    words.append({"word": w["word"], "start": w["start"], "end": w["end"]})
        else:
            seg_start = seg_words[0]["start"]
            seg_end = seg_words[-1]["end"]
            seg_dur = max(seg_end - seg_start, 0.01)
            n_full = len(corrected)
            if n_full == 0:
                continue
            for i, ch in enumerate(corrected):
                words.append({
                    "word": ch,
                    "start": seg_start + seg_dur * i / n_full,
                    "end": seg_start + seg_dur * (i + 1) / n_full,
                })
    return words

words = build_word_stream(all_segs)
if not words:
    print("エラー: Whisper 単語列が空です", file=sys.stderr)
    sys.exit(1)

char_to_word: list[int] = []
for widx, w in enumerate(words):
    for _ in norm(w["word"]):
        char_to_word.append(widx)
whisper_norm = "".join(norm(w["word"]) for w in words)
N = len(char_to_word)  # == len(whisper_norm)

# ── 4) 行連結文字列を作り、word 連結文字列へ difflib で全体アライメント ──
line_display = [apply_corrections(ln) for ln in all_lines]
line_norm = [norm(ld) for ld in line_display]

# 各行の [連結内開始, 終了) スパン
spans = []
acc = 0
for n_ in line_norm:
    spans.append((acc, acc + len(n_)))
    acc += len(n_)
line_cat = "".join(line_norm)

# 行連結位置 → word連結位置 の単調写像（一致ブロック端点で区分線形補間）
sm = difflib.SequenceMatcher(None, line_cat, whisper_norm, autojunk=False)
pts = [(0, 0)]
for a, b, size in sm.get_matching_blocks():
    if size <= 0:
        continue
    pts.append((a, b))
    pts.append((a + size, b + size))
pts.append((len(line_cat), N))
# lc 昇順・w 単調になるよう整理
pts = sorted(set(pts))
mono = []
last_w = -1
for lc, w in pts:
    if w >= last_w:
        mono.append((lc, w))
        last_w = w
mono_lc = [p[0] for p in mono]

def map_pos(p: int) -> float:
    """行連結位置 p を word連結位置へ（区分線形）。"""
    i = bisect.bisect_right(mono_lc, p) - 1
    i = max(0, min(i, len(mono) - 1))
    lc0, w0 = mono[i]
    if i + 1 < len(mono):
        lc1, w1 = mono[i + 1]
    else:
        return float(w0)
    if lc1 == lc0:
        return float(w0)
    return w0 + (w1 - w0) * (p - lc0) / (lc1 - lc0)

def clamp(v, lo, hi):
    return max(lo, min(hi, v))

# ── 5) 各行に時刻を割り当て（start 単調・start<end を保証） ──
entries: list[tuple[float, float, str]] = []
prev_start = words[0]["start"]
for (s_lc, e_lc), disp, nrm in zip(spans, line_display, line_norm):
    if not nrm or not disp:
        continue
    a0 = map_pos(s_lc)
    a1 = map_pos(e_lc)
    start_char = clamp(int(round(a0)), 0, N - 1)
    end_char = clamp(int(round(a1)) - 1, start_char, N - 1)
    start_t = words[char_to_word[start_char]]["start"]
    end_t = words[char_to_word[end_char]]["end"]
    if start_t < prev_start:
        start_t = prev_start
    if end_t <= start_t:
        end_t = start_t + 0.5
    entries.append((start_t, end_t, disp))
    prev_start = start_t

if not entries:
    print("エラー: エントリが生成されませんでした", file=sys.stderr)
    sys.exit(1)

# ── 6) タイミング整形（canonical 流用）＋ SRT 書き出し（Premiere 日本語版: BOM+CRLF） ──
entries = refine_timing(entries, FPS)
with open(out_srt, "w", encoding="utf-8-sig", newline="\r\n") as f:
    for i, (start, end, text) in enumerate(entries, 1):
        f.write(f"{i}\n")
        f.write(f"{to_srt_time(start, FPS)} --> {to_srt_time(end, FPS)}\n")
        f.write(f"{text}\n\n")

lens = [len(t) for _, _, t in entries]
last_end = entries[-1][1] if entries else 0.0
print(f"[assemble] エントリ数={len(entries)} 平均文字数={sum(lens)/len(lens):.2f} "
      f"25字超={sum(1 for l in lens if l > 25)} 4字未満={sum(1 for l in lens if l < 4)} "
      f"末尾end={last_end:.3f}s")
print(f"[assemble] -> {out_srt}")
