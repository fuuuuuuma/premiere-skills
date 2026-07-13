#!/usr/bin/env python3
"""
音声 → Whisper → word timestamps → LLMセマンティック分割 → SRT (v2)

BudouX文字数分割を廃止し、LLMによるセマンティック分割に移行。
word-level timestampsで単語単位の正確なタイミングを実現。
XMLカット点をハード境界として使用。

使い方:
    # Phase 1: Whisper実行 → words JSON出力（LLM分割用）
    python whisper_to_srt.py input.wav
    python whisper_to_srt.py input.wav --xml timeline.xml

    # Phase 1b: 既存segments.jsonからwords JSON出力（Whisperスキップ）
    python whisper_to_srt.py --from-json segments.json
    python whisper_to_srt.py --from-json segments.json --xml timeline.xml

    # Phase 2: LLM分割結果からSRTを組み立て
    python whisper_to_srt.py --assemble grouped.json -o output.srt
    python whisper_to_srt.py --assemble grouped.json --xml timeline.xml -o output.srt
"""

from __future__ import annotations

import argparse
import bisect
import difflib
import json
import os
import platform
import re
import sys
import xml.etree.ElementTree as ET
from pathlib import Path

# ── 設定 ──────────────────────────────────────────────────────────────────────
FPS = 29.97

# ── タイミング調整（Premiere Pro向け） ──
PRE_ROLL_MS = 80       # 字幕を発話の少し前に表示（遅れより早い方が自然）
POST_ROLL_MS = 150     # 字幕を発話後も少し長く表示（読む時間確保）
MIN_GAP_MS = 80        # 連続字幕間の最小間隔（≒2フレーム@29.97fps: 切り替え感）
MIN_DURATION_MS = 800  # 字幕の最小表示時間（短すぎて読めないのを防止）

# gap埋め設定（テロップ欠落を完全排除）
MAX_GAP_FILL_MS = 1500  # これ以下のgapは前の字幕を延長して埋める

# カット点スナップ閾値
SNAP_THRESHOLD_S = 0.200  # ±200ms以内のカット点にスナップ

# 置換は長い文字列から先に適用される（部分一致の衝突を防ぐ）
# ここは意図的に空。チャンネル固有の固有名詞・言い間違い修正は
# config/corrections.local.json（gitignore対象）に書く。テンプレは
# config/corrections.example.json、記入は /srt-fast Step 0 のセットアップ対話で行う。
# 例: {"サンプル誤変換": "サンプル正規表記"} のような
# 「Whisperの誤認識」→「正規表記」のペアを追記していく。
CORRECTIONS: dict[str, str] = {
    # 口語→書き言葉（文字数削減。チャンネルによらず有効な汎用ルールのため既定で有効）
    "っていう": "という",
}

# 文脈ガード付き置換（str.replace の全置換だと「ワークフロー→ワークFlow」のような
# 複合語破壊が起きる場合に使う。前後がカタカナ/長音でない単独出現のみ置換する）。
# チャンネル固有の複合語保護ルールが必要な場合はここに直接追記する
# （例: `(re.compile(r"(?<!{_KATA})フロー(?!{_KATA})"), "Flow")`）。
_KATA = r"[ァ-ヺー]"
REGEX_CORRECTIONS: list[tuple[re.Pattern, str]] = []


def _load_local_corrections() -> None:
    """config/corrections.local.json があれば CORRECTIONS にマージする（無ければ何もしない）"""
    path = Path(__file__).resolve().parent.parent / "config" / "corrections.local.json"
    if not path.exists():
        return
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return
    if isinstance(data, dict):
        CORRECTIONS.update({
            str(k): str(v) for k, v in data.items() if not str(k).startswith("_")
        })


_load_local_corrections()

# フィラー削除パターン（正規表現）
# 否定先読み (?!...) で複合語の誤削除を防止
FILLER_PATTERNS: list[str] = [
    # ── 明確なフィラー（誤検出リスク低） ──
    r'(?<!まあ)まあ(?!まあ)',          # 「まあまあ」（程度表現）は保護
    # 「確かに」は削除しない: 単独の相槌「確かに」も文脈次第で意味を持つため、
    # 一律削除ではなく LLM が lines.txt 生成時に文脈判断で削る
    r'え[ーえっ]*と',                  # えっと、ええと、えーと
    # ── 複合語保護付きフィラー ──
    # 「こう」「ちょっと」は全削除禁止（副詞的用法・強調で意味を持つケースが多い）。
    # LLM の Step 3e で文脈判断で個別削除する
    # r'こう(?![いやしすなだでじゆ])',
    # r'ちょっと(?!した)',
    r'もう(?![少一すい終])',            # もう少し、もう一度、もうすぐ等は保護
    r'はい(?![るっり])',               # はいる等は保護
    # 「ね」は削除しない: 「〜ですね」「〜ですよね」等は共感・確認の意味を持ち、
    # 削除しすぎると口調が硬くなるため意図的に残す
]

# ──────────────────────────────────────────────────────────────────────────────


def snap_to_frame(seconds: float, fps: float = FPS) -> float:
    return round(seconds * fps) / fps


def to_srt_time(seconds: float, fps: float = FPS) -> str:
    # 総ミリ秒を整数で確定してから分解する。(t % 1) * 1000 の丸めでは
    # ms=1000 になる境界があり "00:00:01,1000" のような不正表記が出うる。
    t = snap_to_frame(seconds, fps)
    ms_total = max(0, int(round(t * 1000)))
    h, rem = divmod(ms_total, 3_600_000)
    m, rem = divmod(rem, 60_000)
    s, ms = divmod(rem, 1000)
    return f"{h:02d}:{m:02d}:{s:02d},{ms:03d}"


def _add_number_commas(text: str) -> str:
    """4桁以上の数字にカンマを挿入（1000→1,000、10000→10,000）。"""
    def fmt(m: re.Match) -> str:
        n = m.group(0)
        result = []
        for i, c in enumerate(reversed(n)):
            if i > 0 and i % 3 == 0:
                result.append(",")
            result.append(c)
        return "".join(reversed(result))
    return re.sub(r"\d{4,}(?!つ|本目|回目|年|月|日|番|号|階|枚|個)", fmt, text)


_KANJI_NUM_MAP = {"一": "1", "二": "2", "三": "3", "四": "4", "五": "5",
                  "六": "6", "七": "7", "八": "8", "九": "9"}
_KANJI_COUNTER_PAT = re.compile(
    r"([一二三四五六七八九])(番|個|枚|回|本|台|冊|件|倍|段|列|杯|着|曲|位|種|組)"
)


def _convert_kanji_numbers(text: str) -> str:
    """漢数字（一〜九）＋量詞を算用数字に変換。"""
    return _KANJI_COUNTER_PAT.sub(lambda m: _KANJI_NUM_MAP[m.group(1)] + m.group(2), text)


def apply_corrections(text: str) -> str:
    for wrong in sorted(CORRECTIONS, key=len, reverse=True):
        text = text.replace(wrong, CORRECTIONS[wrong])
    for pattern, repl in REGEX_CORRECTIONS:
        text = pattern.sub(repl, text)
    text = _convert_kanji_numbers(text)
    text = _add_number_commas(text)
    return text


