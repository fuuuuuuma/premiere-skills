#!/usr/bin/env python3
"""入力(WAV/MP4) → 単一パス転写 → fulltext をポーズ位置で N パートに分割（/srt-fast v7）。

v6 までの /srt-fast は「音声を時間で3分割 → 各チャンクで転写＋改行」だったが、
mlx-whisper GPU 化（2026-07-03）で転写が音声長の約1/10まで速くなり、GPU は単一資源
なので転写の並列化は逆にモデル多重ロード＋競合のコストだけが残った。
v7 は転写を単一パス（/srt と完全同一品質・境界なし）にし、並列化の軸を
「音声(時間)分割」から「転写後テキスト(意味)分割」へ移す。これにより
オーバーラップ抽出・中点所有判定・境界dedup・幻聴復元の機構が丸ごと不要になる。

処理:
  1) 16k mono WAV 変換（キャッシュ再利用）
  2) 転写: <stem>.segments.json が無ければ生成
     - mlx-whisper あり → canonical run_whisper() 単一パス（GPU・gap補完内蔵）
     - なし/SRT_WHISPER_ENGINE=cpu → transcribe_parallel.py --jobs 3 に委譲（従来の並列CPU）
  3) fulltext を segments のポーズ位置（文字数等分ターゲット近傍の最大ギャップ）で
     N 分割し、<stem>.part{i}.txt と parts manifest を出力

usage:
  prepare_text_parts.py <input> [--n 0(auto)] [--repo REPO]
"""
from __future__ import annotations

import argparse
import importlib.util
import json
import math
import os
import platform
import subprocess
import sys
import time
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent          # chunk_tools/
SCRIPTS = SCRIPT_DIR.parent                            # scripts/
DEFAULT_REPO = SCRIPTS.parent                          # repo root
CANONICAL = SCRIPTS / "whisper_to_srt.py"

# 1パートの目安文字数。LLM改行エージェントの生成時間と起動オーバーヘッドの均衡点
PART_TARGET_CHARS = 600
PART_MAX = 10
# 分割候補として優先する発話間ポーズの下限（これ未満しか無い窓では最大ギャップで妥協）
STRONG_GAP_S = 0.5


def _load_canonical():
    spec = importlib.util.spec_from_file_location("w2s", str(CANONICAL))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _mlx_available() -> bool:
    if platform.system() != "Darwin":
        return False
    if os.environ.get("SRT_WHISPER_ENGINE", "mlx") == "cpu":
        return False
    return importlib.util.find_spec("mlx_whisper") is not None


def ensure_wav16k(src: Path, out_dir: Path, stem: str) -> Path:
    full_wav = out_dir / f"{stem}.wav"
    if not full_wav.exists():
        subprocess.run(
            ["ffmpeg", "-y", "-i", str(src), "-vn", "-ac", "1", "-ar", "16000",
             "-c:a", "pcm_s16le", str(full_wav)],
            check=True, capture_output=True, timeout=3600,
        )
    return full_wav


def transcribe(src: Path, full_wav: Path, seg_path: Path) -> str:
    """segments.json を生成し、使用エンジン名を返す。既存キャッシュがあれば何もしない。"""
    if seg_path.exists():
        print(f"転写キャッシュあり: {seg_path}")
        return "cache"
    if _mlx_available():
        w2s = _load_canonical()
        segs = w2s.run_whisper(str(full_wav))
        seg_path.write_text(json.dumps(segs, ensure_ascii=False, indent=2))
        return "mlx-single-pass"
    # CPU 環境: 従来の3並列転写（境界復元込み）に委譲。同じ seg_path を出力する
    r = subprocess.run(
        [sys.executable, str(SCRIPTS / "transcribe_parallel.py"), str(src), "--jobs", "3"],
        timeout=7200,
    )
    if r.returncode != 0 or not seg_path.exists():
        print("エラー: 転写に失敗しました")
        sys.exit(1)
    return "cpu-parallel"


