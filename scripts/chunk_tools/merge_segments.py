#!/usr/bin/env python3
"""チャンク転写結果（owned + overlap）を単一の segments リストへ統合する共有モジュール。

/srt の並列転写（transcribe_parallel.py・/srt-fast の CPU フォールバックも同経路）が使う。
（旧利用者 assemble_chunks.py は 2026-07-04 の /srt-fast v7.1 で廃止・削除済み）

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


def _contains(shorter: str, longer: str) -> bool:
    """shorter が longer の実質的な部分文字列か（誤検出防止に最小長6を要求）。"""
    return len(shorter) >= 6 and shorter in longer


def _fuzzy_dup(a: str, b: str) -> bool:
    if not a or not b:
        return False
    if a == b:
        return True
    s, l = (a, b) if len(a) <= len(b) else (b, a)
    return _contains(s, l)


def _union_intervals(items: list[dict]) -> list[list[float]]:
    """start昇順ソート済みだが end が単調とは限らない区間リストを、
    重複/近接(0.05s以内)をマージした非重複区間へ変換する。

    独立転写された隣接チャンクは、一方が長いセグメント(VADが切れ目を
    見つけられず境界を跨いで確保したもの)を、もう一方が短いセグメントで
    同じ時間帯を別々に転写することがある（start昇順ソートだけでは
    end が逆転し得る）。真の「空白区間」を見るには区間の和集合が必要。
    """
    cov: list[list[float]] = []
    for it in items:
        st, en = it["start"], it["end"]
        if cov and st <= cov[-1][1] + 0.05:
            cov[-1][1] = max(cov[-1][1], en)
        else:
            cov.append([st, en])
    return cov


def merge_owned_segments(
    owned_paths: list[str | Path],
    overlap_paths: list[str | Path] | None = None,
    gap_threshold: float = 1.5,
) -> tuple[list[dict], list[dict], list[tuple[float, float]]]:
    """owned segments を時刻順に統合し、真の空白区間だけを overlap から復元する。

    返り値: (merged_segments, recovered_segments, unresolved_gaps)
      unresolved_gaps は復元後もなお gap_threshold 超の空白（要目視）。

    2026-07-03 修正: 旧実装は「start昇順ソート後の隣接ペア」で gap を計算していたが、
    片方のチャンクが境界を跨ぐ長いセグメントを持つと end が逆転し、実際には
    既にカバーされている時間帯を「空白」と誤検出して overlap から重複行を
    復元してしまうバグがあった（実写E2Eで確認: 同一発話が2回SRTに出た）。
    区間の和集合（union）で真の空白のみを検出し、復元候補は既存owned segment
    の部分文字列でないもの（＝境界の二重転写でないもの）に限定する。
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

    coverage = _union_intervals(owned)
    gaps = [
        (coverage[i][1], coverage[i + 1][0])
        for i in range(len(coverage) - 1)
        if coverage[i + 1][0] - coverage[i][1] > gap_threshold
    ]

    recovered: list[dict] = []
    if gaps and overlap:
        owned_norms = [_norm(s.get("text", "")) for s in owned]
        for gap_start, gap_end in gaps:
            for cand in overlap:
                # gap 区間と実質的に重なる candidate のみ（±0.3s の遊び）
                if cand["end"] <= gap_start + 0.3 or cand["start"] >= gap_end - 0.3:
                    continue
                key = _norm(cand.get("text", ""))
                if not key:
                    continue
                # owned のどれかと部分一致していれば境界の二重転写とみなしスキップ
                if any(_fuzzy_dup(key, ot) for ot in owned_norms if ot):
                    continue
                recovered.append(cand)
                owned_norms.append(key)

    merged = sorted(owned + recovered, key=lambda s: s.get("start", 0.0))

    final_coverage = _union_intervals(merged)
    unresolved = [
        (round(final_coverage[i][1], 2), round(final_coverage[i + 1][0], 2))
        for i in range(len(final_coverage) - 1)
        if final_coverage[i + 1][0] - final_coverage[i][1] > gap_threshold
    ]

    return merged, recovered, unresolved
