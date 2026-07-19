#!/usr/bin/env python3
"""
A1音声ベースの無音カット - 全トラック同期編集点
各トラックに複数クリップがある場合も正しく処理する。
A1の各クリップの音声で無音検出し、全トラックに同じタイムライン位置で編集点を入れる。
"""

import xml.etree.ElementTree as ET
import subprocess
import copy
import os
import sys
import numpy as np
from urllib.parse import unquote, urlparse


def pathurl_to_filepath(pathurl):
    parsed = urlparse(pathurl)
    return unquote(parsed.path)


def build_file_id_map(root):
    file_map = {}
    for file_elem in root.iter('file'):
        fid = file_elem.get('id')
        if fid and fid not in file_map:
            pathurl = file_elem.find('pathurl')
            if pathurl is not None and pathurl.text:
                file_map[fid] = pathurl_to_filepath(pathurl.text)
    return file_map


def resolve_file_path(clip, file_id_map):
    file_elem = clip.find('file')
    if file_elem is None:
        return None
    pathurl = file_elem.find('pathurl')
    if pathurl is not None and pathurl.text:
        return pathurl_to_filepath(pathurl.text)
    fid = file_elem.get('id')
    if fid and fid in file_id_map:
        return file_id_map[fid]
    return None


def build_file_video_dims(root):
    """file id → (width, height)。pathurl付きの完全定義だけを対象にする
    (グラフィック等の実体パスなしfileへ誤ってスケールを付けないため)。"""
    dims = {}
    for file_elem in root.iter('file'):
        fid = file_elem.get('id')
        if not fid or fid in dims:
            continue
        pathurl = file_elem.find('pathurl')
        if pathurl is None or not pathurl.text:
            continue
        w = file_elem.findtext('./media/video/samplecharacteristics/width')
        h = file_elem.findtext('./media/video/samplecharacteristics/height')
        if not w or not h:
            continue
        try:
            dims[fid] = (int(w), int(h))
        except ValueError:
            continue
    return dims


def clip_has_motion_filter(clip_elem):
    """クリップに Basic Motion (手動スケール等の明示指定) が既にあるか。"""
    for eff in clip_elem.findall('./filter/effect'):
        if (eff.findtext('effectid') or '').strip() == 'basic':
            return True
        if (eff.findtext('name') or '').strip() == 'Basic Motion':
            return True
    return False


def fit_scale_percent(file_w, file_h, seq_w, seq_h):
    """素材全体がフレーム内へ収まるスケール% (Scale to Frame Size相当)。"""
    return round(min(seq_w / file_w, seq_h / file_h) * 100, 4)


def build_fit_scale_filter(scale_percent):
    f = ET.Element('filter')
    eff = ET.SubElement(f, 'effect')
    ET.SubElement(eff, 'name').text = 'Basic Motion'
    ET.SubElement(eff, 'effectid').text = 'basic'
    ET.SubElement(eff, 'effectcategory').text = 'motion'
    ET.SubElement(eff, 'effecttype').text = 'motion'
    ET.SubElement(eff, 'mediatype').text = 'video'
    p = ET.SubElement(eff, 'parameter')
    p.set('authoringApp', 'PremierePro')
    ET.SubElement(p, 'parameterid').text = 'scale'
    ET.SubElement(p, 'name').text = 'Scale'
    ET.SubElement(p, 'valuemin').text = '0'
    ET.SubElement(p, 'valuemax').text = '1000'
    ET.SubElement(p, 'value').text = str(scale_percent)
    return f


def insert_fit_scale_filters(sequence, root):
    """解像度がシーケンスと異なりスケール指定を持たないビデオクリップへ、
    フレームサイズに収まる Basic Motion Scale を付与する。

    Premiereの「フレームサイズに合わせてスケール」フラグはFCP XMLに
    書き出されないため、そのままimportすると素材が原寸(100%)で読み込まれ
    「カット後に画面の大きさが変わる」ように見える。明示スケールを持つ
    クリップ(手動スケール・キーフレーム)には触れない。
    Returns: 挿入件数
    """
    fmt = sequence.find('.//media/video/format/samplecharacteristics')
    if fmt is None:
        return 0
    try:
        seq_w = int(fmt.findtext('width'))
        seq_h = int(fmt.findtext('height'))
    except (TypeError, ValueError):
        return 0
    if seq_w <= 0 or seq_h <= 0:
        return 0
    dims = build_file_video_dims(root)
    video_elem = sequence.find('.//media/video')
    if video_elem is None:
        return 0
    inserted = 0
    for track_elem in video_elem.findall('track'):
        for clip in track_elem.findall('clipitem'):
            file_elem = clip.find('file')
            if file_elem is None:
                continue
            wh = dims.get(file_elem.get('id'))
            if not wh or wh == (seq_w, seq_h):
                continue
            if clip_has_motion_filter(clip):
                continue
            clip.append(build_fit_scale_filter(
                fit_scale_percent(wh[0], wh[1], seq_w, seq_h)
            ))
            inserted += 1
    return inserted


