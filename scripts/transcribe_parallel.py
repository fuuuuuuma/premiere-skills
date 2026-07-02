#!/usr/bin/env python3
"""音声/動画 → N チャンク並列 faster-whisper 転写 → canonical 互換 segments.json（v6）

/srt の転写工程を高速化する機械工程ドライバ。LLM もエージェントも使わない
（トークン消費ゼロ）。分割は chunk_tools/setup_chunks.py（強い無音スナップ＋
オーバーラップ抽出）、転写は chunk_tools/whisper_chunk.py（canonical 一致の
転写パラメータ・中点所有判定）を N プロセスで並列実行し、
chunk_tools/merge_segments.py で境界欠落を復元しつつ単一 segments.json に統合する。

出力（--output-dir 省略時は <repo>/output/srt/<stem>/）:
  <stem>.segments.json   … canonical /srt 互換（--from-text にそのまま使える）
  <stem>.fulltext.txt    … 修正済み全文（Step 5 の LLM 改行入力）
  <stem>.wav             … 16k mono 変換キャッシュ

usage:
  transcribe_parallel.py <input.wav|.mov|.mp4> [--jobs 3] [--overlap 6.0]
"""
from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
import time
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
REPO = SCRIPT_DIR.parent
TOOLS = SCRIPT_DIR / "chunk_tools"
CANONICAL = SCRIPT_DIR / "whisper_to_srt.py"

sys.path.insert(0, str(TOOLS))
from merge_segments import merge_owned_segments  # noqa: E402


def probe_duration(path: str) -> float:
    out = subprocess.run(
        ["ffprobe", "-v", "error", "-show_entries", "format=duration",
         "-of", "default=noprint_wrappers=1:nokey=1", path],
        capture_output=True, text=True, timeout=120,
    ).stdout.strip()
    return float(out)


def main() -> None:
    ap = argparse.ArgumentParser(description="Nチャンク並列 Whisper 転写（v6）")
    ap.add_argument("input", help="入力 音声/動画 ファイル（WAV/MOV/MP4 等）")
    ap.add_argument("--jobs", type=int, default=3, help="並列本数（既定3。音声が短いと自動減少）")
    ap.add_argument("--overlap", type=float, default=6.0, help="チャンク間オーバーラップ秒")
    ap.add_argument("--keep-intermediate", action="store_true",
                    help="chunk 中間ファイル（wav/segments/overlap/fulltext）を残す")
    a = ap.parse_args()

    src = Path(a.input)
    if not src.exists():
        print(f"エラー: 入力が見つかりません: {src}")
        sys.exit(1)

    duration = probe_duration(str(src))
    # 1チャンク90秒未満になるほどの短尺は並列本数を自動で絞る（モデルロード代の方が高い）
    jobs = max(1, min(a.jobs, int(duration // 90) or 1))
    if jobs != a.jobs:
        print(f"音声 {duration:.0f}s に対し jobs を {a.jobs} → {jobs} に調整")

    t0 = time.time()

    # 1) 分割（16k mono 変換＋強い無音スナップ＋オーバーラップ抽出）
    r = subprocess.run(
        [sys.executable, str(TOOLS / "setup_chunks.py"), str(src), str(REPO),
         str(jobs), str(a.overlap)],
        capture_output=True, text=True, timeout=1800,
    )
    if r.returncode != 0:
        print(r.stdout)
        print(r.stderr, file=sys.stderr)
        sys.exit(1)
    m = re.search(r"MANIFEST:\s*(\S+)", r.stdout)
    manifest = json.loads(Path(m.group(1)).read_text())
    out_dir = Path(manifest["out_dir"])
    stem = manifest["stem"]
    if manifest.get("bounds_fallback"):
        print(f"⚠ 無音候補なしで等分点分割になった境界: {manifest['bounds_fallback']}（境界付近は目視推奨）")
    t_split = time.time()
    print(f"[1/3] 分割完了: {jobs}チャンク ({t_split - t0:.1f}s)")

    # 2) N プロセス並列転写
    procs = []
    for c in manifest["chunks"]:
        seg = out_dir / f"{stem}.chunk{c['idx']}.segments.json"
        ovl = out_dir / f"{stem}.chunk{c['idx']}.overlap.json"
        full = out_dir / f"{stem}.chunk{c['idx']}.fulltext.txt"
        p = subprocess.Popen(
            [sys.executable, str(TOOLS / "whisper_chunk.py"),
             "--audio", c["wav"], "--offset", str(c["offset"]),
             "--owned-start", str(c["owned_start"]), "--owned-end", str(c["owned_end"]),
             "--jobs", str(jobs),
             "--out", str(seg), "--overlap-out", str(ovl),
             "--fulltext", str(full), "--script", str(CANONICAL)],
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
        )
        procs.append((c["idx"], p, seg, ovl, full))

    failed = False
    for idx, p, *_ in procs:
        out, _ = p.communicate()
        tail = out.strip().splitlines()[-1] if out.strip() else ""
        print(f"  chunk{idx}: exit={p.returncode} {tail}")
        if p.returncode != 0:
            print(out)
            failed = True
    if failed:
        print("エラー: チャンク転写に失敗しました")
        sys.exit(1)
    t_whisper = time.time()
    print(f"[2/3] 並列転写完了 ({t_whisper - t_split:.1f}s)")

    # 3) 統合（境界欠落を overlap から復元）
    merged, recovered, unresolved = merge_owned_segments(
        [seg for _, _, seg, _, _ in procs],
        [ovl for _, _, _, ovl, _ in procs],
    )
    seg_path = out_dir / f"{stem}.segments.json"
    seg_path.write_text(json.dumps(merged, ensure_ascii=False, indent=2))
    fulltext_path = out_dir / f"{stem}.fulltext.txt"
    fulltext_path.write_text("".join(s.get("text", "") for s in merged))

    if not a.keep_intermediate:
        for c in manifest["chunks"]:
            Path(c["wav"]).unlink(missing_ok=True)
        for _, _, seg, ovl, full in procs:
            seg.unlink(missing_ok=True)
            ovl.unlink(missing_ok=True)
            full.unlink(missing_ok=True)

    print(f"[3/3] 統合完了: segments={len(merged)} 境界復元={len(recovered)}件")
    if unresolved:
        print(f"  ⚠ 未解消の空白区間（>1.5s・要目視）: {unresolved[:5]}")
    print(f"\nセグメント保存: {seg_path}")
    print(f"全文テキスト: {fulltext_path}")
    print(f"総所要: {time.time() - t0:.1f}s（音声 {duration:.0f}s / {jobs}並列）")


if __name__ == "__main__":
    main()