def remove_fillers(text: str) -> str:
    for pattern in FILLER_PATTERNS:
        text = re.sub(pattern, "", text)
    text = text.replace("?", "").replace("？", "")
    text = re.sub(r"^[、,\s]+", "", text)
    text = re.sub(r"[、,]{2,}", "、", text)
    return re.sub(r"\s{2,}", " ", text).strip()


# ── Whisper ──────────────────────────────────────────────────────────────────


MLX_MODEL = os.environ.get("SRT_WHISPER_MODEL", "mlx-community/whisper-large-v3-turbo")


def run_whisper(audio_path: str, cpu_threads: int = 0) -> list[dict]:
    """Whisperを実行してセグメントリストを返す（word_timestamps含む）。

    2026-07-03 高速化: Apple Silicon では mlx-whisper (GPU・large-v3-turbo) を既定に。
    実測(M4 Max・180s実音声): faster-whisper large-v3 CPU 54.5s → mlx turbo 9.8s(5.6倍)。
    テキスト類似度0.91・一致語の開始時刻ずれ中央値80ms・内容欠落なしを確認済み
    （mlx large-v3 非turbo はフレーズ欠落があり不採用）。
    SRT_WHISPER_ENGINE=cpu で従来エンジン強制、SRT_WHISPER_MODEL でmlxモデル差し替え。
    mlx-whisper 未導入・実行失敗時は faster-whisper (CPU) に自動フォールバック。
    """
    if platform.system() == "Darwin" and os.environ.get("SRT_WHISPER_ENGINE", "mlx") != "cpu":
        try:
            return _run_whisper_mlx(audio_path, cpu_threads)
        except ImportError:
            print("mlx-whisper 未導入 → faster-whisper (CPU) で続行。"
                  "高速化: pip3 install --break-system-packages mlx-whisper")
        except Exception:
            import traceback
            traceback.print_exc()
            print("mlx-whisper 失敗 → faster-whisper (CPU) にフォールバック")
    return _run_whisper_faster(audio_path, cpu_threads)


def _run_whisper_mlx(audio_path: str, cpu_threads: int = 0) -> list[dict]:
    """mlx-whisper (Apple GPU) 経路。出力スキーマ・補正処理はCPU経路と完全一致。

    mlx は VAD を持たず、長い無音明けの短い発話を落とすことがある（実測で確認）。
    そのため転写後にタイムラインの未カバー区間だけを CPU+VAD で補完転写する
    （_rescue_gaps）。字幕用途のカバレッジを baseline 同等に保つための必須工程。
    """
    import mlx_whisper  # 未導入なら ImportError → フォールバック

    print(f"mlx-whisper {MLX_MODEL} (Apple GPU) 転写中: {audio_path}")
    result = mlx_whisper.transcribe(
        audio_path,
        path_or_hf_repo=MLX_MODEL,
        language="ja",
        word_timestamps=True,
        temperature=0.0,
        condition_on_previous_text=False,
        no_speech_threshold=0.6,
        hallucination_silence_threshold=2.0,
    )

    seg_list = []
    for seg in result["segments"]:
        raw_text = apply_corrections(seg["text"].strip())
        clean_text = remove_fillers(raw_text)
        if not clean_text:
            continue

        words = []
        for w in seg.get("words", []):
            word_raw = apply_corrections(w["word"].strip())
            word_clean = remove_fillers(word_raw)
            if word_clean:
                words.append({
                    "word": word_clean,
                    "start": w["start"],
                    "end": w["end"],
                })

        seg_list.append({
            "start": seg["start"],
            "end": seg["end"],
            "text": clean_text,
            "words": words,
        })

    seg_list = _rescue_gaps(audio_path, seg_list, cpu_threads)

    print(f"Whisperセグメント数: {len(seg_list)}")
    return seg_list


GAP_RESCUE_MIN_S = 1.5   # merge_segments.py の空白警告閾値と揃える
GAP_RESCUE_MARGIN_S = 0.25


GAP_RESCUE_SPACER_S = 2.0  # 連結時の無音スペーサ（VADに区間境界を跨がせない）
GAP_RESCUE_SR = 16000


def _rescue_gaps(audio_path: str, seg_list: list[dict], cpu_threads: int = 0) -> list[dict]:
    """mlx転写の未カバー区間（無音扱いされた区間）だけを CPU+VAD で補完転写する。

    大半の空白は真の無音だが、無音明けの短い接続句が落ちるケースがある（実測）。
    Whisperは音声長に関わらず30秒窓単位で推論するため、区間を1本ずつ転写すると
    区間数×窓コストで長尺動画が破綻する。→ 全区間を無音スペーサ入りで1本の
    音声に連結し、CPU+VADで**1回だけ**転写してから元のタイムラインへ写像する。
    """
    import subprocess
    import tempfile
    import wave

    try:
        dur = float(subprocess.run(
            ["ffprobe", "-v", "error", "-show_entries", "format=duration",
             "-of", "default=noprint_wrappers=1:nokey=1", audio_path],
            capture_output=True, text=True, timeout=60,
        ).stdout.strip())
    except Exception:
        dur = float(seg_list[-1]["end"]) if seg_list else 0.0

    gaps = []
    prev = 0.0
    for s in seg_list:
        if s["start"] - prev >= GAP_RESCUE_MIN_S:
            gaps.append((float(prev), float(s["start"])))
        prev = max(prev, s["end"])
    if dur - prev >= GAP_RESCUE_MIN_S:
        gaps.append((float(prev), dur))
    if not gaps:
        return seg_list

    print(f"gap補完: {len(gaps)}区間を連結してCPU Whisper(VAD付き)で再転写 "
          f"{[(round(float(a), 1), round(float(b), 1)) for a, b in gaps]}")

    # 各gap（±マージン）を s16le/16k/mono で切り出し、無音スペーサを挟んで連結
    spacer = b"\x00\x00" * int(GAP_RESCUE_SPACER_S * GAP_RESCUE_SR)
    slices = []   # (concat開始秒, 元音声開始秒, スライス長秒)
    pcm_parts = []
    concat_pos = 0.0
    for g0, g1 in gaps:
        s0 = max(0.0, g0 - GAP_RESCUE_MARGIN_S)
        length = g1 - s0 + GAP_RESCUE_MARGIN_S
        r = subprocess.run(
            ["ffmpeg", "-v", "error", "-ss", str(s0), "-t", str(length),
             "-i", audio_path, "-f", "s16le", "-acodec", "pcm_s16le",
             "-ac", "1", "-ar", str(GAP_RESCUE_SR), "-"],
            capture_output=True, timeout=120, check=True,
        )
        pcm = r.stdout
        slices.append((concat_pos, s0, len(pcm) / 2 / GAP_RESCUE_SR))
        pcm_parts.append(pcm)
        pcm_parts.append(spacer)
        concat_pos += len(pcm) / 2 / GAP_RESCUE_SR + GAP_RESCUE_SPACER_S

    tmp = tempfile.NamedTemporaryFile(suffix=".wav", delete=False).name
    try:
        with wave.open(tmp, "wb") as wf:
            wf.setnchannels(1)
            wf.setsampwidth(2)
            wf.setframerate(GAP_RESCUE_SR)
            wf.writeframes(b"".join(pcm_parts))

        model = _fw_load_model(cpu_threads)
        # 30秒窓の中で複数スライスが1セグメントに融合することがあるため、
        # セグメント単位ではなく語タイムスタンプ単位でスライスへ写像し、
        # スライスごとにセグメントを組み直す。
        words_by_slice = {}
        for seg in _fw_transcribe(model, tmp):
            for w in seg["words"]:
                mid_local = (w["start"] + w["end"]) / 2
                for k, (c0, s0, slen) in enumerate(slices):
                    if c0 <= mid_local < c0 + slen:
                        g0, g1 = gaps[k]
                        shift = s0 - c0
                        if g0 <= mid_local + shift < g1:  # マージン由来の重複は捨てる
                            words_by_slice.setdefault(k, []).append({
                                "word": w["word"],
                                "start": w["start"] + shift,
                                "end": w["end"] + shift,
                            })
                        break
        rescued = []
        for k, words in sorted(words_by_slice.items()):
            rescued.append({
                "start": words[0]["start"],
                "end": words[-1]["end"],
                "text": "".join(w["word"] for w in words),
                "words": words,
            })
    finally:
        try:
            os.unlink(tmp)
        except OSError:
            pass

    if rescued:
        print(f"gap補完: {len(rescued)}セグメント回復 "
              f"{[s['text'][:15] for s in rescued]}")
        seg_list = sorted(seg_list + rescued, key=lambda s: s["start"])
    return seg_list


