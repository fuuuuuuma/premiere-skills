#!/usr/bin/env python3
"""
A1音声ベースの無音カット - 全トラック同期編集点
各トラックに複数クリップがある場合も正しく処理する。
A1の各クリップの音声で無音検出し、全トラックに同じタイムライン位置で編集点を入れる。
"""

import xml.etree.ElementTree as ET
import subprocess
import re
import copy
import os
import sys
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


def detect_silence(audio_file, start_sec, duration_sec, threshold_db=-35, min_silence=0.2):
    """ffmpeg silencedetectで無音区間を検出。返り値は(start_sec, end_sec)のリスト（絶対時間）"""
    cmd = [
        'ffmpeg', '-hide_banner',
        '-ss', str(start_sec),
        '-t', str(duration_sec),
        '-i', audio_file,
        '-vn',
        '-af', f'silencedetect=noise={threshold_db}dB:d={min_silence}',
        '-f', 'null', '-'
    ]
    result = subprocess.run(cmd, capture_output=True, text=True, timeout=3600)

    silences = []
    silence_start = None
    for line in result.stderr.split('\n'):
        if 'silence_start:' in line:
            m = re.search(r'silence_start:\s*([\d.eE+-]+)', line)
            if m:
                silence_start = float(m.group(1)) + start_sec
        elif 'silence_end:' in line and silence_start is not None:
            m = re.search(r'silence_end:\s*([\d.eE+-]+)', line)
            if m:
                silence_end = float(m.group(1)) + start_sec
                silences.append((silence_start, silence_end))
                silence_start = None
    if silence_start is not None:
        silences.append((silence_start, start_sec + duration_sec))
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
             "推奨: ~/ClaudeCode/projects/premiere-skills/output/cut/",
    )
    parser.add_argument("--threshold", type=float, default=-35,
                        help="無音判定の閾値(dB)。小さい値ほど厳しく(=カット減)。ぶつぶつ喋りは -45〜-50 推奨")
    parser.add_argument("--min-silence", type=float, default=0.2,
                        help="無音と判定する最小秒数。大きくすると短い間(ま)を残す")
    parser.add_argument("--padding", type=int, default=2,
                        help="カット前後に残すパディングフレーム数")
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

    THRESHOLD_DB = -35
    MIN_SILENCE = 0.2
    PADDING_FRAMES = 2
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
    sequence = root.find('.//sequence')

    rate_elem = sequence.find('.//rate')
    tb = int(rate_elem.find('timebase').text)
    ntsc = rate_elem.find('ntsc').text.upper() == 'TRUE'
    timebase = tb * 1000 / 1001 if ntsc else tb
    ticks_per_frame = int(TICKS_PER_SECOND / timebase)
    min_silence_frames = max(1, int(round(MIN_SILENCE * timebase)))

    seq_duration = int(sequence.find('duration').text)
    print(f"  タイムベース: {timebase:.4f}fps")
    print(f"  シーケンス長: {seq_duration}f ({seq_duration/timebase:.1f}s, {seq_duration/timebase/60:.1f}min)")

    file_id_map = build_file_id_map(root)

    # ── トラック情報収集（複数クリップ対応） ──
    print("\n[2/4] トラック情報収集...")
    video_elem = sequence.find('.//media/video')
    audio_elem = sequence.find('.//media/audio')

    tracks = []  # list of track dicts

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
                offset = in_frame - max(0, tl_start)
                filepath = resolve_file_path(clip, file_id_map)
                enabled = clip.find('enabled')
                is_enabled = enabled is not None and enabled.text.upper() == 'TRUE'

                clips.append({
                    'clip_elem': clip,
                    'in_frame': in_frame,
                    'out_frame': out_frame,
                    'tl_start': tl_start,
                    'tl_end': tl_end,
                    'offset': offset,
                    'filepath': filepath,
                    'enabled': is_enabled,
                })
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

    for ci, a1_clip in enumerate(a1_track['clips']):
        audio_file = a1_clip['filepath']
        if not audio_file or not os.path.exists(audio_file):
            print(f"  WARNING: A1クリップ{ci+1}の音声ファイルが見つかりません: {audio_file}")
            continue

        fname = os.path.basename(audio_file)
        in_sec = a1_clip['in_frame'] / timebase
        dur_sec = (a1_clip['out_frame'] - a1_clip['in_frame']) / timebase
        print(f"  クリップ{ci+1}: {fname}")
        print(f"    解析範囲: {in_sec:.2f}s ～ {in_sec + dur_sec:.2f}s ({dur_sec:.1f}s)")

        silences = detect_silence(audio_file, in_sec, dur_sec, THRESHOLD_DB, MIN_SILENCE)
        print(f"    検出無音: {len(silences)}箇所")

        # ソース秒 → タイムラインフレームに変換
        for s_start, s_end in silences:
            sf_start = int(round(s_start * timebase))
            sf_end = int(round(s_end * timebase))
            tf_start = max(sf_start - a1_clip['offset'], a1_clip['tl_start'])
            tf_end = min(sf_end - a1_clip['offset'], a1_clip['tl_end'])
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
    total_silences = sum(len(detect_silence.__code__.co_varnames) for _ in [])  # avoid recount
    print(f"  キープ区間: {len(keep_tl_regions)}個")
    print(f"  カット: {total_cut}f ({total_cut/timebase:.1f}s)")
    print(f"  結果: {tl_duration/timebase:.1f}s → {total_kept/timebase:.1f}s "
          f"({total_cut/tl_duration*100:.1f}%削減)")

    # ── XML再構築 ──
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

            offset = source_clip['offset']
            src_in = nc['old_tl_start'] + offset
            src_out = nc['old_tl_end'] + offset

            for tag, val in [('in', src_in), ('out', src_out),
                             ('start', nc['new_tl_start']), ('end', nc['new_tl_end'])]:
                elem = new_clip.find(tag)
                if elem is not None:
                    elem.text = str(val)

            for tag, frame in [('pproTicksIn', src_in), ('pproTicksOut', src_out)]:
                elem = new_clip.find(tag)
                if elem is not None:
                    elem.text = str(frame * ticks_per_frame)

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

            # link参照更新
            for link in new_clip.findall('link'):
                linkref = link.find('linkclipref')
                if linkref is not None and linkref.text in old_id_to_track:
                    other_track_idx = old_id_to_track[linkref.text]
                    # 同じsub_idxの新クリップにリンク（同じタイムライン位置の対応クリップ）
                    # ただし他トラックのsub_idx数が異なる可能性があるので、
                    # タイムライン位置で対応するクリップを探す
                    target_sub_idx = find_matching_sub_idx(
                        new_clips_per_track[other_track_idx],
                        nc['new_tl_start'], nc['new_tl_end']
                    )
                    if target_sub_idx is not None:
                        linkref.text = new_id_map[(other_track_idx, target_sub_idx)]
                        clipindex_elem = link.find('clipindex')
                        if clipindex_elem is not None:
                            clipindex_elem.text = str(target_sub_idx + 1)

            track_elem.append(new_clip)

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
