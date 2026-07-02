#!/usr/bin/env python3
"""1 チャンク(オーバーラップ付き)を faster-whisper で転写し、グローバル時刻へ
オフセットして「担当区間(owned)」のセグメントだけを segments.json / fulltext.txt
へ書き出す。

/srt-fast 用。ベンチ版 _chunk_tools/whisper_chunk.py の改良版:
  - 転写パラメータは canonical whisper_to_srt.py と完全一致（cpu_threads のみ並列用）。
  - --owned-start/--owned-end の外（= オーバーラップ食い込み分）のセグメントは捨てる。
    → chunk 末の無音由来幻聴・境界の二重採用を構造的に防ぐ。

usage:
  whisper_chunk.py --audio chunk.wav --offset <ext_start> \
    --owned-start <s> --owned-end <e> --cpu-threads 4 \
    --out chunk.segments.json --fulltext chunk.fulltext.txt --script <canonical.py>
"""
import argparse
import json
import platform
import importlib.util
from pathlib import Path

ap = argparse.ArgumentParser()
ap.add_argument("--audio", required=True)
ap.add_argument("--offset", type=float, default=0.0, help="chunk local 0s のグローバル時刻(ext_start)")
ap.add_argument("--owned-start", type=float, required=True)
ap.add_argument("--owned-end", type=float, required=True)
ap.add_argument("--cpu-threads", type=int, default=4)
ap.add_argument("--out", required=True, help="segments.json 出力パス")
ap.add_argument("--fulltext", required=True, help="fulltext.txt 出力パス")
ap.add_argument("--script", required=True, help="canonical whisper_to_srt.py パス")
a = ap.parse_args()

# canonical モジュールを import（apply_corrections / remove_fillers を再利用）
spec = importlib.util.spec_from_file_location("w2s", a.script)
w2s = importlib.util.module_from_spec(spec)
spec.loader.exec_module(w2s)

from faster_whisper import WhisperModel  # noqa: E402

if platform.system() == "Darwin":
    device, compute_type = "cpu", "int8"
else:
    device, compute_type = "auto", "auto"

print(f"[chunk] WhisperModel large-v3 device={device} cpu_threads={a.cpu_threads}")
model = WhisperModel("large-v3", device=device, compute_type=compute_type,
                     cpu_threads=a.cpu_threads)

# ── 転写パラメータは canonical whisper_to_srt.py と一致（変えると /srt と精度が乖離する）──
segments, _ = model.transcribe(
    a.audio,
    language="ja",
    word_timestamps=True,
    vad_filter=True,
    vad_parameters={
        "threshold": 0.45,
        "min_silence_duration_ms": 500,
        "speech_pad_ms": 200,
    },
    beam_size=1,
    best_of=1,
    temperature=0.0,
    condition_on_previous_text=False,
    no_speech_threshold=0.6,
    hallucination_silence_threshold=2.0,
)

off = a.offset
os_, oe = a.owned_start, a.owned_end
seg_list = []
dropped = 0
for seg in segments:
    g_start = seg.start + off
    # 担当区間外（オーバーラップ食い込み分）は捨てる
    if g_start < os_ or g_start >= oe:
        dropped += 1
        continue
    raw = w2s.apply_corrections(seg.text.strip())
    clean = w2s.remove_fillers(raw)
    if not clean:
        continue
    words = []
    if seg.words:
        for wd in seg.words:
            wr = w2s.apply_corrections(wd.word.strip())
            wc = w2s.remove_fillers(wr)
            if wc:
                words.append({
                    "word": wc,
                    "start": round(wd.start + off, 3),
                    "end": round(wd.end + off, 3),
                })
    seg_list.append({
        "start": round(seg.start + off, 3),
        "end": round(seg.end + off, 3),
        "text": clean,
        "words": words,
    })

Path(a.out).write_text(json.dumps(seg_list, ensure_ascii=False, indent=2))
Path(a.fulltext).write_text("".join(s["text"] for s in seg_list))
print(f"[chunk] owned[{os_},{oe}) segments={len(seg_list)} dropped_overlap={dropped} "
      f"offset=+{off}s -> {a.out}")