def _fw_load_model(cpu_threads: int = 0):
    try:
        from faster_whisper import WhisperModel
    except ImportError:
        print("エラー: faster-whisper が見つかりません。")
        print("インストール: pip install faster-whisper")
        sys.exit(1)

    if platform.system() == "Darwin":
        device, compute_type = "cpu", "int8"
    else:
        device, compute_type = "auto", "auto"

    print(f"Whisper large-v3 を読み込み中... (device={device})")
    return WhisperModel("large-v3", device=device, compute_type=compute_type,
                        cpu_threads=cpu_threads)


def _run_whisper_faster(audio_path: str, cpu_threads: int = 0) -> list[dict]:
    """faster-whisper (CPU) 経路 — 従来実装そのまま。"""
    model = _fw_load_model(cpu_threads)
    print(f"文字起こし中: {audio_path}")
    seg_list = _fw_transcribe(model, audio_path)
    print(f"Whisperセグメント数: {len(seg_list)}")
    return seg_list


def _fw_transcribe(model, audio_path: str) -> list[dict]:
    # 高速化: beam_size=1 (greedy), best_of=1, VAD しきい値緩め
    # large-v3 を維持しつつ、推論コストを 1/5 に削減（約2〜3倍高速）
    segments, _ = model.transcribe(
        audio_path,
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

    seg_list = []
    for seg in segments:
        raw_text = apply_corrections(seg.text.strip())
        clean_text = remove_fillers(raw_text)
        if not clean_text:
            continue

        words = []
        if seg.words:
            for w in seg.words:
                word_raw = apply_corrections(w.word.strip())
                word_clean = remove_fillers(word_raw)
                if word_clean:
                    words.append({
                        "word": word_clean,
                        "start": w.start,
                        "end": w.end,
                    })

        seg_list.append({
            "start": seg.start,
            "end": seg.end,
            "text": clean_text,
            "words": words,
        })

    return seg_list


# ── XML解析 ──────────────────────────────────────────────────────────────────


def parse_xml_cut_points(xml_path: str) -> tuple[list[float], float]:
    """FCP XMLからV1トラックのカット点を抽出する。"""
    tree = ET.parse(xml_path)
    root = tree.getroot()

    best_seq = None
    best_v1_clips = 0
    best_fps = FPS

    for seq in root.iter("sequence"):
        rate_el = seq.find(".//rate")
        if rate_el is None:
            continue
        tb_el = rate_el.find("timebase")
        ntsc_el = rate_el.find("ntsc")
        if tb_el is None:
            continue
        timebase = int(tb_el.text)
        ntsc = ntsc_el is not None and ntsc_el.text.upper() == "TRUE"
        seq_fps = timebase * 1000 / 1001 if ntsc else float(timebase)

        v_tracks = seq.findall(".//media/video/track")
        if not v_tracks:
            v_tracks = seq.findall(".//video/track")
        v1_clip_count = len(v_tracks[0].findall("clipitem")) if v_tracks else 0

        if v1_clip_count > best_v1_clips:
            best_v1_clips = v1_clip_count
            best_seq = seq
            best_fps = seq_fps

    if best_seq is None:
        print("警告: XMLにsequenceが見つかりません")
        return [], FPS

    video_tracks = best_seq.findall(".//media/video/track")
    if not video_tracks:
        video_tracks = best_seq.findall(".//video/track")

    cut_points: set[float] = set()
    if video_tracks:
        v1 = video_tracks[0]
        for clip in v1.findall("clipitem"):
            start_el = clip.find("start")
            end_el = clip.find("end")
            if start_el is not None and start_el.text and start_el.text != "-1":
                cut_points.add(int(start_el.text) / best_fps)
            if end_el is not None and end_el.text and end_el.text != "-1":
                cut_points.add(int(end_el.text) / best_fps)

    sorted_cuts = sorted(cut_points)
    print(f"XMLカット点: {len(sorted_cuts)}個 (fps={best_fps:.4f})")
    return sorted_cuts, best_fps


def estimate_offset_from_xml(xml_root: ET.Element, fps: float) -> float:
    """XMLのタイムライン構造からWAV→タイムラインのオフセットを算出する。"""
    best_seq = None
    best_clips = 0
    for seq in xml_root.iter("sequence"):
        v_tracks = seq.findall(".//media/video/track")
        if not v_tracks:
            v_tracks = seq.findall(".//video/track")
        n = len(v_tracks[0].findall("clipitem")) if v_tracks else 0
        if n > best_clips:
            best_clips = n
            best_seq = seq

    if best_seq is None:
        return 0.0

    audio_tracks = best_seq.findall(".//media/audio/track")
    if not audio_tracks:
        return 0.0

    a1 = audio_tracks[0]
    clip_items = a1.findall("clipitem")
    first_start = None
    for clip in clip_items:
        s = clip.findtext("start")
        if s is not None and s != "-1":
            first_start = int(s) / fps
            break

    if first_start is None:
        return 0.0

    # WAV がカット済み（タイムライン書き出し）の場合、最初の音声クリップは
    # start=0 から始まり、WAV time = timeline time となる → offset 0。
    # WAV がソース録画の場合、タイムライン頭に無音区間があれば first_start > 0
    # でその秒数だけ後ろにずれる。
    # 旧実装は「最初の start>0 の clip」を探していたが、隣接する 2 番目以降の
    # クリップを誤って拾い、誤オフセットを生じるケースがあった。
    if first_start > 0:
        print(f"オフセット算出: +{first_start:.3f}秒 "
              f"(タイムライン上の最初のオーディオ開始位置)")
    else:
        print(f"オフセット算出: +0.000秒 (WAVはカット済みタイムライン出力)")
    return first_start


def snap_to_cut_points(
    entries: list[tuple[float, float, str]],
    cut_points: list[float],
    threshold: float = SNAP_THRESHOLD_S,
) -> list[tuple[float, float, str]]:
    """SRTエントリのstart/endを±threshold以内の最近カット点にスナップする。"""
    if not cut_points:
        return entries

    def find_nearest(t: float) -> float | None:
        idx = bisect.bisect_left(cut_points, t)
        best = None
        best_dist = threshold + 1
        for i in (idx - 1, idx):
            if 0 <= i < len(cut_points):
                dist = abs(cut_points[i] - t)
                if dist < best_dist:
                    best_dist = dist
                    best = cut_points[i]
        return best if best_dist <= threshold else None

    result: list[tuple[float, float, str]] = []
    snapped_count = 0
    for s, e, t in entries:
        new_s = find_nearest(s)
        new_e = find_nearest(e)
        if new_s is not None:
            s = new_s
            snapped_count += 1
        if new_e is not None:
            e = new_e
        if e <= s:
            e = s + 1.0 / FPS
        result.append((s, e, t))

    print(f"カット点スナップ: {snapped_count}/{len(entries)}エントリ")
    return result


# ── タイミング調整 ────────────────────────────────────────────────────────────


def refine_timing(
    entries: list[tuple[float, float, str]],
    fps: float = FPS,
) -> list[tuple[float, float, str]]:
    """セグメント間のgapにpre-roll/post-rollを適用する。"""
    if not entries:
        return entries

    pre_roll = PRE_ROLL_MS / 1000.0
    post_roll = POST_ROLL_MS / 1000.0
    min_dur = MIN_DURATION_MS / 1000.0
    max_gap_fill = MAX_GAP_FILL_MS / 1000.0
    gap_threshold = 0.05

    result = list(entries)

    for i in range(len(result)):
        s, e, t = result[i]

        if i > 0:
            prev_end = result[i - 1][1]
            gap_before = s - prev_end
            if gap_before > gap_threshold:
                s = s - min(pre_roll, gap_before * 0.4)
                s = max(0.0, s)

        if i + 1 < len(result):
            next_start = result[i + 1][0]
            gap_after = next_start - e
            if gap_after > gap_threshold:
                if gap_after <= max_gap_fill:
                    e = next_start
                else:
                    e = e + min(post_roll, gap_after * 0.4)
        else:
            e = e + post_roll

        if e - s < min_dur:
            desired = s + min_dur
            if i + 1 < len(result):
                desired = min(desired, result[i + 1][0])
            e = max(e, desired)

        result[i] = (s, e, t)

    # フレームスナップ + 最小1フレーム保証
    frame_dur = 1.0 / fps
    snapped: list[tuple[float, float, str]] = []
    for s, e, t in result:
        ss = snap_to_frame(s, fps)
        se = snap_to_frame(e, fps)
        if se <= ss:
            se = ss + snap_to_frame(frame_dur, fps)
        snapped.append((ss, se, t))

    # フレームスナップ後の重複解消
    for i in range(len(snapped) - 1):
        s_cur, e_cur, t_cur = snapped[i]
        s_next, _, _ = snapped[i + 1]
        if e_cur > s_next:
            snapped[i] = (s_cur, s_next, t_cur)

    # 0ms表示エントリを除去
    snapped = [(s, e, t) for s, e, t in snapped if e > s]

    return snapped


# ── Phase 1: Words JSON出力（LLM分割用） ─────────────────────────────────────


def output_words_json(
    seg_list: list[dict],
    output_path: str,
    xml_path: str | None = None,
) -> None:
    """Whisperセグメントから単語リストJSONを出力する（LLM分割用）。

    単語ごとのtimestampをフラット化し、XMLカット点がある場合は
    各単語に cut_before フラグを付与する。
    """
    # XML解析
    cut_points: list[float] = []
    offset = 0.0
    fps = FPS
    if xml_path:
        tree = ET.parse(xml_path)
        xml_root = tree.getroot()
        cut_points, xml_fps = parse_xml_cut_points(xml_path)
        if cut_points:
            offset = estimate_offset_from_xml(xml_root, xml_fps)
            fps = xml_fps

    # 全単語をフラット化（オフセット適用済み）
    words: list[dict] = []
    word_id = 0
    for seg in seg_list:
        seg_words = seg.get("words", [])
        if not seg_words:
            # word timestampがない場合はセグメント全体を1単語として扱う
            text = apply_corrections(seg["text"])
            if text:
                words.append({
                    "id": word_id,
                    "word": text,
                    "start": round(seg["start"] + offset, 3),
                    "end": round(seg["end"] + offset, 3),
                })
                word_id += 1
            continue

        for w in seg_words:
            word_text = apply_corrections(w["word"])
            if word_text:
                words.append({
                    "id": word_id,
                    "word": word_text,
                    "start": round(w["start"] + offset, 3),
                    "end": round(w["end"] + offset, 3),
                })
                word_id += 1

    # カット点マーキング: 単語間にカット点がある場合 cut_before=true を付与
    if cut_points and words:
        # 音声範囲内のカット点のみ使用
        audio_start = words[0]["start"] - 1.0
        audio_end = words[-1]["end"] + 1.0
        relevant_cuts = [cp for cp in cut_points
                         if audio_start <= cp <= audio_end]

        cut_idx = 0
        for i, word in enumerate(words):
            if i == 0:
                continue
            prev_end = words[i - 1]["end"]
            word_start = word["start"]

            # prev_end以降で最初のカット点を探す
            while cut_idx < len(relevant_cuts) and relevant_cuts[cut_idx] < prev_end - 0.05:
                cut_idx += 1

            if cut_idx < len(relevant_cuts) and relevant_cuts[cut_idx] <= word_start + 0.1:
                word["cut_before"] = True

    # 出力
    output = {
        "metadata": {
            "fps": fps,
            "offset": offset,
            "total_words": len(words),
            "xml": xml_path or None,
        },
        "cut_points": [round(cp, 3) for cp in cut_points] if cut_points else [],
        "words": words,
    }

    with open(output_path, "w", encoding="utf-8") as f:
        json.dump(output, f, ensure_ascii=False, indent=2)

    print(f"\n単語JSON出力: {output_path}")
    print(f"  総単語数: {len(words)}")
    if cut_points:
        cut_marked = sum(1 for w in words if w.get("cut_before"))
        print(f"  カット点マーク: {cut_marked}箇所")
        print(f"  オフセット: {offset:+.3f}秒")
    print(f"  fps: {fps}")


# ── Phase 1d: 機械的前処理（smart-group） ────────────────────────────────────


def smart_group(
    candidates_path: str,
    output_path: str,
) -> None:
    """candidates.json → 機械的に修復した中間 JSON を出力。

    Agent1 の仕事をスクリプト化。LLM 判断不要の修復のみ行う:
    - 2文字以下断片を前後に結合
    - 文頭禁止パターンを前エントリに結合
    - 25字超を自然な位置で分割
    - Whisper 誤認識は apply_corrections() で既に適用済み
    """
    with open(candidates_path, encoding="utf-8") as f:
        cands = json.load(f)

    # --- 禁止文頭パターン ---
    _FORBIDDEN_RE = re.compile(
        r'^('
        r'[をにがはのとやもねよぞ][^0-9A-Za-z]|'
        r'ます[。]?$|まし[た]|ません|でした|きます|きました|'
        r'ない[。]?$|なく[て]?|なっ[た]|'
        r'ている|ていく|てくる|てみる|ておく|てしまう|てほしい|てくれ|てあげ|'
        r'てもらう|ていただ|ており|ておき|ていた|ていま|てくださ|'
        r'という[。]?$|として[。]?$|について|によって|にとって|'
        r'ため[にの]|とき[にの]|はず[。]?|こと[にをがはで]|もの[をがはで]|'
        r'[ァ-ヺー]{1,3}[^ァ-ヺー]'
        r')'
    )

    def _is_fragment(text: str) -> bool:
        if len(text) <= 2:
            return True
        if _FORBIDDEN_RE.match(text):
            return True
        return False

    # Pass 1-3: 繰り返しマージ + 分割
    entries = [{"text": c["text"], "start": c["start"], "end": c["end"]} for c in cands if c["text"].strip()]

    for _ in range(3):
        # マージ
        merged: list[dict] = []
        for e in entries:
            t = e["text"].strip()
            if not t:
                continue
            if merged and _is_fragment(t):
                merged[-1]["text"] += t
                merged[-1]["end"] = e["end"]
            else:
                merged.append({"text": t, "start": e["start"], "end": e["end"]})

        # 分割（25字超）
        result: list[dict] = []
        for e in merged:
            if len(e["text"]) <= 25:
                result.append(e)
                continue
            # 分割点を探す（後半が禁止文頭にならないように）
            t = e["text"]
            n = len(t)
            best = None
            best_score = 999.0
            for pat in [
                r'(?:ですね|ますね|ですよ|ますよ|ですが|ますが|ですけど|ですけども|んですけど|んですけども|ませんでした|いただきます|してください|ございます|おりまして|ておりまして)',
                r'(?:ので|けど|けども|から|なので|だから|すると|すれば)',
                r'(?:っている|ている|ていく|ておく|てくる|てくれ|してみ|できる|なった|しまし|しました|されて|させて)',
                r'(?:って|たり|とか|ても|のに)',
                r'(?:を|に|が|は|で|と|も)',
            ]:
                for m in re.finditer(pat, t):
                    pos = m.end()
                    if 6 <= pos <= n - 4:
                        remaining = t[pos:]
                        if not _is_fragment(remaining):
                            score = abs(pos - n * 0.5) / n * 10
                            if score < best_score:
                                best = pos
                                best_score = score
                if best is not None:
                    break
            if best and 4 < best < n - 3:
                dur = e["end"] - e["start"]
                ratio = best / n
                mid = round(e["start"] + dur * ratio, 3)
                result.append({"text": t[:best], "start": e["start"], "end": mid})
                result.append({"text": t[best:], "start": mid, "end": e["end"]})
            else:
                result.append(e)
        entries = result

    # 最終マージ（短すぎるもの）
    final: list[dict] = []
    for e in entries:
        t = e["text"].strip()
        if not t:
            continue
        if len(t) < 4 and final:
            final[-1]["text"] += t
            final[-1]["end"] = e["end"]
        elif final and _is_fragment(t):
            final[-1]["text"] += t
            final[-1]["end"] = e["end"]
        else:
            final.append({"text": t, "start": e["start"], "end": e["end"]})

    # 出力
    with open(output_path, "w", encoding="utf-8") as f:
        json.dump(final, f, ensure_ascii=False, indent=2)

    lens = [len(e["text"]) for e in final]
    over25 = sum(1 for l in lens if l > 25)
    under4 = sum(1 for l in lens if l < 4)
    print(f"\nスマートグループ出力: {output_path}")
    print(f"  エントリ数: {len(final)}")
    print(f"  平均文字数: {sum(lens)/len(lens):.1f}")
    print(f"  25字超: {over25}件")
    print(f"  4字未満: {under4}件")


# ── Phase 1c: 初期候補分割（機械） ────────────────────────────────────────────


def emit_candidates(
    segments_path: str,
    output_path: str,
    xml_path: str | None = None,
    pause_threshold: float = 0.30,
) -> None:
    """
    Step 3c の初期候補分割。
    Whisper segment 境界 + XML カット点 + word gap > pause_threshold を OR して、
    過剰に細かく切った candidates.json を出力する。

    LLM は Step 3d-3f でこれを Read し、違和感を検出・修正してから grouped.json を Write する。

    **重要**: このスクリプトは最終判断を行わない。切れすぎているのは想定内。
    意味境界判断は LLM (Claude) が行う。
    """
    with open(segments_path, encoding="utf-8") as f:
        seg_list = json.load(f)

    # XML カット点 + オフセット
    cut_points: list[float] = []
    offset = 0.0
    if xml_path:
        tree = ET.parse(xml_path)
        xml_root = tree.getroot()
        cut_points, xml_fps = parse_xml_cut_points(xml_path)
        if cut_points:
            offset = estimate_offset_from_xml(xml_root, xml_fps)

    # セグメント情報 + 各セグメントの単語範囲をフラットに構築
    # 各セグメントの区切りは candidate boundary の候補になる
    flat_words: list[dict] = []
    segment_breaks: set[int] = set()  # word インデックス境界
    for seg in seg_list:
        seg_words = seg.get("words", [])
        if not seg_words:
            continue
        # このセグメント開始は前のセグメントとの境界
        if flat_words:
            segment_breaks.add(len(flat_words))
        for w in seg_words:
            word_text = apply_corrections(w["word"].strip())
            word_text = remove_fillers(word_text)
            if not word_text:
                continue
            flat_words.append({
                "word": word_text,
                "start": round(w["start"] + offset, 3),
                "end": round(w["end"] + offset, 3),
            })

    if not flat_words:
        print("エラー: 単語が見つかりません")
        sys.exit(1)

    # 候補境界: segment breaks + cut points + long gaps
    # 境界は「その位置の前で切る」を意味する (word index)
    boundaries: set[int] = set()
    boundaries.add(0)
    boundaries.add(len(flat_words))
    boundaries |= segment_breaks

    # カット点による境界
    if cut_points:
        audio_start = flat_words[0]["start"] - 1.0
        audio_end = flat_words[-1]["end"] + 1.0
        relevant_cuts = [cp for cp in cut_points if audio_start <= cp <= audio_end]
        cut_idx = 0
        for i in range(1, len(flat_words)):
            prev_end = flat_words[i - 1]["end"]
            word_start = flat_words[i]["start"]
            while cut_idx < len(relevant_cuts) and relevant_cuts[cut_idx] < prev_end - 0.05:
                cut_idx += 1
            if cut_idx < len(relevant_cuts) and relevant_cuts[cut_idx] <= word_start + 0.1:
                boundaries.add(i)

    # word gap による境界
    for i in range(1, len(flat_words)):
        gap = flat_words[i]["start"] - flat_words[i - 1]["end"]
        if gap > pause_threshold:
            boundaries.add(i)

    # 境界でグループ化
    sorted_bounds = sorted(boundaries)
    candidates: list[dict] = []
    for i in range(len(sorted_bounds) - 1):
        start_idx = sorted_bounds[i]
        end_idx = sorted_bounds[i + 1]
        if end_idx <= start_idx:
            continue
        group_words = flat_words[start_idx:end_idx]
        text = "".join(w["word"] for w in group_words)
        if not text:
            continue
        candidates.append({
            "text": text,
            "start": group_words[0]["start"],
            "end": group_words[-1]["end"],
        })

    # 出力
    with open(output_path, "w", encoding="utf-8") as f:
        json.dump(candidates, f, ensure_ascii=False, indent=2)

    lens = [len(c["text"]) for c in candidates]
    print(f"\n候補JSON出力: {output_path}")
    print(f"  候補エントリ数: {len(candidates)}")
    if lens:
        print(f"  平均文字数: {sum(lens)/len(lens):.1f}")
        print(f"  最大: {max(lens)}, 最小: {min(lens)}")
    if cut_points:
        print(f"  XMLカット点: {len(cut_points)}個")
        print(f"  オフセット: {offset:+.3f}秒")
    print(f"\n  ※ これは候補抽出です。LLMが Step 3d-3f でこれを Read し、")
    print(f"     違和感を検出・修正してから grouped.json を Write してください。")


# ── 品質チェック（QA） ────────────────────────────────────────────────────────

# 文頭に来てはいけないパターン（QA 用・カタカナ破片ルールは誤検出が多いため除外）
QA_FORBIDDEN_HEAD_RE = re.compile(
    r'^('
    r'[をにがはのとやもねよぞ][^0-9A-Za-zぁ-ゖ一-龥ァ-ヺー]|'
    r'ます$|まし[た]|ません|でした|きます|きました|'
    r'ない$|なくて|なった|'
    r'ている|ていく|てくる|てみる|ておく|てしまう|てほしい|てくれ|てあげ|'
    r'てもらう|ていただ|ており|ておき|ていた|ていま|てくださ|'
    r'という$|として$|について|によって|にとって|'
    r'ため[にの]|とき[にの]|こと[にをがはで]|もの[をがはで]'
    r')'
)


def qa_report(
    entries: list[tuple[float, float, str]],
    srt_path: str | None = None,
) -> int:
    """SRT の品質チェックを行い、レポートを標準出力する。

    skill 側（srt.md Step 7）はこの出力をそのまま報告に転記する。
    LLM が SRT を Read し直して集計する必要はない（トークン節約）。
    戻り値は要修正件数（25字超 + 文頭NG + 0ms表示 + 重複）。
    """
    texts = [t for _, _, t in entries]
    lens = [len(t) for t in texts]
    durs = [e - s for s, e, _ in entries]
    over25 = [(i + 1, t) for i, t in enumerate(texts) if len(t) > 25]
    under4 = [(i + 1, t) for i, t in enumerate(texts) if len(t) < 4]
    head_ng = [(i + 1, t) for i, t in enumerate(texts) if QA_FORBIDDEN_HEAD_RE.match(t)]
    zero_dur = sum(1 for d in durs if d <= 0)
    overlaps = 0
    gaps500 = 0
    max_gap = 0.0
    max_gap_at = 0.0
    for i in range(len(entries) - 1):
        g = entries[i + 1][0] - entries[i][1]
        if g < -0.001:
            overlaps += 1
        if g > 0.5:
            gaps500 += 1
            if g > max_gap:
                max_gap = g
                max_gap_at = entries[i][1]

    print("\n── SRT 品質チェック（QA） ──")
    print(f"  総エントリ: {len(entries)}")
    if lens and durs:
        print(f"  平均文字数: {sum(lens) / len(lens):.1f} / 中央値: {sorted(lens)[len(lens) // 2]}")
        print(f"  表示時間平均: {sum(durs) / len(durs):.2f}s / 文字/秒: {sum(lens) / max(sum(durs), 0.01):.1f}")
    pct25 = len(over25) / max(len(entries), 1) * 100
    print(f"  25字超: {len(over25)}件 ({pct25:.1f}% / 目標1%未満)")
    for idx, t in over25[:10]:
        print(f"    #{idx}: {t}")
    print(f"  4字未満: {len(under4)}件")
    print(f"  文頭NG候補（要確認）: {len(head_ng)}件")
    for idx, t in head_ng[:10]:
        print(f"    #{idx}: {t}")
    print(f"  0ms表示: {zero_dur}件 / 時間重複: {overlaps}件")
    tail = f"（最大 {max_gap:.1f}s @ {max_gap_at:.1f}s 付近）" if gaps500 else ""
    print(f"  0.5s超ギャップ: {gaps500}件{tail}")
    if srt_path:
        raw = open(srt_path, "rb").read()
        bom = "OK" if raw[:3] == b"\xef\xbb\xbf" else "NG"
        crlf = "OK" if b"\r\n" in raw else "NG"
        print(f"  UTF-8 BOM: {bom} / CRLF: {crlf}")
    issues = len(over25) + len(head_ng) + zero_dur + overlaps
    if issues == 0:
        print("  ✅ 要修正なし")
    else:
        print(f"  ⚠ 要修正候補 {issues} 件（25字超・文頭NGは lines.txt を直して再実行）")
    # SRT_QA_JSON=1 のとき機械可読な1行を末尾に追加出力（/srt-fast の自動修復ループ用。
    # 既定では出さないので /srt のレポート転記には影響しない）
    if os.environ.get("SRT_QA_JSON"):
        print("QA_JSON: " + json.dumps({
            "total": len(entries),
            "avg_chars": round(sum(lens) / len(lens), 2) if lens else 0,
            "over25": len(over25), "under4": len(under4), "head_ng": len(head_ng),
            "zero_dur": zero_dur, "overlaps": overlaps, "gaps500": gaps500,
            "max_gap": round(max_gap, 2), "max_gap_at": round(max_gap_at, 1),
            "over25_items": [[i, t] for i, t in over25[:30]],
            "head_ng_items": [[i, t] for i, t in head_ng[:30]],
        }, ensure_ascii=False))
    return issues


def _parse_srt_file(path: str) -> list[tuple[float, float, str]]:
    """既存 SRT を (start, end, text) のリストに読み込む（--qa 用）。"""
    with open(path, encoding="utf-8-sig") as f:
        content = f.read()
    entries: list[tuple[float, float, str]] = []

    def _pt(t: str) -> float:
        t = t.strip().replace(",", ".")
        h, m, s = t.split(":")
        return int(h) * 3600 + int(m) * 60 + float(s)

    for block in content.strip().split("\n\n"):
        lines_b = [ln for ln in block.strip().splitlines() if ln.strip()]
        if len(lines_b) >= 3 and "-->" in lines_b[1]:
            a, b = lines_b[1].split("-->")
            entries.append((_pt(a), _pt(b), "\n".join(lines_b[2:])))
    return entries


# ── Phase 2b: --from-text (改行テキスト → SRT 直接生成・v6) ───────────────────


def _normalize_for_match(s: str) -> str:
    """マッチング用に改行/空白/句読点/記号を除去。"""
    return re.sub(r"[\s、。,\.!\?！？「」『』()（）\[\]【】・…ー~〜]+", "", s)


def assemble_from_text(
    segments_path: str,
    text_path: str,
    output_path: str,
    fps: float = FPS,
    xml_path: str | None = None,
) -> None:
    """改行テキスト + segments.json から SRT を直接生成する（v6 のメイン経路）。

    各行を 1 テロップとし、行連結文字列 ↔ Whisper 単語連結文字列を
    difflib.SequenceMatcher で全体アライメントして時刻を割り当てる。

    v5 の局所アンカー方式（行頭6字を累積位置±60で前方検索）は、繰り返し語への
    吸着・辞書非同期の累積文字数ズレ・逆戻り誤マッチで4種のバグを起こした
    （memory/feedback_srt_grouping_rules.md 参照）。全体最適のアライメントは
    前方ラチェットが原理的に起きず、lines.txt 側の固有名詞修正が CORRECTIONS
    辞書に未登録でも局所の不一致として吸収される（/srt-fast assemble で実証済み・
    /srt との時刻差 median ±0.00s）。
    """
    with open(segments_path, encoding="utf-8") as f:
        seg_list = json.load(f)
    with open(text_path, encoding="utf-8") as f:
        raw_text = f.read()

    # XML オフセット + カット点
    cut_points: list[float] = []
    offset = 0.0
    if xml_path:
        tree = ET.parse(xml_path)
        xml_root = tree.getroot()
        cut_points, xml_fps = parse_xml_cut_points(xml_path)
        if cut_points:
            offset = estimate_offset_from_xml(xml_root, xml_fps)
            fps = xml_fps

    # Whisper 単語列（apply_corrections 適用済み）を flatten
    # NOTE: Whisper の word は細切れ（例: 複数文字にまたがる辞書エントリの場合、
    # 単語単位では部分一致しない）なので apply_corrections を単語単位で掛けても発動しない。
    # segment 単位で raw を連結して apply_corrections を試し、置換が発生した
    # segment は文字数が変わるので、seg 全体を seg.start〜seg.end で線形配分する。
    words: list[dict] = []
    for seg in seg_list:
        seg_words = seg.get("words", [])
        if not seg_words:
            text = apply_corrections(seg["text"])
            if text:
                words.append({
                    "word": text,
                    "start": seg["start"] + offset,
                    "end": seg["end"] + offset,
                })
            continue

        raw_concat = "".join(w["word"] for w in seg_words)
        corrected = apply_corrections(raw_concat)

        if raw_concat == corrected:
            # 置換なし: 単語レベルの精密な時刻を維持
            for w in seg_words:
                word_text = w["word"]
                if word_text:
                    words.append({
                        "word": word_text,
                        "start": w["start"] + offset,
                        "end": w["end"] + offset,
                    })
        else:
            # 置換あり: seg 全体を文字単位で線形配分（累積ズレを seg 内に閉じ込める）
            seg_start = seg_words[0]["start"] + offset
            seg_end = seg_words[-1]["end"] + offset
            seg_dur = max(seg_end - seg_start, 0.01)
            n_full = len(corrected)
            if n_full == 0:
                continue
            for i, c in enumerate(corrected):
                t_start = seg_start + seg_dur * i / n_full
                t_end = seg_start + seg_dur * (i + 1) / n_full
                words.append({
                    "word": c,
                    "start": t_start,
                    "end": t_end,
                })

    if not words:
        print("エラー: Whisper 単語列が空です")
        sys.exit(1)

    # 文字→単語インデックスの対応表を作る（正規化後の文字列ベース）
    char_to_word: list[int] = []  # char position → word index
    for widx, w in enumerate(words):
        for _ in _normalize_for_match(w["word"]):
            char_to_word.append(widx)

    total_chars = len(char_to_word)
    whisper_norm_text = "".join(_normalize_for_match(w["word"]) for w in words)

    # 改行テキストを行に分割（空行は段落区切りとして無視）
    lines = [ln.strip() for ln in raw_text.split("\n") if ln.strip()]
    line_display = [apply_corrections(ln) for ln in lines]
    line_norm = [_normalize_for_match(ld) for ld in line_display]

    # ── 行→時刻: difflib 全体アライメント（v6） ──
    # 各行の [行連結内開始, 終了) スパン
    spans: list[tuple[int, int]] = []
    acc = 0
    for n_ in line_norm:
        spans.append((acc, acc + len(n_)))
        acc += len(n_)
    line_cat = "".join(line_norm)

    # 行連結位置 → word連結位置 の単調写像（一致ブロック端点で区分線形補間）
    sm = difflib.SequenceMatcher(None, line_cat, whisper_norm_text, autojunk=False)
    pts: list[tuple[int, int]] = [(0, 0)]
    matched_chars = 0
    for a, b, size in sm.get_matching_blocks():
        if size <= 0:
            continue
        matched_chars += size
        pts.append((a, b))
        pts.append((a + size, b + size))
    pts.append((len(line_cat), total_chars))
    pts = sorted(set(pts))
    mono: list[tuple[int, int]] = []
    last_w = -1
    for lc, wpos in pts:
        if wpos >= last_w:
            mono.append((lc, wpos))
            last_w = wpos
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

    # 各行に時刻を割り当て（start 単調・start<end を保証）
    entries: list[tuple[float, float, str]] = []
    prev_start = words[0]["start"]
    for (s_lc, e_lc), disp, nrm in zip(spans, line_display, line_norm):
        if not nrm or not disp:
            continue
        a0 = map_pos(s_lc)
        a1 = map_pos(e_lc)
        start_char = max(0, min(int(round(a0)), total_chars - 1))
        end_char = max(start_char, min(int(round(a1)) - 1, total_chars - 1))
        start_time = words[char_to_word[start_char]]["start"]
        end_time = words[char_to_word[end_char]]["end"]
        if start_time < prev_start:
            start_time = prev_start
        if end_time <= start_time:
            end_time = start_time + 0.5
        entries.append((start_time, end_time, disp))
        prev_start = start_time

    coverage = matched_chars / max(1, min(len(line_cat), total_chars))
    print(f"\n改行テキスト → SRT（difflib 全体アライメント）")
    print(f"  入力行数: {len(lines)}")
    print(f"  生成エントリ数: {len(entries)}")
    print(f"  アライメント一致率: {coverage * 100:.1f}%")
    if coverage < 0.55:
        print("  ⚠ 一致率が異常に低い: lines.txt と segments.json の組が正しいか確認してください")

    # タイミング調整
    entries = refine_timing(entries, fps)
    if cut_points:
        entries = snap_to_cut_points(entries, cut_points)

    # Premiere Pro 日本語版: UTF-8 BOM + CRLF
    with open(output_path, "w", encoding="utf-8-sig", newline="\r\n") as f:
        for i, (start, end, text) in enumerate(entries, 1):
            f.write(f"{i}\n")
            f.write(f"{to_srt_time(start, fps)} --> {to_srt_time(end, fps)}\n")
            f.write(f"{text}\n\n")

    print(f"\n完了: {output_path}")
    print(f"  エントリ数: {len(entries)}")
    print(f"  fps: {fps}")
    qa_report(entries, srt_path=output_path)


# ── Phase 2: SRTアセンブリ（LLM分割結果から） ────────────────────────────────


def assemble_srt(
    grouped_path: str,
    output_path: str,
    fps: float = FPS,
    xml_path: str | None = None,
) -> None:
    """LLMグルーピング結果からSRTを組み立てる。

    grouped.jsonフォーマット:
    [
      {"text": "テロップテキスト", "start": 3.73, "end": 4.55},
      ...
    ]
    """
    with open(grouped_path, encoding="utf-8") as f:
        groups = json.load(f)

    # カット点取得（XMLあり）
    cut_points: list[float] = []
    if xml_path:
        cut_points, xml_fps = parse_xml_cut_points(xml_path)
        if cut_points:
            fps = xml_fps

    # エントリ構築（テキスト修正を安全ネットとして再適用）
    entries: list[tuple[float, float, str]] = []
    for g in groups:
        text = apply_corrections(g["text"])
        if text:
            entries.append((g["start"], g["end"], text))

    if not entries:
        print("エラー: グルーピングデータが空です")
        sys.exit(1)

    # タイミング調整（pre-roll, post-roll, gap fill, min duration, frame snap）
    entries = refine_timing(entries, fps)

    # カット点スナップ（微調整）
    if cut_points:
        entries = snap_to_cut_points(entries, cut_points)

    # Premiere Pro日本語版: UTF-8 BOM + CRLF
    with open(output_path, "w", encoding="utf-8-sig", newline="\r\n") as f:
        for i, (start, end, text) in enumerate(entries, 1):
            f.write(f"{i}\n")
            f.write(f"{to_srt_time(start, fps)} --> {to_srt_time(end, fps)}\n")
            f.write(f"{text}\n\n")

    print(f"\n完了: {output_path}")
    print(f"  エントリ数: {len(entries)}")
    print(f"  fps: {fps}")
    if xml_path:
        print(f"  XMLカット点同期: 有効")


# ── メイン ────────────────────────────────────────────────────────────────────


def main() -> None:
    parser = argparse.ArgumentParser(
        description="音声 → SRT（LLMセマンティック分割・v2）",
        epilog="推奨入力: WAV / 16kHz / モノラル / 16bit",
    )
    parser.add_argument("audio", nargs="?", help="入力音声ファイルのパス（WAV推奨）")
    parser.add_argument("-o", "--output", help="出力ファイルのパス")
    parser.add_argument(
        "--fps", type=float, default=FPS,
        help=f"フレームレート（デフォルト: {FPS}）",
    )
    parser.add_argument(
        "--whisper-only", action="store_true",
        help="Whisperだけ実行してJSONを保存",
    )
    parser.add_argument(
        "--from-json",
        help="既存のsegments.jsonからwords JSONを出力（Whisperスキップ）",
    )
    parser.add_argument(
        "--emit-candidates",
        help="segments.jsonから初期候補分割（candidates.json）を出力。Step 3c 用。",
    )
    parser.add_argument(
        "--smart-group",
        help="candidates.jsonから機械的前処理（断片結合・禁止文頭修復・25字分割）を行う。Step 3c.5 用。",
    )
    parser.add_argument(
        "--pause-threshold",
        type=float,
        default=0.30,
        help="単語間ギャップによる境界検出の閾値（秒）。デフォルト0.30",
    )
    parser.add_argument(
        "--assemble",
        help="LLMグルーピング結果（grouped.json）からSRTを組み立て",
    )
    parser.add_argument(
        "--qa",
        help="既存 SRT ファイルの品質チェックのみ実行して終了",
    )
    parser.add_argument(
        "--from-text",
        help="改行テキスト（.txt）から直接 SRT を生成（v5 メイン経路）。"
             "segments.json（--segments）と併用。",
    )
    parser.add_argument(
        "--segments",
        help="--from-text と併用。Whisper の segments.json を指定する。",
    )
    parser.add_argument(
        "--xml",
        help="Premiere Pro XMLファイル（カット点同期・オフセット自動算出）",
    )
    parser.add_argument(
        "--output-dir",
        help="全ての出力ファイルをこのディレクトリに配置する。"
             "指定しない場合は入力ファイルと同じディレクトリ。"
             "推奨: $REPO_DIR/output/srt/<video-name>/",
    )
    args = parser.parse_args()

    # --output-dir が指定されていれば作成
    if args.output_dir:
        os.makedirs(args.output_dir, exist_ok=True)

    def resolve_output_path(src_path: str, suffix: str) -> str:
        """入力パスと拡張子から出力パスを決定する。
        --output-dir 指定時はそこに、なければ入力と同じディレクトリに配置。
        拡張子は '.segments.json' '.words.json' '.candidates.json' '.srt' 等。
        """
        basename = Path(src_path).stem
        for strip_suf in (".segments", ".words", ".candidates", ".grouped"):
            if basename.endswith(strip_suf):
                basename = basename[: -len(strip_suf)]
                break
        if args.output_dir:
            return str(Path(args.output_dir) / f"{basename}{suffix}")
        return str(Path(src_path).parent / f"{basename}{suffix}")

    # XML存在チェック
    xml_path = None
    if args.xml:
        if not os.path.exists(args.xml):
            print(f"エラー: XMLが見つかりません: {args.xml}")
            sys.exit(1)
        xml_path = args.xml

    # ── QA: 既存 SRT の品質チェックのみ ──
    if args.qa:
        if not os.path.exists(args.qa):
            print(f"エラー: SRTが見つかりません: {args.qa}")
            sys.exit(1)
        qa_report(_parse_srt_file(args.qa), srt_path=args.qa)
        return

    # ── Phase 2: SRTアセンブリ ──
    if args.assemble:
        if not os.path.exists(args.assemble):
            print(f"エラー: grouped JSONが見つかりません: {args.assemble}")
            sys.exit(1)
        output_path = args.output or resolve_output_path(args.assemble, ".srt")
        assemble_srt(args.assemble, output_path, args.fps, xml_path=xml_path)
        return

    # ── Phase 2b: --from-text（改行テキスト → SRT 直接生成・v5） ──
    if args.from_text:
        if not os.path.exists(args.from_text):
            print(f"エラー: テキストファイルが見つかりません: {args.from_text}")
            sys.exit(1)
        if not args.segments:
            print("エラー: --from-text は --segments と併用が必須です")
            sys.exit(1)
        if not os.path.exists(args.segments):
            print(f"エラー: segments JSON が見つかりません: {args.segments}")
            sys.exit(1)
        output_path = args.output or resolve_output_path(args.from_text, ".srt")
        assemble_from_text(
            args.segments,
            args.from_text,
            output_path,
            fps=args.fps,
            xml_path=xml_path,
        )
        return

    # ── Phase 1d: candidates.json → smart-group (Step 3c.5) ──
    if args.smart_group:
        if not os.path.exists(args.smart_group):
            print(f"エラー: candidates JSONが見つかりません: {args.smart_group}")
            sys.exit(1)
        output_path = args.output or resolve_output_path(args.smart_group, ".smartgroup.json")
        smart_group(args.smart_group, output_path)
        return

    # ── Phase 1c: segments.json → candidates.json (Step 3c) ──
    if args.emit_candidates:
        if not os.path.exists(args.emit_candidates):
            print(f"エラー: segments JSONが見つかりません: {args.emit_candidates}")
            sys.exit(1)
        output_path = args.output or resolve_output_path(args.emit_candidates, ".candidates.json")
        emit_candidates(
            args.emit_candidates,
            output_path,
            xml_path=xml_path,
            pause_threshold=args.pause_threshold,
        )
        return

    # ── Phase 1b: segments.json → words JSON ──
    if args.from_json:
        if not os.path.exists(args.from_json):
            print(f"エラー: JSONが見つかりません: {args.from_json}")
            sys.exit(1)
        with open(args.from_json, encoding="utf-8") as f:
            seg_list = json.load(f)
        output_path = args.output or resolve_output_path(args.from_json, ".words.json")
        output_words_json(seg_list, output_path, xml_path=xml_path)
        return

    # ── Phase 1: Whisper → segments.json + words JSON ──
    if not args.audio:
        parser.print_help()
        sys.exit(1)

    if not os.path.exists(args.audio):
        print(f"エラー: ファイルが見つかりません: {args.audio}")
        sys.exit(1)

    seg_list = run_whisper(args.audio)

    # segments.json 保存（キャッシュ）
    json_path = resolve_output_path(args.audio, ".segments.json")
    with open(json_path, "w", encoding="utf-8") as f:
        json.dump(seg_list, f, ensure_ascii=False, indent=2)
    print(f"セグメント保存: {json_path}")

    if not args.whisper_only:
        # words.json 出力（LLM分割用）
        words_path = args.output or resolve_output_path(args.audio, ".words.json")
        output_words_json(seg_list, words_path, xml_path=xml_path)


if __name__ == "__main__":
    main()
