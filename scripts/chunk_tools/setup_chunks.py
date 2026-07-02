#!/usr/bin/env python3
"""入力(WAV/MP4) → 16k mono WAV → silencedetect で無音点を探し N チャンクへ分割。

/srt-fast 用（ベンチ版 _chunk_tools/setup_chunks.py の改良版）。
ベンチで判明した欠陥への対策を内蔵する:

  - 境界分断対策: 分割点は「強い無音(既定 d=0.7s)」の中点へスナップ。
    浅い 0.4s ポーズで文の途中を割るのを防ぐ。
  - 幻聴/境界欠落対策: 各チャンクは担当区間の前後へ OVERLAP 秒だけ食い込ませて
    抽出する(= chunk 末が無音で終わらない → Whisper の "ご視聴ありがとう" 系
    末尾幻聴が出にくい)。抽出範囲(ext)と担当範囲(owned)を別々に manifest へ記録し、
    後段(whisper_chunk.py)が owned 区間だけを採用してオーバーラップ分は捨てる。

usage:
  setup_chunks.py <input> <repo_dir> <n_chunks> [overlap_s=6.0] [silence_d=0.7]
"""
import sys
import re
import json
import subprocess
from pathlib import Path

src = sys.argv[1]
repo = sys.argv[2]
n = int(sys.argv[3])
OVERLAP = float(sys.argv[4]) if len(sys.argv) > 4 else 6.0
SILENCE_D = float(sys.argv[5]) if len(sys.argv) > 5 else 0.7

stem = Path(src).stem
out_dir = Path(repo) / "output" / "srt" / stem
out_dir.mkdir(parents=True, exist_ok=True)
full_wav = out_dir / f"{stem}.wav"

# 1) full WAV 16k mono（既存ならスキップ）
if not full_wav.exists():
    subprocess.run(
        ["ffmpeg", "-y", "-i", src, "-vn", "-ac", "1", "-ar", "16000",
         "-c:a", "pcm_s16le", str(full_wav)],
        check=True, capture_output=True,
    )

# 2) duration
probe = subprocess.run(
    ["ffprobe", "-v", "error", "-show_entries", "format=duration",
     "-of", "default=noprint_wrappers=1:nokey=1", str(full_wav)],
    capture_output=True, text=True,
)
duration = float(probe.stdout.strip())

# 3) silencedetect（強めの無音 = 文の切れ目を狙う）
sd = subprocess.run(
    ["ffmpeg", "-hide_banner", "-i", str(full_wav),
     "-af", f"silencedetect=noise=-30dB:d={SILENCE_D}", "-f", "null", "-"],
    capture_output=True, text=True,
)
log = sd.stderr
starts = [float(x) for x in re.findall(r"silence_start:\s*([0-9.]+)", log)]
ends = [float(x) for x in re.findall(r"silence_end:\s*([0-9.]+)", log)]
mids = []
for i, s in enumerate(starts):
    e = ends[i] if i < len(ends) else duration
    mids.append((s + e) / 2.0)

# 4) 等分ターゲット付近の無音中点へスナップ（候補が無ければ等分点で妥協）
WINDOW = 60.0
bounds = [0.0]
bounds_fallback = []  # 無音候補なしで等分点になった境界（発話途中分割の恐れ）
for i in range(1, n):
    target = duration * i / n
    cands = [m for m in mids if abs(m - target) <= WINDOW and m > bounds[-1] + 5]
    if cands:
        split = min(cands, key=lambda m: abs(m - target))
    else:
        split = target
        bounds_fallback.append(i)
        print(f"WARN: chunk境界{i} は±{WINDOW:.0f}s内に無音候補なし。"
              f"等分点 {target:.1f}s で妥協（発話途中分割の恐れ・境界付近は目視推奨）")
    bounds.append(round(split, 3))
bounds.append(duration)

# 5) チャンク WAV 抽出（owned 区間 ± OVERLAP を ext として切り出す）
chunks = []
for i in range(n):
    owned_start, owned_end = bounds[i], bounds[i + 1]
    ext_start = round(max(0.0, owned_start - (OVERLAP if i > 0 else 0.0)), 3)
    ext_end = round(min(duration, owned_end + (OVERLAP if i < n - 1 else 0.0)), 3)
    cw = out_dir / f"{stem}.chunk{i + 1}.wav"
    subprocess.run(
        ["ffmpeg", "-y", "-i", str(full_wav), "-ss", str(ext_start), "-to", str(ext_end),
         "-ac", "1", "-ar", "16000", "-c:a", "pcm_s16le", str(cw)],
        check=True, capture_output=True,
    )
    chunks.append({
        "idx": i + 1,
        "wav": str(cw),
        "offset": ext_start,        # chunk.wav local 0s = global ext_start
        "owned_start": owned_start,  # この区間だけを最終 SRT に採用
        "owned_end": owned_end,
        "ext_start": ext_start,
        "ext_end": ext_end,
        "dur": round(ext_end - ext_start, 3),
    })

manifest = {
    "stem": stem,
    "out_dir": str(out_dir),
    "full_wav": str(full_wav),
    "duration": round(duration, 3),
    "n": n,
    "overlap": OVERLAP,
    "silence_d": SILENCE_D,
    "bounds": bounds,
    "bounds_fallback": bounds_fallback,
    "silence_count": len(mids),
    "chunks": chunks,
}
mpath = out_dir / f"{stem}.chunks.json"
mpath.write_text(json.dumps(manifest, ensure_ascii=False, indent=2))
print("MANIFEST:", mpath)
print(json.dumps(manifest, ensure_ascii=False, indent=2))