def split_parts(segs: list[dict], n_req: int) -> list[tuple[int, int]]:
    """segments を N パートに分割し、(開始segインデックス, 終了segインデックス+1) を返す。

    分割点は「文字数等分ターゲット近傍（±25%窓）で発話間ギャップが最大の境界」。
    強いポーズ＝文の切れ目で割るという v6 setup_chunks の方針をテキスト側で継承する。
    """
    texts = [s.get("text", "") for s in segs]
    cum = [0]
    for t in texts:
        cum.append(cum[-1] + len(t))
    total = cum[-1]

    if n_req > 0:
        n = n_req  # 明示指定は尊重する（短文でも潰さない）
    else:
        n = min(PART_MAX, max(1, math.ceil(total / PART_TARGET_CHARS)))
        if total < PART_TARGET_CHARS:
            n = 1  # 自動算出時のみ: 短文は分割しない（並列の旨みより起動コストが勝つ）
    n = max(1, min(n, len(segs)))
    if n == 1:
        return [(0, len(segs))]

    # 境界 b (seg b-1 | seg b) のギャップ秒
    gaps = {b: max(0.0, segs[b]["start"] - segs[b - 1]["end"]) for b in range(1, len(segs))}

    bounds = [0]
    for i in range(1, n):
        target = total * i / n
        window = 0.25 * total / n
        cands = [b for b in range(bounds[-1] + 1, len(segs))
                 if abs(cum[b] - target) <= window]
        if not cands:
            cands = [b for b in range(bounds[-1] + 1, len(segs))]
            if not cands:
                break
            b_pick = min(cands, key=lambda b: abs(cum[b] - target))
        else:
            strong = [b for b in cands if gaps[b] >= STRONG_GAP_S]
            pool = strong or cands
            # 強ポーズがあればターゲット最寄り、無ければ窓内最大ギャップで妥協
            if strong:
                b_pick = min(pool, key=lambda b: abs(cum[b] - target))
            else:
                b_pick = max(pool, key=lambda b: (gaps[b], -abs(cum[b] - target)))
        bounds.append(b_pick)
    bounds.append(len(segs))
    bounds = sorted(set(bounds))
    return [(bounds[i], bounds[i + 1]) for i in range(len(bounds) - 1)]


def main() -> None:
    ap = argparse.ArgumentParser(description="/srt-fast v7 前処理（単一パス転写＋テキスト分割）")
    ap.add_argument("input", help="入力 音声/動画 ファイル（WAV/MOV/MP4 等）")
    ap.add_argument("--n", type=int, default=0, help="パート数（0=文字数から自動）")
    ap.add_argument("--repo", default=str(DEFAULT_REPO), help="リポジトリルート")
    ap.add_argument("--xml", default=None,
                    help="Premiere FCP7 XML（カット点同期する場合。canonical --xml にそのまま渡る）")
    a = ap.parse_args()

    src = Path(a.input)
    if not src.exists():
        print(f"エラー: 入力が見つかりません: {src}")
        sys.exit(1)
    xml_path = None
    if a.xml:
        xml_path = Path(a.xml).expanduser().resolve()
        if not xml_path.exists():
            print(f"エラー: --xml が見つかりません: {xml_path}")
            sys.exit(1)

    t0 = time.time()
    stem = src.stem
    out_dir = Path(a.repo) / "output" / "srt" / stem
    out_dir.mkdir(parents=True, exist_ok=True)
    seg_path = out_dir / f"{stem}.segments.json"
    full_path = out_dir / f"{stem}.fulltext.txt"

    full_wav = ensure_wav16k(src, out_dir, stem)
    t_wav = time.time()

    engine = transcribe(src, full_wav, seg_path)
    t_tr = time.time()

    segs = json.loads(seg_path.read_text())
    segs = [s for s in segs if s.get("text", "").strip()]
    if not segs:
        print("エラー: 転写結果が空です")
        sys.exit(1)
    full_path.write_text("".join(s["text"] for s in segs))

    ranges = split_parts(segs, a.n)
    parts = []
    for i, (s0, s1) in enumerate(ranges, 1):
        text = "".join(s["text"] for s in segs[s0:s1])
        p = out_dir / f"{stem}.part{i}.txt"
        p.write_text(text)
        parts.append({
            "idx": i,
            "path": str(p),
            "lines_out": str(out_dir / f"{stem}.part{i}.lines.txt"),
            "chars": len(text),
            "start_s": round(segs[s0]["start"], 3),
            "end_s": round(segs[s1 - 1]["end"], 3),
            "gap_before_s": round(max(0.0, segs[s0]["start"] - segs[s0 - 1]["end"]), 3) if s0 > 0 else 0.0,
        })

    manifest = {
        "stem": stem,
        "out_dir": str(out_dir),
        "wav": str(full_wav),
        "segments": str(seg_path),
        "fulltext": str(full_path),
        "lines_out": str(out_dir / f"{stem}.fast.lines.txt"),
        "srt_out": str(out_dir / f"{stem}.fast.srt"),
        "xml": str(xml_path) if xml_path else None,
        "n": len(parts),
        "total_chars": sum(p["chars"] for p in parts),
        "engine": engine,
        "wav_seconds": round(t_wav - t0, 1),
        "transcribe_seconds": round(t_tr - t_wav, 1),
        "parts": parts,
    }
    mpath = out_dir / f"{stem}.parts.json"
    mpath.write_text(json.dumps(manifest, ensure_ascii=False, indent=2))
    print("MANIFEST:", mpath)
    print(json.dumps(manifest, ensure_ascii=False, indent=2))
    print(f"PREPARE_SECONDS={time.time() - t0:.1f} (wav={t_wav - t0:.1f}s "
          f"transcribe={t_tr - t_wav:.1f}s engine={engine})")


if __name__ == "__main__":
    main()
