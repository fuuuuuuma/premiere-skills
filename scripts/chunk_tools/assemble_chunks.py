#!/usr/bin/env python3
"""全チャンクの segments と lines を統合し、canonical assemble_from_text
（v6・difflib 全体アライメント）で最終 SRT を組む。/srt-fast の最終段。

設計の変遷:
  - ベンチ版 merge_chunks.py: 各 chunk が個別に --from-text → 短い文脈でアンカー誤マッチ。
  - assemble v1: 全文連結 → canonical --from-text 1回。ただし当時の canonical は
    前方バイアスの局所アンカーで、3パス転写では累積ドリフト/オーバーランが発生。
  - assemble v2: 本ファイル内に difflib 全体アライメントを実装して解決
    （/srt との時刻差 median ±0.00s）。
  - assemble v3（現行・2026-07-02）: canonical whisper_to_srt.py の
    assemble_from_text が v6 で同じ全体アライメントを採用したため、本ファイルは
    ①segments 統合（owned+overlap の境界欠落復元 = merge_segments.py）
    ②lines 統合（幻聴除去・境界重複 dedup・復元 segment のテロップ行挿入）
    ③canonical assemble_from_text 呼び出し（QA レポート込み）
    のみを行う。アライメント実装の二重管理を解消。

usage:
  assemble_chunks.py <chunks.json> <canonical_whisper_to_srt.py> <out.srt>
"""
import sys
import json
import importlib.util
from collections import defaultdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from merge_segments import merge_owned_segments  # noqa: E402

manifest_path = sys.argv[1]
canonical = sys.argv[2]
out_srt = sys.argv[3]

# ── canonical を import（ヘルパー再利用） ──
spec = importlib.util.spec_from_file_location("w2s", canonical)
w2s = importlib.util.module_from_spec(spec)
spec.loader.exec_module(w2s)
apply_corrections = w2s.apply_corrections
norm = w2s._normalize_for_match

m = json.loads(Path(manifest_path).read_text())
out_dir = Path(m["out_dir"])
stem = m["stem"]
chunks = sorted(m["chunks"], key=lambda c: c["idx"])
bounds = m.get("bounds") or []

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

# ── 1) segments 統合（owned を時刻順連結 + overlap から境界欠落を復元） ──
owned_paths = [out_dir / f"{stem}.chunk{c['idx']}.segments.json" for c in chunks]
overlap_paths = [out_dir / f"{stem}.chunk{c['idx']}.overlap.json" for c in chunks]
all_segs, recovered, unresolved = merge_owned_segments(owned_paths, overlap_paths)
concat_seg = out_dir / f"{stem}.fast.segments.json"
concat_seg.write_text(json.dumps(all_segs, ensure_ascii=False, indent=2))
if recovered:
    print(f"[assemble] 境界欠落の復元: {len(recovered)} 件（overlap 転写から）")
if unresolved:
    print(f"[assemble] ⚠ 未解消の空白区間(>1.5s・要目視): {unresolved[:5]}")

# 復元 segment は chunk の lines.txt に対応行が無いので、境界位置にテロップ行として挿入する
rec_by_boundary: dict[int, list[dict]] = defaultdict(list)
for s in recovered:
    mid = (s["start"] + s["end"]) / 2
    if len(bounds) > 2:
        bi = min(range(1, len(bounds) - 1), key=lambda i: abs(bounds[i] - mid))
    else:
        bi = 1
    rec_by_boundary[bi].append(s)
for v in rec_by_boundary.values():
    v.sort(key=lambda s: s["start"])

# ── 2) lines をチャンク単位で読み（幻聴除去）、境界の重複行を dedup して連結 ──
def _normkey(s: str) -> str:
    return norm(apply_corrections(s))


def _fuzzy_dup(a: str, b: str) -> bool:
    """完全一致、または一方が他方の部分文字列（最小長6）なら同一発話とみなす。

    2026-07-03 修正: 境界前後のチャンクが独立にLLM改行するため、同じ発話が
    「このどちらか使っていただけたらなと思います」(chunk i 末尾) /
    「のでこのどちらか使っていただけたらなと思います」(chunk i+1 先頭、接続の
    「ので」が付いただけ)のように**非完全一致**で重複することがある。
    完全一致のみの旧実装ではこれを見逃し、同一内容が2行のSRTエントリに
    分裂して残っていた（実写E2Eで確認）。
    """
    if not a or not b:
        return False
    if a == b:
        return True
    s, l = (a, b) if len(a) <= len(b) else (b, a)
    return len(s) >= 6 and s in l

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
    if len(cl) < MAX_DEDUP:
        print(f"[assemble] ⚠ chunk{chunks[ci]['idx']} の行数が少ない ({len(cl)}行)")
    drop_k = 0
    if ci > 0 and prev_tail_keys:
        head_keys = [_normkey(x) for x in cl[:MAX_DEDUP]]
        kmax = min(MAX_DEDUP, len(prev_tail_keys), len(head_keys))
        for k in range(kmax, 0, -1):
            if all(_fuzzy_dup(pt, hk) for pt, hk in zip(prev_tail_keys[-k:], head_keys[:k])):
                drop_k = k
                break
    if drop_k:
        dropped_dup += drop_k
    all_lines.extend(cl[drop_k:])
    prev_tail_keys = [_normkey(x) for x in cl[-MAX_DEDUP:]]  # 自身の dedup 前 tail で比較
    # chunk ci と ci+1 の間の境界（bounds index = ci+1）に復元行を挿入
    for s in rec_by_boundary.get(ci + 1, []):
        text = s.get("text", "").strip()
        if text and text not in HALLUCINATION_LINES:
            all_lines.append(text)

concat_lines = out_dir / f"{stem}.fast.lines.txt"
concat_lines.write_text("\n".join(all_lines) + "\n")

print(f"[assemble] segments={len(all_segs)} lines={len(all_lines)} "
      f"dropped_hallucination={dropped_halluc} dropped_boundary_dup={dropped_dup} "
      f"recovered_lines={sum(len(v) for v in rec_by_boundary.values())}")

# ── 3) canonical assemble_from_text（v6 全体アライメント + refine_timing + QA） ──
w2s.assemble_from_text(str(concat_seg), str(concat_lines), out_srt)
print(f"[assemble] -> {out_srt}")
