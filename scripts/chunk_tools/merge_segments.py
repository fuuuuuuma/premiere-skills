#!/usr/bin/env python3
"""チャンク転写結果（owned + overlap）を単一の segments リストへ統合する共有モジュール。

/srt の並列転写（transcribe_parallel.py）と /srt-fast の組み立て
（assemble_chunks.py）の両方が使う。

境界欠落の恒久対策（2026-07-02）:
  チャンク境界を跨ぐ発話は、各チャンクが独立に VAD を通るため
  「どのチャンクの owned 区間にも segment が生成されない」ことがある
  （overlap で音声は両チャンクに渡っているが、Whisper が発話として
  検出するかまでは保証されない）。そこで whisper_chunk.py は owned 外の
  segment も .overlap.json に保存しておき、本モジュールが owned 連結後の
  時間カバレッジを検査して、gap_threshold 秒を超える空白区間に重なる
  overlap segment を復元する。
"""
from __future__ import annotations

import json
import re
from pathlib import Path

_NORM_RE = re.compile(r"[\s、。,\.!\?！？「」『』()（）\[\]【】・…ー~〜]+")


def _norm(s: str) -> str:
    return _NORM_RE.sub("", s)


def merge_owned_segments(
    owned_paths: list[str | Path],
    overlap_paths: list[str | Path] | None = None,
    gap_threshold: float = 1.5,
) -> tuple[list[dict], list[dict], list[tuple[float, float]]]:
    """owned segments を時刻順に統合し、大きな空白区間を overlap から復元する。

    返り値: (merged_segments, recovered_segments, unresolved_gaps)
      unresolved_gaps は復元後もなお gap_threshold 超の空白（要目視）。
    """
    owned: list[dict] = []
    for p in owned_paths:
        p = Path(p)
        if p.exists():
            owned.extend(json.loads(p.read_text()))
    owned.sort(key=lambda s: s.get("start", 0.0))

    overlap: list[dict] = []
    for p in overlap_paths or []:
        p = Path(p)
        if p.exists():
            overlap.extend(json.loads(p.read_text()))
    overlap.sort(key=lambda s: s.get("start", 0.0))

    recovered: list[dict] = []
    if owned and overlap:
        owned_texts = {_norm(s.get("text", "")) for s in owned}
        for i in range(len(owned) - 1):
            gap_start = owned[i]["end"]
            gap_end = owned[i + 1]["start"]
            if gap_end - gap_start <= gap_threshold:
                continue
            for cand in overlap:
                # gap 区間と実質的に重なる candidate のみ（±0.3s の遊び）
                if cand["end"] <= gap_start + 0.3 or cand["start"] >= gap_end - 0.3:
                    continue
                key = _norm(cand.get("text", ""))
                if not key or key in owned_texts:
                    continue  # 既存 segment と同一テキストは境界二重転写なのでスキップ
                recovered.append(cand)
                owned_texts.add(key)

    merged = sorted(owned + recovered, key=lambda s: s.get("start", 0.0))

    unresolved: list[tuple[float, float]] = []
    for i in range(len(merged) - 1):
        g0, g1 = merged[i]["end"], merged[i + 1]["start"]
        if g1 - g0 > gap_threshold:
            unresolved.append((round(g0, 2), round(g1, 2)))

    return merged, recovered, unresolved
