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
CORRECTIONS: dict[str, str] = {
    # ── AI製品名（複合語を先に） ──
    # ClaudeCode（スペースなし表記）をチャンネルルールに採用 (2026-04-18)
    "Claudeコード": "ClaudeCode",
    "クロードコード": "ClaudeCode",
    "Claude Code": "ClaudeCode",
    "チャットジーピーティー": "ChatGPT",
    "チャットGPT": "ChatGPT",
    # ── 動画制作ツール（2026-04-18 追加） ──
    "リノイズ": "Renoise",
    "レノイズ": "Renoise",
    "リモーション": "Remotion",
    "レモーション": "Remotion",
    # ── 画像生成AI（No.769で追加） ──
    "ナノバナナ": "NanoBanana",   # ★ NanoBanana（Gemini画像生成ツール）13回/本
    # ── 動画生成AI（No.769で追加） ──
    "二次ジャーニー": "Midjourney",  # Midjourneyの誤変換（別形）
    "ミッドジャーニー": "Midjourney",  # Midjourneyの誤変換
    "シーダンス": "Seedance",     # Seedance動画生成AI
    "ビデュー": "Vidu",           # Vidu動画生成AI
    "クリング": "Kling",          # Kling動画生成AIの誤変換
    # ── AIエージェント（No.769で追加） ──
    "アンチグラビティ": "Antigravity",  # ★ Antigravity AIエージェント 4回/本
    "ジェンスパーク": "Genspark",  # Genspark AIエージェント
    "コデックス": "Codex",         # ChatGPT Codex
    "コレックス": "Codex",         # ChatGPT Codex（別誤変換）
    "グロック": "Grok",            # ★ xAI Grok 11回/本
    # ── 無料AIツール（No.769で追加） ──
    "フロー": "Flow",              # ★ Flow（無料AI） 12回/本
    # ── 音楽生成AI ──
    "エースミュージック": "Ace Music",  # ★ Ace Music（ローカル音楽生成） 13回/本
    "群れ替えAI": "Mureka AI",    # ★ MurekaAI の誤変換（3回/本）
    "ブレーカーAI": "Mureka AI",  # ★ MurekaAI の別誤変換（2回/本）
    "フリービートAI": "FreeBeatAI",  # FreeBeatAI の誤変換（カタカナ→英字）
    "Snow": "Suno",               # ★ Suno の英語表記誤変換（No.769で4回）
    "スノー": "Suno",             # ★ Suno音楽生成AIの誤変換
    "ユーディオ": "Udio",         # UdioのWhisper誤変換
    "UDEO": "Udio",               # Udioの別誤変換
    "リリア": "Lilia",            # Gemini LiliaのWhisper誤変換（5回/本）
    # ── 動画・画像生成AI ──
    "ベオ3": "Veo 3",             # Google Veo 3の誤変換
    # ── クラウドサービス ──
    "コパイロット": "Copilot",    # Microsoft Copilot
    # ── Claudeモデル名 ──
    "OPAS": "Opus",
    "オーパス": "Opus",
    "SONNET": "Sonnet",
    "ソネット": "Sonnet",
    "HAIKU": "Haiku",
    "ハイク": "Haiku",
    # ── AI製品名（単語） ──
    "クロード": "Claude",
    "ジェミニ": "Gemini",
    "マナス": "Manus",
    "ロバート": "Lovart",
    "カーソル": "Cursor",
    # ── チャンネル固有名詞 ──
    # 2026-04-18: N1 はチャンネル表記「[COMPANY]」に統一
    "エヌワン": "[COMPANY]",
    "N1": "[COMPANY]",
    "AI周期カラボ": "[CHANNEL]",    # ★ チャンネル名の誤変換
    "AI収益化カラボ": "[CHANNEL]",  # ★ チャンネル名の別誤変換
    "AICカラボ": "[CHANNEL]",       # ★ No.ClaudeCode × Renoise × Remotion で発生
    "AI周囲カラボ": "[CHANNEL]",    # ★ No.ClaudeCode × Renoise × Remotion で発生
    "AI主義カラボ": "[CHANNEL]",    # ★ No.ClaudeCode × Renoise × Remotion 後半で発生
    "AICクラブ": "[CHANNEL]",       # ★ 別パターン
    # ── [COMPANY]関連の追加誤認識（2026-04-18） ──
    "NHAI副業大学": "[COMPANY]大学",
    "NHがやってる": "[COMPANY]がやってる",
    "n 1": "[COMPANY]",
    # ── 動画生成・ツール追加（2026-04-18） ──
    "SEEDANCE": "Seedance",
    "Hike": "Haiku",
    "Mid Journey": "Midjourney",
    "Nano Banana": "NanoBanana",
    "ビッグトック": "TikTok",
    "ディスコード": "Discord",
    "キャンバー": "Canva",
    "フィグマ": "Figma",
    # 同音異義語（文脈依存・完全一致のみ置換）
    "精神をテーマに": "青春をテーマに",
    "レイブに出されている": "ライブに出されている",
    "他社に売る": "他者に売る",
    # ── サービス名・ブランド名 ──
    "ネットフリックス": "Netflix",
    "ネトフリ": "Netflix",
    "ジーピーティーズ": "GPTs",
    "ジーピーティーエス": "GPTs",
    # ── 口語→書き言葉（文字数削減） ──
    "っていう": "という",          # 4字→3字（No.769ユーザー指示）
    # ── Whisperの一般的な誤変換 ──
    "該注": "外注",
    "v側近性": "即金性",
    "受託": "受託",
    # ── No.801 学習結果（2026-04-12、音声生成AI徹底比較） ──
    # UIラベル・ボタン名（英語で統一）
    "ジェネレーションコンプリーティットサクセスフリー": "Generation completed successfully",
    "ジェネレイト": "Generate",
    "サウンドタック": "Sound Tag",
    "リリックス": "Lyrics",
    "アングリー": "Angry",
    # プロダクト名(半角スペース正規化)
    "ミニマックスオーディオ": "MiniMax Audio",
    "ミニマックス": "MiniMax",
    "MiniMaxAudio": "MiniMax Audio",
    "フィッシュオーディオ": "Fish Audio",
    "FishAudio": "Fish Audio",
    "イレブンラブス": "ElevenLabs",
    "イレブンラボ": "ElevenLabs",
    # 動画生成
    "ハイローAI": "HailuoAI",
    "ハイロー": "HailuoAI",
    "QN3TTS": "Qwen3-TTS",
    "クエン3TTS": "Qwen3-TTS",
    # 機能名（長い UI 文字列）
    "リムーブバックグラウンドノイズ": "Remove Background Noise",
    "アドユアボイストゥザミックス": "Add Your Voice to the Mix",
    # テキストツースピーチ（UI表記「Speach」を正とする）
    "テキストトゥスピーチ": "Text-to-Speach",
    "テキストツースピーチ": "Text-to-Speach",
    "テキスト2スピーチ": "Text-to-Speach",
    # 助詞補完
    "確かに声似てる": "確かに声は似てる",
    # 同音異義語修正（No.801）
    "アフィリート": "アフィリエイト",
    "ステディサトシ": "Steady Satoshi",
    # ── No.822 学習結果（2026-04-25、中国の動画生成AIがレベチすぎる） ──
    "ソラ": "Sora",
    "ベオー3.1": "Veo 3.1",
    "ベオスリー": "Veo 3",
    "ランメイ": "Runway",
    "ランウェイ": "Runway",
    "ノーラン": "NoLang",
    "KlingAI": "Kling AI",
    "ハイルオAI": "HailuoAI",
    "ハイルオ": "HailuoAI",
    "ラブアート": "Lovart",
    "1AI": "Wan",
    "Seedance2.0": "Seedance 2.0",
    "SEA DANCE 2.0": "Seedance 2.0",
    "Cダンス2.0": "Seedance 2.0",
    "キャップカット": "CapCut",
    "トップビューAI": "TopView AI",
    "ドリーミナ": "Dreamina",
    "ハッピーホース1.0": "HappyHorse 1.0",
    "ハッピーホース": "HappyHorse",
    "岡山": "奥山",
    "アーティフィカルアナリシスビデオアレナ": "Artificial Analysis Video Arena",
    "アーティフィカルアナリシス": "Artificial Analysis",
    "ビデュ": "Vidu",
    "Claudeコワーク": "Claude Cowork",
    "インスタグラム": "Instagram",
    "公式ライン": "公式LINE",
    "LINE登録者限定に": "LINE登録者限定で",
}