def clip_declared_fps(clip_elem, seq_timebase, seq_ntsc):
    """clipitem自身が宣言する実効fpsを返す（<rate>が無ければシーケンス値を継承）。

    XMEMLでは <start>/<end> がシーケンスrate単位、<in>/<out> はクリップ自身の
    rate単位で書かれる。29.97素材を30fpsシーケンスへ置いた場合など両者が食い違う
    プロジェクトでは、この2つを同じ単位として足し引きすると素材の掴み位置が
    タイムライン位置に比例してズレる。
    """
    rate = clip_elem.find('rate')
    tb, ntsc = seq_timebase, seq_ntsc
    if rate is not None:
        tb_elem = rate.find('timebase')
        if tb_elem is not None and tb_elem.text:
            tb = int(tb_elem.text)
        ntsc_elem = rate.find('ntsc')
        if ntsc_elem is not None and ntsc_elem.text:
            ntsc = ntsc_elem.text.strip().upper() == 'TRUE'
    if tb <= 0:
        return None
    return tb * 1000 / 1001 if ntsc else float(tb)


def conform_sequence_rate(tree, sequence, declared_tb, declared_ntsc,
                          true_tb, true_ntsc):
    """出力シーケンスを「Premiereが報告する真のレート」で書き直す。

    書き出しXMLの<rate>宣言が実シーケンスと食い違うと、取り込んだカット結果が
    別のフレームレートのシーケンスになる (30fps→29fps等)。XMEMLでは
    <start>/<end> は宣言レートのグリッド上の値なので、宣言を書き換えるだけでは
    再生速度が変わってしまう。時刻(秒)を保ったままグリッドを張り替える。

    <in>/<out> はクリップ自身のレート基準、pproTicksは絶対時間なので触らない。
    宣言と実レートが一致していれば何もしない (通常ケースは完全な無変更)。
    """
    declared_fps = declared_tb * 1000 / 1001 if declared_ntsc else float(declared_tb)
    true_fps = true_tb * 1000 / 1001 if true_ntsc else float(true_tb)
    if abs(declared_fps - true_fps) < 1e-9:
        return False

    scale = true_fps / declared_fps
    print(f"  WARNING: 書き出しXMLの宣言レート {declared_fps:.4f}fps が"
          f" Premiereの報告する {true_fps:.4f}fps と違います"
          f" → 出力を {true_fps:.4f}fps へ揃えます (タイムライン位置は時刻を保持)")

    # タイムライン位置 (シーケンスグリッド上の値) を張り替える
    for tag in ('start', 'end'):
        for elem in sequence.iter(tag):
            text = (elem.text or '').strip()
            if not text:
                continue
            try:
                value = int(text)
            except ValueError:
                continue
            # -1 はトランジション用の番兵値。そのまま残す
            elem.text = str(value if value < 0 else int(round(value * scale)))

    duration_elem = sequence.find('duration')
    if duration_elem is not None and (duration_elem.text or '').strip().isdigit():
        duration_elem.text = str(int(round(int(duration_elem.text) * scale)))

    # シーケンス自身を説明するrate宣言だけを差し替える
    # (clipitem/file のrateは素材の性質なので触らない)
    targets = [sequence.find('rate')]
    timecode = sequence.find('timecode')
    if timecode is not None:
        targets.append(timecode.find('rate'))
    video_format = sequence.find('./media/video/format/samplecharacteristics')
    if video_format is not None:
        targets.append(video_format.find('rate'))
    for rate_elem in targets:
        if rate_elem is None:
            continue
        tb_elem = rate_elem.find('timebase')
        if tb_elem is not None:
            tb_elem.text = str(true_tb)
        ntsc_elem = rate_elem.find('ntsc')
        if ntsc_elem is not None:
            ntsc_elem.text = 'TRUE' if true_ntsc else 'FALSE'
    return True


def probe_media_fps_duration(path):
    """実メディアの実fpsと長さ(秒)をffprobeで取得。失敗時は(None, None)。

    XMLが宣言する timebase（例: 29）と実体の fps（例: 29.998）がズレることがある。
    フレーム→秒換算を実fpsで行わないと、解析窓が実メディア長を超過し末尾を取りこぼす。
    """
    try:
        out = subprocess.run(
            ['ffprobe', '-v', 'error', '-select_streams', 'v:0',
             '-show_entries', 'stream=avg_frame_rate',
             '-show_entries', 'format=duration',
             '-of', 'default=noprint_wrappers=1', path],
            capture_output=True, text=True, timeout=120
        ).stdout
        fps = None
        duration = None
        for line in out.split('\n'):
            if line.startswith('avg_frame_rate='):
                v = line.split('=', 1)[1].strip()
                if '/' in v:
                    num, den = v.split('/')
                    if float(den) != 0:
                        fps = float(num) / float(den)
                elif v:
                    fps = float(v)
            elif line.startswith('duration='):
                v = line.split('=', 1)[1].strip()
                if v and v != 'N/A':
                    duration = float(v)
        if fps is not None and fps <= 0:
            fps = None
        return fps, duration
    except Exception:
        return None, None


