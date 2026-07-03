#!/usr/bin/env python3
"""1 チャンク(オーバーラップ付き)を faster-whisper で転写し、グローバル時刻へ
オフセットして「担当区間(owned)」のセグメントを segments.json / fulltext.txt
へ書き出す。owned 外のセグメントは捨てずに .overlap.json へ退避する。

/srt と /srt-fast の並列転写で共用。設計（2026-07-02 更新）:
  - 転写パラメータは canonical whisper_to_srt.py と完全一致。
  - 所有判定は「セグメント中点(midpoint)が owned 区間内か」。旧実装の
    開始時刻(g_start)基準では境界を跨ぐ発話が両チャンクから落ちやすかった。
    中点基準ならどのセグメントもちょうど1チャンクに所有される。
  - owned 外セグメントは --overlap-out に保存し、merge_segments.py が
    時間カバレッジ検査で欠落区間の復元に使う（境界欠落の恒久対策）。
  - cpu_threads は --jobs（並列本数）から自動算出（総コア数/並列本数）。

usage:
  whisper_chunk.py --audio chunk.wav --offset <ext_start> \
    --owned-start <s> --owned-end <e> --jobs 3 \
    --out chunk.segments.json --overlap-out chunk.overlap.json \
    --fulltext chunk.fulltext.txt --script <canonical.py>
"""
import argparse
import json
import os
import importlib.util
from pathlib import Path

ap = argparse.ArgumentParser()
ap.add_argument("--audio", required=True)
ap.add_argument("--offset", type=float, default=0.0, help="chunk local 0s のグローバル時刻(ext_start)")
ap.add_argument("--owned-start", type=float, required=True)
ap.add_argument("--owned-end", type=float, required=True)
ap.add_argument("--jobs", type=int, default=3, help="並列実行本数（cpu_threads 自動算出用）")
ap.add_argument("--cpu-threads", type=int, default=0,
                help="明示指定時はこちらを優先。0 なら総コア数/jobs で自動算出")
ap.add_argument("--out", required=True, help="owned segments.json 出力パス")
ap.add_argument("--overlap-out", default=None,
                help="owned 外 segments の退避先（省略時は <out> の .overlap.json）")
ap.add_argument("--fulltext", required=True, help="fulltext.txt 出力パス")
ap.add_argument("--script", required=True, help="canonical whisper_to_srt.py パス")
a = ap.parse_args()

cpu_threads = a.cpu_threads or max(2, (os.cpu_count() or 8) // max(1, a.jobs))
overlap_out = a.overlap_out or str(Path(a.out).with_suffix("")) + ".overlap.json"
if a.out.endswith(".segments.json"):
    overlap_out = a.overlap_out or a.out[: -len(".segments.json")] + ".overlap.json"

# canonical モジュールを import（run_whisper / apply_corrections / remove_fillers を再利用）
spec = importlib.util.spec_from_file_location("w2s", a.script)
w2s = importlib.util.module_from_spec(spec)
spec.loader.exec_module(w2s)

# ── 転写は canonical run_whisper に委譲 ──
# エンジン選択(mlx GPU / faster-whisper CPU)・転写パラメータ・補正処理を
# whisper_to_srt.py 一箇所に集約（分岐を持つと /srt と精度が乖離するため）。
print(f"[chunk] canonical run_whisper に委譲 cpu_threads={cpu_threads} (jobs={a.jobs})")
raw_segs = w2s.run_whisper(a.audio, cpu_threads=cpu_threads)

off = a.offset
os_, oe = a.owned_start, a.owned_end
seg_list = []
overlap_list = []
for seg in raw_segs:
    words = [{
        "word": wd["word"],
        "start": round(wd["start"] + off, 3),
        "end": round(wd["end"] + off, 3),
    } for wd in seg["words"]]
    entry = {
        "start": round(seg["start"] + off, 3),
        "end": round(seg["end"] + off, 3),
        "text": seg["text"],
        "words": words,
    }
    # 所有判定は中点基準（開始時刻基準だと境界を跨ぐ発話が両チャンクから落ちる）
    mid = (seg["start"] + seg["end"]) / 2 + off
    if os_ <= mid < oe:
        seg_list.append(entry)
    else:
        overlap_list.append(entry)

Path(a.out).write_text(json.dumps(seg_list, ensure_ascii=False, indent=2))
Path(overlap_out).write_text(json.dumps(overlap_list, ensure_ascii=False, indent=2))
Path(a.fulltext).write_text("".join(s["text"] for s in seg_list))
print(f"[chunk] owned[{os_},{oe}) segments={len(seg_list)} overlap_kept={len(overlap_list)} "
      f"offset=+{off}s -> {a.out}")