# フィラー削除パターン（正規表現）
# 否定先読み (?!...) で複合語の誤削除を防止
FILLER_PATTERNS: list[str] = [
    # ── 明確なフィラー（誤検出リスク低） ──
    r'まあ',                         # ほぼ常にフィラー
    r'確かに',                        # 相槌
    r'え[ーえっ]*と',                  # えっと、ええと、えーと
    # ── 複合語保護付きフィラー ──
    # 「こう」「ちょっと」は No.801 学習で全削除禁止。完成版 V4 で残されているケースが多い
    # （副詞的用法・強調）。LLM の Step 3e で文脈判断で個別削除する
    # r'こう(?![いやしすなだでじゆ])',   # ← No.801: 全削除禁止（5回残存）
    # r'ちょっと(?!した)',             # ← No.801: 全削除禁止（1回残存）
    r'もう(?![少一すい終])',            # もう少し、もう一度、もうすぐ等は保護
    r'はい(?![るっり])',               # はいる等は保護
    # 「ね」は削除しない: 「〜ですね」「〜ですよね」等は共感・確認の意味があり
    # チャンネルスタイルでは意図的に残される（No.770分析で確認）
]

# ──────────────────────────────────────────────────────────────────────────────


def snap_to_frame(seconds: float, fps: float = FPS) -> float:
    return round(seconds * fps) / fps


