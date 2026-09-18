#!/usr/bin/env python3
"""
動画 → タイムスタンプ付きコンタクトシート生成（/telop-check の前処理）

書き出し済みMP4を1秒間隔でサンプリングし、各フレームの左上に経過秒数を焼き込んだ上で
グリッド画像（既定4x4=16枚/シート）に敷き詰める。エージェントがテロップの誤字・数字表記・
い抜き・ら抜き・固有名詞ミスを目視で拾う際の入力になる。

設計の要点:
- drawtext はタイル敷き詰めより前段（select より前）で焼き込む。select で間引いても
  各フレームの frame_num（= fps=1 なので実秒と一致）はズレない。
- --stride で間引き可能（既定1=間引きなし）。長尺動画で枚数を抑えたいときに使う。
- manifest.json はグリッド枚数と設定値のみを記録する。各フレームの実秒はグリッド画像
  内に焼き込み済みの数字を直接読ませる方式とし、秒数レンジの事前計算はしない
  （select 後は連番が飛ぶため、計算に頼ると簡単にズレる）。

usage:
    python3 telop_frames.py input.mp4 --out work/telop_check/<stem>
    python3 telop_frames.py input.mp4 --out work/telop_check/<stem> --stride 2  # 20分超の長尺向け
"""
from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path


def ffprobe_duration(video_path: str) -> float:
    out = subprocess.run(
        [
            "ffprobe", "-v", "error",
            "-show_entries", "format=duration",
            "-of", "default=noprint_wrappers=1:nokey=1",
            video_path,
        ],
        capture_output=True, text=True, check=True,
    )
    return float(out.stdout.strip())


def build_filter(stride: int, cols: int, rows: int, cell_w: int, cell_h: int) -> str:
    return (
        "fps=1,"
        "drawtext=text='%{frame_num}s':start_number=0:x=8:y=8:fontsize=26:"
        "fontcolor=yellow:box=1:boxcolor=black@0.7,"
        f"select='not(mod(n\\,{stride}))',"
        f"scale={cell_w}:{cell_h},"
        f"tile={cols}x{rows}"
    )


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("video", help="入力動画パス（書き出し済みMP4等）")
    ap.add_argument("--out", required=True, help="出力先ディレクトリ（グリッド画像とmanifest.jsonを書く）")
    ap.add_argument("--stride", type=int, default=1, help="サンプリング間引き（秒単位。既定1=毎秒）")
    ap.add_argument("--cols", type=int, default=4)
    ap.add_argument("--rows", type=int, default=4)
    ap.add_argument("--cell-width", type=int, default=480)
    ap.add_argument("--cell-height", type=int, default=270)
    args = ap.parse_args()

    video = Path(args.video)
    if not video.exists():
        sys.exit(f"動画が見つかりません: {video}")
    if args.stride < 1:
        sys.exit("--stride は1以上の整数で指定してください")

    out_dir = Path(args.out)
    out_dir.mkdir(parents=True, exist_ok=True)
    for stale in out_dir.glob("grid_*.png"):
        stale.unlink()

    duration = ffprobe_duration(str(video))
    per_grid = args.cols * args.rows
    sampled_frames = duration / args.stride
    approx_grid_count = max(1, -(-int(sampled_frames) // per_grid))  # ceil

    if approx_grid_count > 60:
        print(
            f"[警告] 想定グリッド枚数が {approx_grid_count} 枚（動画長 {duration:.0f}秒）。"
            f" --stride を上げる（例: --stride 2）と半分程度に減らせます。",
            file=sys.stderr,
        )

    vf = build_filter(args.stride, args.cols, args.rows, args.cell_width, args.cell_height)
    grid_pattern = str(out_dir / "grid_%03d.png")
    cmd = ["ffmpeg", "-y", "-i", str(video), "-vf", vf, "-vsync", "0", grid_pattern]
    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0:
        sys.exit(f"ffmpeg失敗:\n{result.stderr[-4000:]}")

    grids = sorted(out_dir.glob("grid_*.png"))
    if not grids:
        sys.exit("グリッド画像が1枚も生成されませんでした。動画・ffmpegの状態を確認してください。")

    manifest = {
        "video": str(video.resolve()),
        "duration_sec": round(duration, 2),
        "stride_sec": args.stride,
        "cols": args.cols,
        "rows": args.rows,
        "frames_per_grid": per_grid,
        "grid_count": len(grids),
        "grid_files": [str(g.resolve()) for g in grids],
        "note": "各セルの左上に焼き込まれた黄色い数字が実秒（動画開始からの経過秒）。エージェントはこの数字を直接読んで報告すること。",
    }
    manifest_path = out_dir / "manifest.json"
    manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")

    print(json.dumps({
        "manifest": str(manifest_path.resolve()),
        "grid_count": len(grids),
        "duration_sec": round(duration, 2),
    }, ensure_ascii=False))


if __name__ == "__main__":
    main()