def detect_silence_envelope(audio_file, start_sec, duration_sec,
                            threshold_db=-48, min_silence=0.2,
                            sr=16000, win_ms=20):
    """RMSエンベロープの閾値交差で無音区間を検出（前後対称化の根本対策）。

    silencedetectは発話の『頭』(鋭い立ち上がり)は正確だが『終わり』(緩やかな減衰)を
    約1f遅れて落とすため、前後で残し量がズレる。これは検出方向ではなく減衰音の
    終端定義の曖昧さに由来する。そこで前後を同一基準＝1本の中央窓RMSエンベロープの
    閾値dB交差点で定義すると、対称パディングが定義上ぴったり前後同じ残し量になる。

    返り値は (start_sec, end_sec) のリスト（絶対時間・秒）。numpy必須。
    """
    cmd = [
        'ffmpeg', '-hide_banner', '-v', 'error',
        '-ss', str(start_sec),
        '-t', str(duration_sec),
        '-i', audio_file,
        '-vn', '-ac', '1', '-ar', str(sr),
        '-f', 's16le', '-'
    ]
    raw = subprocess.run(cmd, capture_output=True, timeout=3600).stdout
    x = np.frombuffer(raw, dtype=np.int16).astype(np.float64)
    if x.size == 0:
        return []

    # 中央窓RMS（O(N)の累積和で算出）— 前後の端を完全に同一の窓・基準で測る
    w = max(1, int(sr * win_ms / 1000))
    csum = np.concatenate(([0.0], np.cumsum(x * x)))
    idx = np.arange(x.size)
    lo = np.clip(idx - w // 2, 0, x.size)
    hi = np.clip(lo + w, 0, x.size)
    env = np.sqrt(np.maximum((csum[hi] - csum[lo]) / np.maximum(hi - lo, 1), 1e-9))

    thr = 32768.0 * (10 ** (threshold_db / 20.0))
    quiet = env <= thr  # 無音サンプル

    # 無音ラン（連続quiet）のうち min_silence 以上を抽出。両端は同一交差基準。
    min_len = max(1, int(round(min_silence * sr)))
    d = np.diff(quiet.astype(np.int8))
    starts = list(np.where(d == 1)[0] + 1)
    ends = list(np.where(d == -1)[0] + 1)
    if quiet[0]:
        starts = [0] + starts
    if quiet[-1]:
        ends = ends + [x.size]

    silences = []
    for s, e in zip(starts, ends):
        if e - s >= min_len:
            silences.append((start_sec + s / sr, start_sec + e / sr))
    return silences


def main():
    import argparse
    parser = argparse.ArgumentParser(
        description="A1音声ベース 無音カット（全トラック同期編集点）",
    )
    parser.add_argument("input_xml", help="入力 Premiere Pro XML のパス")
    parser.add_argument(
        "-o", "--output",
        help="出力 XML のパス。未指定時は '<入力>_カット済み.xml'（--output-dir 指定時はそこに配置）",
    )
    parser.add_argument(
        "--output-dir",
        help="出力ディレクトリ。指定時はこのディレクトリに '<basename>_カット済み.xml' を配置。"
             "推奨: $REPO_DIR/output/cut/",
    )
    parser.add_argument("--threshold", type=float, default=-48,
                        help="無音判定の閾値(dB)。小さい値ほど厳しく(=カット減)。ぶつぶつ喋りは -45〜-50 推奨")
    parser.add_argument("--min-silence", type=float, default=0.2,
                        help="無音と判定する最小秒数。大きくすると短い間(ま)を残す")
    parser.add_argument("--padding", type=int, default=2,
                        help="カット前後に残すパディングフレーム数")
    parser.add_argument("--sequence-timebase", type=int, default=None,
                        help="Premiereが報告するシーケンスの真のtimebase。書き出しXMLの"
                             "宣言値と食い違う場合、出力はこちらの値で書き直す"
                             "（30fpsで受けたシーケンスを30fpsで返すための保険）")
    parser.add_argument("--sequence-ntsc", default=None,
                        choices=["TRUE", "FALSE", "true", "false"],
                        help="--sequence-timebase と対で使うNTSCフラグ")
    parser.add_argument("--no-fit-scale", action="store_true",
                        help="素材解像度がシーケンスと異なるクリップへの自動フィット"
                             "スケール付与を無効化 (「フレームサイズに合わせる」フラグは"
                             "XMLに保存されないため、既定では自動付与して見た目を保つ)")
    args = parser.parse_args()

    input_xml = args.input_xml
    base, ext = os.path.splitext(input_xml)
    basename_noext = os.path.basename(base)

    if args.output:
        output_xml = args.output
    elif args.output_dir:
        os.makedirs(args.output_dir, exist_ok=True)
        output_xml = os.path.join(args.output_dir, f"{basename_noext}_カット済み{ext}")
    else:
        output_xml = f"{base}_カット済み{ext}"

    THRESHOLD_DB = args.threshold
    MIN_SILENCE = args.min_silence
    PADDING_FRAMES = args.padding
    TICKS_PER_SECOND = 254016000000

    print("=" * 60)
    print("A1音声ベース 無音カット（全トラック同期）")
    print("=" * 60)
    print(f"入力: {input_xml}")
    print(f"出力: {output_xml}")
    print(f"閾値: {THRESHOLD_DB}dB | 最小無音: {MIN_SILENCE}s | パディング: {PADDING_FRAMES}f")

    # ── XML解析 ──
    print("\n[1/4] XML解析...")
    tree = ET.parse(input_xml)
    root = tree.getroot()
    # 複数 <sequence>（ネストシーケンス・bin内の別シーケンス等）がある場合は
    # clipitem 総数が最大のものを編集対象に選ぶ（先頭固定だと意図しない
    # シーケンスを加工する恐れがある。whisper_to_srt.py の best_seq と同方針）
    sequences = root.findall('.//sequence')
    if not sequences:
        print("ERROR: XMLに<sequence>が見つかりません")
        sys.exit(1)
    sequence = max(sequences, key=lambda s: len(s.findall('.//clipitem')))
    if len(sequences) > 1:
        name = sequence.findtext('name') or sequence.get('id') or '?'
        print(f"  WARNING: <sequence>が{len(sequences)}個あります。"
              f"clipitem最多の '{name}' を編集対象に選択")

    # シーケンス直下の<rate>を最優先で読む。'.//rate' の文書順先頭は、実XMLの
    # 形によっては<timecode>やクリップ側のrateを掴み、非30fpsでの全カット位置
    # ズレ (30fps前提に見える壊れ方) の温床になる。
    rate_elem = sequence.find('rate')
    if rate_elem is None or rate_elem.find('timebase') is None:
        rate_elem = sequence.find('.//rate')
    tb = int(rate_elem.find('timebase').text)
    ntsc = rate_elem.find('ntsc').text.upper() == 'TRUE'
    timebase = tb * 1000 / 1001 if ntsc else tb
    ticks_per_frame = int(TICKS_PER_SECOND / timebase)
    min_silence_frames = max(1, int(round(MIN_SILENCE * timebase)))
    if PADDING_FRAMES * 2 >= min_silence_frames:
        print(f"WARNING: --padding({PADDING_FRAMES}f)×2 が --min-silence"
              f"({MIN_SILENCE}s={min_silence_frames}f) 以上のため、検出した無音が"
              f"すべてパディングに食われて1フレームもカットされません。"
              f"padding を min-silence の半分未満にしてください")

    seq_duration = int(sequence.find('duration').text)
    print(f"  タイムベース: {timebase:.4f}fps")
    print(f"  シーケンス長: {seq_duration}f ({seq_duration/timebase:.1f}s, {seq_duration/timebase/60:.1f}min)")

    file_id_map = build_file_id_map(root)

    # ── トラック情報収集（複数クリップ対応） ──
    print("\n[2/4] トラック情報収集...")
    video_elem = sequence.find('.//media/video')
    audio_elem = sequence.find('.//media/audio')

    tracks = []  # list of track dicts
    probe_cache = {}  # 同一ファイルの複数クリップで ffprobe を繰り返さない（全フェーズで共有）

    track_label_idx = {'video': 0, 'audio': 0}
    for media_type, media_elem in [('video', video_elem), ('audio', audio_elem)]:
        if media_elem is None:
            continue
        for track_elem in media_elem.findall('track'):
            clip_elems = track_elem.findall('clipitem')
            if not clip_elems:
                continue
            track_label_idx[media_type] += 1
            label = f"{'V' if media_type == 'video' else 'A'}{track_label_idx[media_type]}"

            clips = []
            for clip in clip_elems:
                in_frame = int(clip.find('in').text)
                out_frame = int(clip.find('out').text)
                tl_start = int(clip.find('start').text)
                tl_end = int(clip.find('end').text)
                # in/out はクリップ自身のrate単位。シーケンスrateとの比を掛けて
                # 「タイムライン移動量→素材フレーム移動量」に換算する
                # (同一rateなら比=1.0で従来と完全に同じ値になる)。
                clip_fps = clip_declared_fps(clip, tb, ntsc) or timebase
                src_per_tl = clip_fps / timebase
                offset = in_frame - max(0, tl_start)
                # 素材内の開始時刻(秒)。無音検出はこの実時間軸で行う。
                in_sec = in_frame / clip_fps
                filepath = resolve_file_path(clip, file_id_map)
                enabled = clip.find('enabled')
                is_enabled = enabled is not None and enabled.text.upper() == 'TRUE'

                # 実メディアの実fpsと実長。pproTicksの「実メディア終端を超えない」
                # クランプ判定にのみ使う (換算基準には使わない — 下のpproTicks節参照)。
                real_fps = None
                media_dur = None
                if filepath and os.path.exists(filepath):
                    if filepath not in probe_cache:
                        probe_cache[filepath] = probe_media_fps_duration(filepath)
                    real_fps, media_dur = probe_cache[filepath]

                clips.append({
                    'clip_elem': clip,
                    'in_frame': in_frame,
                    'out_frame': out_frame,
                    'tl_start': tl_start,
                    'tl_end': tl_end,
                    'offset': offset,
                    'clip_fps': clip_fps,
                    'src_per_tl': src_per_tl,
                    'in_sec': in_sec,
                    'filepath': filepath,
                    'enabled': is_enabled,
                    'real_fps': real_fps,
                    'media_dur': media_dur,
                })
                if abs(src_per_tl - 1.0) > 1e-9:
                    print(f"    レート混在: クリップ宣言{clip_fps:.4f}fps / "
                          f"シーケンス{timebase:.4f}fps → in/outはクリップrate基準で換算")
                fname = os.path.basename(filepath) if filepath else '?'
                print(f"  {label}: {fname} | offset={offset} | in={in_frame} out={out_frame} | "
                      f"tl=[{tl_start},{tl_end}] | enabled={is_enabled}")

            tracks.append({
                'type': media_type,
                'label': label,
                'track_elem': track_elem,
                'clips': clips,
            })

    # ── A1の全クリップで無音検出 ──
    print("\n[3/4] A1音声で無音検出...")

    a1_track = next((t for t in tracks if t['label'] == 'A1'), None)
    if a1_track is None:
        print("ERROR: A1トラックが見つかりません")
        sys.exit(1)

    # タイムライン全体の範囲
    tl_total_start = max(0, min(c['tl_start'] for c in a1_track['clips']))
    tl_total_end = max(c['tl_end'] for c in a1_track['clips'])
    tl_duration = tl_total_end - tl_total_start

    all_silence_tl_frames = []
    a1_real_fps = None  # 情報表示用：A1メイン素材の実fps（probe_cacheはトラック収集フェーズと共有）

    for ci, a1_clip in enumerate(a1_track['clips']):
        audio_file = a1_clip['filepath']
        if not audio_file or not os.path.exists(audio_file):
            print(f"  WARNING: A1クリップ{ci+1}の音声ファイルが見つかりません: {audio_file}")
            continue

        fname = os.path.basename(audio_file)
        # 時間↔フレームの換算は必ずシーケンス宣言timebaseで行う。
        # 音声は実時間で再生され、タイムラインは宣言fps（例: 30.0）で刻むので、
        # 実音声 T 秒は timeline フレーム T*timebase に置かれる。動画の実fps(例: 29.998)は
        # 音声配置に無関係。ここで実fpsを使うと毎秒(timebase-実fps)分ずれ、後半ほど累積ドリフトする。
        # 実fpsは末尾クランプの判定とfps正規化の判断にのみ使う。
        if audio_file not in probe_cache:
            probe_cache[audio_file] = probe_media_fps_duration(audio_file)
        real_fps, media_dur = probe_cache[audio_file]
        if a1_real_fps is None and real_fps:
            a1_real_fps = real_fps
        # in/out はクリップ自身のrate単位なので、素材内の時刻もそのrateで割る
        clip_fps = a1_clip.get('clip_fps') or timebase
        in_sec = a1_clip['in_sec']
        dur_sec = (a1_clip['out_frame'] - a1_clip['in_frame']) / clip_fps
        # 解析窓を実メディア長でクランプ（窓が実体を超過して末尾を取りこぼすのを防ぐ安全網）
        if media_dur is not None:
            max_dur = media_dur - in_sec
            if max_dur > 0 and dur_sec > max_dur + 0.5:
                print(f"    ⚠ 解析窓 {in_sec + dur_sec:.1f}s が実メディア長 {media_dur:.1f}s を超過 → クランプ")
                dur_sec = max_dur
        print(f"  クリップ{ci+1}: {fname}")
        if real_fps and abs(real_fps - timebase) > 0.01:
            print(f"    実fps={real_fps:.4f}（宣言timebase={timebase:.4f}）→ 換算は宣言timebase基準（音声は実時間配置）")
        print(f"    解析範囲: {in_sec:.2f}s ～ {in_sec + dur_sec:.2f}s ({dur_sec:.1f}s)")

        # 前後を同一基準で検出（中央窓RMSエンベロープの閾値dB交差）。
        # silencedetectは減衰する発話末尾の検出が約1f遅れ前後非対称になるため使わない。
        silences = detect_silence_envelope(
            audio_file, in_sec, dur_sec, THRESHOLD_DB, MIN_SILENCE)
        print(f"    検出無音: {len(silences)}箇所")

        # 検出秒（素材内の実時間）→ タイムラインframe。
        # 素材内の経過時間 (s - in_sec) をシーケンスtimebaseで刻み、クリップの
        # タイムライン開始位置へ足す。素材側の単位 (clip_fps) はin_secに畳んで
        # あるため、ここは常にシーケンス基準の整数フレームになる。
        for s_start, s_end in silences:
            tf_start = max(
                a1_clip['tl_start'] + int(round((s_start - in_sec) * timebase)),
                a1_clip['tl_start'])
            tf_end = min(
                a1_clip['tl_start'] + int(round((s_end - in_sec) * timebase)),
                a1_clip['tl_end'])
            if tf_end - tf_start >= min_silence_frames:
                all_silence_tl_frames.append((tf_start, tf_end))

    # マージ
    if all_silence_tl_frames:
        all_silence_tl_frames.sort()
        merged = [list(all_silence_tl_frames[0])]
        for s, e in all_silence_tl_frames[1:]:
            if s <= merged[-1][1]:
                merged[-1][1] = max(merged[-1][1], e)
            else:
                merged.append([s, e])
        all_silence_tl_frames = [tuple(x) for x in merged]

    # パディング適用 → カット区間
    cut_regions = []
    for tf_start, tf_end in all_silence_tl_frames:
        cs = tf_start + PADDING_FRAMES
        ce = tf_end - PADDING_FRAMES
        if ce > cs:
            cut_regions.append((cs, ce))

    # キープ区間（タイムライン全体）
    keep_tl_regions = []
    current = tl_total_start
    for cs, ce in sorted(cut_regions):
        if cs > current:
            keep_tl_regions.append((current, cs))
        current = max(current, ce)
    if current < tl_total_end:
        keep_tl_regions.append((current, tl_total_end))

    total_kept = sum(e - s for s, e in keep_tl_regions)
    total_cut = tl_duration - total_kept
    print(f"  キープ区間: {len(keep_tl_regions)}個")
    print(f"  カット: {total_cut}f ({total_cut/timebase:.1f}s)")
    print(f"  結果: {tl_duration/timebase:.1f}s → {total_kept/timebase:.1f}s "
          f"({total_cut/tl_duration*100:.1f}%削減)")

    # ── XML再構築 ──
    # rate（timebase/ntsc）は宣言値のまま変更しない。frame番号とtick換算の基準を
    # 揃え続けるための決定。実fpsで出力rateを補正すると、旧timebase基準で計算した
    # フレーム番号と新fps基準のticks_per_frameが食い違い、Premiere側で時間軸がズレる
    # （実測: 220秒素材で数秒規模のドリフト、file要素のrateまで書き換わり同一ソースが
    # 二重fpsで読み込まれる不具合を確認済み）。
    print(f"\n[4/4] XML再構築...")

    # 新タイムライン位置
    new_tl_positions = []  # (old_tl_start, old_tl_end, new_tl_start, new_tl_end)
    current_tl = 0
    for old_start, old_end in keep_tl_regions:
        new_start = current_tl
        new_end = current_tl + (old_end - old_start)
        new_tl_positions.append((old_start, old_end, new_start, new_end))
        current_tl = new_end
    new_total_duration = current_tl

    # シーケンスduration更新
    dur_elem = sequence.find('duration')
    if dur_elem is not None:
        dur_elem.text = str(new_total_duration)
    sequence.set('MZ.WorkOutPoint', str(new_total_duration * ticks_per_frame))

    # 元クリップのIDマッピング（link更新用）
    # old_clip_id → (track_index, clip_index_in_track)
    old_id_to_track = {}
    for track_idx, track_info in enumerate(tracks):
        for clip_idx, clip_info in enumerate(track_info['clips']):
            clip_id = clip_info['clip_elem'].get('id')
            if clip_id:
                old_id_to_track[clip_id] = track_idx

    # 各トラックについて、keep区間を元クリップに分割して新クリップ生成
    # まず全トラック分の新クリップ情報を計算
    # new_clips_per_track[track_idx] = [(new_tl_start, new_tl_end, source_clip_info, old_tl_start, old_tl_end), ...]
    new_clips_per_track = {}

    for track_idx, track_info in enumerate(tracks):
        new_clips = []
        for old_tl_start, old_tl_end, new_tl_start, new_tl_end in new_tl_positions:
            # このキープ区間がどの元クリップにまたがるか
            for clip_info in track_info['clips']:
                overlap_start = max(old_tl_start, clip_info['tl_start'])
                overlap_end = min(old_tl_end, clip_info['tl_end'])
                if overlap_end > overlap_start:
                    # このクリップとの重なり部分
                    new_sub_start = new_tl_start + (overlap_start - old_tl_start)
                    new_sub_end = new_tl_start + (overlap_end - old_tl_start)
                    new_clips.append({
                        'new_tl_start': new_sub_start,
                        'new_tl_end': new_sub_end,
                        'old_tl_start': overlap_start,
                        'old_tl_end': overlap_end,
                        'source_clip': clip_info,
                    })
        new_clips_per_track[track_idx] = new_clips

    # 新ID割り当て
    clip_counter = 1
    new_id_map = {}  # (track_idx, sub_idx) → new_clip_id
    for track_idx in range(len(tracks)):
        for sub_idx in range(len(new_clips_per_track[track_idx])):
            new_id_map[(track_idx, sub_idx)] = f"clipitem-{clip_counter}"
            clip_counter += 1

    # 各トラックのクリップ置換
    for track_idx, track_info in enumerate(tracks):
        track_elem = track_info['track_elem']

        # 元クリップ・トランジション除去（カット後は不要）
        for clip in track_elem.findall('clipitem'):
            track_elem.remove(clip)
        for trans in track_elem.findall('transitionitem'):
            track_elem.remove(trans)

        # ファイルID別に定義済みかどうかを追跡
        file_defined = set()

        for sub_idx, nc in enumerate(new_clips_per_track[track_idx]):
            source_clip = nc['source_clip']
            new_clip = copy.deepcopy(source_clip['clip_elem'])
            new_clip_id = new_id_map[(track_idx, sub_idx)]
            new_clip.set('id', new_clip_id)

            # タイムライン移動量はクリップ自身のrateへ換算してから素材in点へ足す。
            # 同一rate (src_per_tl=1.0) なら従来の "old_tl + offset" と同じ整数値。
            src_per_tl = source_clip.get('src_per_tl', 1.0)
            tl_origin = source_clip['tl_start']
            src_origin = source_clip['in_frame']
            src_in = int(round(
                (nc['old_tl_start'] - tl_origin) * src_per_tl + src_origin))
            src_out = int(round(
                (nc['old_tl_end'] - tl_origin) * src_per_tl + src_origin))

            for tag, val in [('in', src_in), ('out', src_out),
                             ('start', nc['new_tl_start']), ('end', nc['new_tl_end'])]:
                elem = new_clip.find(tag)
                if elem is not None:
                    elem.text = str(val)

            # pproTicksはフレーム値と「同一基準 (宣言timebase)」で書く。
            # 旧実装は実fps基準で書いており、フレーム値 (宣言基準) との差が
            # クリップ位置に比例して開く: 宣言10 vs 実10.06の画面収録 (2026-07-18
            # 実測) では終盤で約14秒ズレ、Premiereがticksを優先してカット崩壊。
            # 差0.1%級 (30 vs 29.998) では見えなかっただけで基準混在が誤り。
            # 実fps/実長は「実メディア終端を超えない」クランプにのみ使う
            # (2026-06の波形読み込み不能の教訓はクランプで担保する)。
            media_dur = source_clip.get('media_dur')
            # 素材内の絶対時刻はクリップ自身のrateで割る。in/outと同じ基準に
            # 揃わないとPremiereがticks優先で読んだときに素材位置がズレる。
            src_frame_fps = source_clip.get('clip_fps') or timebase
            for tag, frame in [('pproTicksIn', src_in), ('pproTicksOut', src_out)]:
                elem = new_clip.find(tag)
                if elem is not None:
                    seconds = frame / src_frame_fps
                    if media_dur and seconds > media_dur:
                        seconds = media_dur
                    elem.text = str(round(seconds * TICKS_PER_SECOND))

            # file参照: 同じfileIDは最初だけ詳細、以降は空参照
            file_elem = new_clip.find('file')
            if file_elem is not None:
                fid = file_elem.get('id')
                if fid in file_defined:
                    for child in list(file_elem):
                        file_elem.remove(child)
                    file_elem.text = None
                    file_elem.tail = None
                else:
                    file_defined.add(fid)

            # link参照更新（対応クリップが見つからない link は要素ごと除去する。
            # 削除済みclipitem IDを残すとPremiere読み込み時にリンク解決エラーや
            # 誤バインドを起こす。mic_gate.py の全除去方針と同じ扱い）
            for link in new_clip.findall('link'):
                linkref = link.find('linkclipref')
                if linkref is None or linkref.text not in old_id_to_track:
                    new_clip.remove(link)
                    continue
                other_track_idx = old_id_to_track[linkref.text]
                # 同じタイムライン位置の対応クリップを探す
                # （他トラックのsub_idx数が異なる可能性があるため位置で対応付ける）
                target_sub_idx = find_matching_sub_idx(
                    new_clips_per_track[other_track_idx],
                    nc['new_tl_start'], nc['new_tl_end']
                )
                if target_sub_idx is None:
                    new_clip.remove(link)
                    continue
                linkref.text = new_id_map[(other_track_idx, target_sub_idx)]
                clipindex_elem = link.find('clipindex')
                if clipindex_elem is not None:
                    clipindex_elem.text = str(target_sub_idx + 1)

            track_elem.append(new_clip)

    # 解像度不一致クリップへのフィットスケール付与
    # (「フレームサイズに合わせる」はXML非保存のため、明示スケールが無い
    #  クリップはimport時に原寸へ戻り画面の大きさが変わって見える)
    if not args.no_fit_scale:
        fitted = insert_fit_scale_filters(sequence, root)
        if fitted:
            print(f"  解像度不一致のクリップ {fitted}件へフィットスケールを付与"
                  f" (--no-fit-scale で無効化可)")

    # キーフレーム付きエフェクトを分割した場合は見え方が変わる恐れを警告
    # (キーフレームのリタイムは行わない — when座標系の仕様が実機未確定のため)
    keyframed = set()
    for track_idx, track_info in enumerate(tracks):
        frag_counts = {}
        for nc in new_clips_per_track[track_idx]:
            key = id(nc['source_clip'])
            frag_counts.setdefault(key, []).append(nc)
        for clip_info in track_info['clips']:
            frags = frag_counts.get(id(clip_info), [])
            if not frags:
                continue
            untouched = (
                len(frags) == 1
                and frags[0]['old_tl_start'] == clip_info['tl_start']
                and frags[0]['old_tl_end'] == clip_info['tl_end']
            )
            if untouched:
                continue
            if clip_info['clip_elem'].find('.//keyframe') is not None:
                name = (clip_info['clip_elem'].findtext('name')
                        or os.path.basename(clip_info['filepath'] or '?'))
                keyframed.add(f"{track_info['label']}:{name}")
    if keyframed:
        print(f"  WARNING: キーフレーム付きエフェクトのクリップを分割しました: "
              f"{', '.join(sorted(keyframed))} — カット後のモーション/スケールの"
              f"見え方をPremiereで確認してください")

    # 宣言レートがPremiereの報告する実シーケンスレートと違う場合、出力を
    # 実レートで書き直す。「30fpsのシーケンスをカットしたら29fpsで返ってきた」
    # を防ぐための最終保証 (2026-07-20 実機報告)。
    if args.sequence_timebase:
        conform_sequence_rate(
            tree, sequence, declared_tb=tb, declared_ntsc=ntsc,
            true_tb=args.sequence_timebase,
            true_ntsc=(args.sequence_ntsc or "FALSE").upper() == "TRUE",
        )

    # XML出力
    ET.indent(tree, space='\t')
    tree.write(output_xml, encoding='UTF-8', xml_declaration=True)

    with open(output_xml, 'r', encoding='UTF-8') as f:
        content = f.read()
    content = content.replace(
        "<?xml version='1.0' encoding='UTF-8'?>",
        '<?xml version="1.0" encoding="UTF-8"?>\n<!DOCTYPE xmeml>'
    )
    with open(output_xml, 'w', encoding='UTF-8') as f:
        f.write(content)

    print(f"\n出力: {output_xml}")
    print(f"完了: {tl_duration/timebase:.1f}s → {new_total_duration/timebase:.1f}s "
          f"(カット {total_cut/timebase:.1f}s, {total_cut/tl_duration*100:.1f}%削減)")


def find_matching_sub_idx(other_new_clips, tl_start, tl_end):
    """タイムライン位置が重なる対応クリップのインデックスを返す"""
    for idx, nc in enumerate(other_new_clips):
        if nc['new_tl_start'] == tl_start and nc['new_tl_end'] == tl_end:
            return idx
    # 完全一致がない場合、最も重なりが大きいものを返す
    best_idx = None
    best_overlap = 0
    for idx, nc in enumerate(other_new_clips):
        overlap = min(tl_end, nc['new_tl_end']) - max(tl_start, nc['new_tl_start'])
        if overlap > best_overlap:
            best_overlap = overlap
            best_idx = idx
    return best_idx


if __name__ == '__main__':
    main()