def to_srt_time(seconds: float, fps: float = FPS) -> str:
    t = snap_to_frame(seconds, fps)
    h = int(t // 3600)
    m = int((t % 3600) // 60)
    s = int(t % 60)
    ms = round((t % 1) * 1000)
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


def run_whisper(audio_path: str) -> list[dict]:
    """Whisperを実行してセグメントリストを返す（word_timestamps含む）。"""
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
    model = WhisperModel("large-v3", device=device, compute_type=compute_type)
    print(f"文字起こし中: {audio_path}")

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

    print(f"Whisperセグメント数: {len(seg_list)}")
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
    # クリップを誤って拾い、+2.8s 等の誤オフセットを生じていた（No.822 で発覚）。
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


# ── Phase 2b: --from-text (改行テキスト → SRT 直接生成・v5) ───────────────────


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
    """改行テキスト + segments.json から SRT を直接生成する（v5 のメイン経路）。

    各行を 1 テロップとし、時刻は Whisper 単語タイミングの累積文字数から割り当てる。
    固有名詞修正（apply_corrections）は Whisper 側・テキスト側の両方に適用してから
    マッチするので、Renoise / ClaudeCode など表記揺れは吸収できる。
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
    # NOTE: Whisper の word は細切れ（例: "A","IC","カ","ラ","ボ"）なので
    # apply_corrections を単語単位で掛けても「AICカラボ→[CHANNEL]」が発動しない。
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

    entries: list[tuple[float, float, str]] = []
    pos = 0  # char_to_word 上の現在位置（lines.txt 累積文字数 = Whisper 文字位置の推定値）
    last_end_time = words[0]["start"]
    tail_count = 0
    anchored = 0
    drift_sum = 0
    TAIL_DUR_S = 1.0  # 末尾で単語切れした行に付与する暫定表示秒数
    SEARCH_RADIUS = 60   # 累積文字位置 ± この範囲内で行頭の prefix を探す
    PROBE_LEN = 6        # 探索に使う行頭文字数

    min_pos = 0  # アンカー検索の下限（前エントリの終端を下回らない）

    def _local_anchor(line_norm: str, expected_pos: int) -> int:
        """行頭 prefix を expected_pos 周辺で探し、ヒットしたらその位置を返す。
        ヒットしない場合は -1。短い行（< PROBE_LEN）はアンカー検索しない。
        lo は min_pos 以上に制限し、過去位置への逆戻りによる誤マッチを防ぐ。
        """
        if len(line_norm) < PROBE_LEN:
            return -1
        probe = line_norm[:PROBE_LEN]
        lo = max(min_pos, expected_pos - SEARCH_RADIUS)
        hi = min(total_chars, expected_pos + SEARCH_RADIUS + len(probe))
        idx = whisper_norm_text.find(probe, lo, hi)
        return idx

    for line in lines:
        line_display = apply_corrections(line)
        line_norm = _normalize_for_match(line_display)
        line_len = len(line_norm)
        if line_len == 0:
            continue

        if pos >= total_chars:
            start_time = last_end_time
            end_time = last_end_time + TAIL_DUR_S
            entries.append((start_time, end_time, line_display))
            last_end_time = end_time
            tail_count += 1
            continue

        # 1. 累積文字数 pos 周辺で行頭の prefix を局所検索
        anchor = _local_anchor(line_norm, pos)
        if anchor >= 0 and anchor != pos:
            drift_sum += anchor - pos
            anchored += 1
            pos = anchor

        end_pos = min(pos + line_len, total_chars) - 1
        start_widx = char_to_word[pos]
        end_widx = char_to_word[end_pos]
        start_time = words[start_widx]["start"]
        end_time = words[end_widx]["end"]

        entries.append((start_time, end_time, line_display))
        last_end_time = max(last_end_time, end_time)
        pos += line_len
        min_pos = pos  # 次行のアンカー検索はここより前を探さない

    print(f"\n改行テキスト → SRT")
    print(f"  入力行数: {len(lines)}")
    print(f"  生成エントリ数: {len(entries)}")
    if tail_count:
        print(f"  末尾補完（単語列超過）: {tail_count} 件（暫定 {TAIL_DUR_S}s/行）")
    if anchored:
        print(f"  アンカー再同期: {anchored} 行（累積drift補正合計 {drift_sum:+d} 文字）")

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
    lens = [len(t) for _, _, t in entries]
    if lens:
        print(f"  平均文字数: {sum(lens)/len(lens):.1f}")
        print(f"  25字超: {sum(1 for l in lens if l > 25)} 件")
        print(f"  4字未満: {sum(1 for l in lens if l < 4)} 件")


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
             "推奨: ~/ClaudeCode/projects/premiere-skills/output/srt/<video-name>/",
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
