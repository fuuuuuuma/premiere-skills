#!/usr/bin/env python3
"""
話者マイク自動ゲート（mic-gate）

二人の演者がステレオ1本のL/R（または別トラック）に分かれて録音された素材で、
「片方が喋っている区間はもう片方のマイクを無効(enabled=FALSE)にする」を XML 加工で自動化する。

設計: docs/specs/2026-06-20-mic-gate-design.md
- 被り除去 = 相対ドミナンス(a) + 自動キャリブレーション(c)
- ゲート式: enabled_X = X発話 OR NOT Y発話（同時・無音は両方ON）
- 非破壊: クリップを削除せず、話者交代点で分割して enabled を切るだけ

入力XMLは /cut 済み・未カットどちらでも可（各クリップの source in/out から判定するため）。
"""

import argparse
import copy
import os
import subprocess
import sys
import xml.etree.ElementTree as ET
from urllib.parse import unquote, urlparse

import numpy as np

TICKS_PER_SECOND = 254016000000


# ── パス解決（silence_cut.py と同じ規約） ──
def pathurl_to_filepath(pathurl):
    return unquote(urlparse(pathurl).path)


def build_file_id_map(root):
    file_map = {}
    file_full = {}  # id -> 完全な<file>要素のテンプレ（子持ち）
    for file_elem in root.iter('file'):
        fid = file_elem.get('id')
        if not fid:
            continue
        if fid not in file_map:
            pathurl = file_elem.find('pathurl')
            if pathurl is not None and pathurl.text:
                file_map[fid] = pathurl_to_filepath(pathurl.text)
        if fid not in file_full and len(list(file_elem)) > 0:
            file_full[fid] = copy.deepcopy(file_elem)
    return file_map, file_full


# ── 音声解析 ──
def decode_stereo_envelope(path, sr, hop, win):
    """ステレオを s16le で全デコードし、L/R の dB エンベロープ(ホップ間隔)を返す。

    返り値: (env_L, env_R, hop_sec)  ※env は 20*log10(rms/32768)
    """
    cmd = ['ffmpeg', '-v', 'error', '-i', path,
           '-vn', '-ac', '2', '-ar', str(sr), '-f', 's16le', '-']
    raw = subprocess.run(cmd, capture_output=True, timeout=3600).stdout
    x = np.frombuffer(raw, dtype=np.int16).astype(np.float64).reshape(-1, 2)
    L, R = x[:, 0], x[:, 1]

    hop_n = max(1, int(sr * hop))
    win_n = max(hop_n, int(sr * win))

    def env(c):
        n = (len(c) - win_n) // hop_n + 1
        if n <= 0:
            return np.array([-120.0])
        # 各ホップ位置の窓RMS（累積和でO(N)）
        csum = np.concatenate(([0.0], np.cumsum(c * c)))
        starts = np.arange(n) * hop_n
        ends = starts + win_n
        rms = np.sqrt(np.maximum((csum[ends] - csum[starts]) / win_n, 1e-9))
        return 20 * np.log10(rms / 32768 + 1e-12)

    return env(L), env(R), hop


# ── ラン整形（平滑化） ──
def runs(mask):
    """bool配列を (start, end, value) のランに分解。"""
    out = []
    if len(mask) == 0:
        return out
    s = 0
    for i in range(1, len(mask)):
        if mask[i] != mask[s]:
            out.append((s, i, bool(mask[s])))
            s = i
    out.append((s, len(mask), bool(mask[s])))
    return out


def fill_short_false(mask, min_len):
    """min_len 未満の False ラン（短いポーズ）を True に塗る = releaseハングオーバー。
    発話ターン中の音節間ポーズを橋渡しし、ゲートが音節単位に粉砕するのを防ぐ。"""
    m = mask.copy()
    for s, e, v in runs(mask):
        if not v and (e - s) < min_len:
            m[s:e] = True
    return m


def drop_short_true(mask, min_len):
    """min_len 未満の True ラン（単発の微小blip）を False に落とす = attackデバウンス。
    被りや物音による一瞬の誤発話をターン開始として拾わないようにする。"""
    m = mask.copy()
    for s, e, v in runs(mask):
        if v and (e - s) < min_len:
            m[s:e] = False
    return m


def drop_short_runs(mask, min_len):
    """min_len 未満のラン（True/False両方）を隣に吸収（最小区間長）。"""
    m = mask.copy()
    changed = True
    while changed:
        changed = False
        rs = runs(m)
        for idx, (s, e, v) in enumerate(rs):
            if (e - s) < min_len and len(rs) > 1:
                m[s:e] = (not v)  # 反転して隣と統合
                changed = True
                break
    return m


def absorb_short_states(states, min_len):
    """min_len 未満の状態ラン(3値: R/L/both)を隣（前優先）に吸収＝min-dwell。
    優勢話者の切替がチャタリングしないようにする。"""
    s = list(states)
    changed = True
    while changed:
        changed = False
        rs = []
        st = 0
        for k in range(1, len(s)):
            if s[k] != s[st]:
                rs.append((st, k, s[st])); st = k
        rs.append((st, len(s), s[st]))
        for k, (a, b, v) in enumerate(rs):
            if (b - a) < min_len and len(rs) > 1:
                nb = rs[k - 1][2] if k > 0 else rs[k + 1][2]
                for j in range(a, b):
                    s[j] = nb
                changed = True
                break
    return s


def enabled_regions_frames(enabled_mask, hop_sec, fps):
    """enabled bool(ホップ単位) を (src_frame_start, src_frame_end, enabled) の区間へ。"""
    out = []
    for s, e, v in runs(enabled_mask):
        f0 = int(round(s * hop_sec * fps))
        f1 = int(round(e * hop_sec * fps))
        if f1 > f0:
            out.append((f0, f1, v))
    return out


def main():
    ap = argparse.ArgumentParser(description="話者マイク自動ゲート（mic-gate）")
    ap.add_argument("input_xml")
    ap.add_argument("--output-dir")
    ap.add_argument("-o", "--output")
    ap.add_argument("--l-sourcetrack", type=int, default=1,
                    help="ファイル左ch(=演者L)に対応する source audio track番号")
    ap.add_argument("--r-sourcetrack", type=int, default=2,
                    help="ファイル右ch(=演者R)に対応する source audio track番号")
    ap.add_argument("--swap", action="store_true",
                    help="L/R↔sourcetrack対応を入れ替える")
    ap.add_argument("--floor", type=float, default=-50.0, help="絶対無音floor(dB)")
    ap.add_argument("--hysteresis", type=float, default=3.0,
                    help="優勢の切替に必要なA1-A2のdB差。小さいほど敏感(チャタリング増)")
    ap.add_argument("--min-dwell", type=float, default=0.25,
                    help="優勢状態の最小継続(s)。これ未満の瞬間的な切替は無視")
    ap.add_argument("--silence-hold", type=float, default=0.5,
                    help="この秒を超える無音で両方ONに戻す。短い無音はターン継続とみなす")
    ap.add_argument("--diff-smooth", type=float, default=0.12,
                    help="A1-A2差の移動平均窓(s)。音節ジッタ低減")
    ap.add_argument("--min-seg", type=float, default=0.12, help="最終の微小クリップ除去しきい(s)")
    ap.add_argument("--bleed-default", type=float, default=12.0, help="ソロ不足時の被り減衰既定(dB)")
    args = ap.parse_args()

    base, ext = os.path.splitext(args.input_xml)
    basename = os.path.basename(base)
    if args.output:
        out_xml = args.output
    elif args.output_dir:
        os.makedirs(args.output_dir, exist_ok=True)
        out_xml = os.path.join(args.output_dir, f"{basename}_ゲート済み{ext}")
    else:
        out_xml = f"{base}_ゲート済み{ext}"

    l_src = args.l_sourcetrack
    r_src = args.r_sourcetrack
    if args.swap:
        l_src, r_src = r_src, l_src

    print("=" * 60)
    print("話者マイク自動ゲート（mic-gate）")
    print("=" * 60)
    print(f"入力: {args.input_xml}")
    print(f"出力: {out_xml}")
    print(f"ゲート対応: sourcetrack {l_src}→L(ch左) / sourcetrack {r_src}→R(ch右)"
          + ("  [--swap適用]" if args.swap else ""))

    # ── XML解析 ──
    tree = ET.parse(args.input_xml)
    root = tree.getroot()
    seq = root.find('.//sequence')
    rate = seq.find('.//rate')
    tb = int(rate.find('timebase').text)
    ntsc = rate.find('ntsc').text.upper() == 'TRUE'
    fps = tb * 1000 / 1001 if ntsc else tb
    ticks_per_frame = int(TICKS_PER_SECOND / fps)
    print(f"  fps={fps:.4f}")

    file_map, file_full = build_file_id_map(root)

    audio = seq.find('.//media/audio')
    if audio is None:
        print("ERROR: audioトラックなし"); sys.exit(1)

    # 音声トラック収集（A番号付与・空は番号だけ進める）
    # ★グループ化は sourcetrack 単位（=ステレオ展開ペア=同一演者）。
    #   outputchannelindex(左右leg)で割ると同一演者の左右に別ゲートがかかり音量が崩れる。
    a_tracks = []  # (audio_index, track_elem, [clip dict...], sourcetrack)
    a_idx = 0
    media_path = None
    for tr in audio.findall('track'):
        clips_el = tr.findall('clipitem')
        a_idx += 1
        if not clips_el:
            a_tracks.append((a_idx, tr, [], None))
            continue
        clips = []
        st = None
        for c in clips_el:
            cin = int(c.find('in').text); cout = int(c.find('out').text)
            cs = int(c.find('start').text); ce = int(c.find('end').text)
            clips.append({'el': c, 'in': cin, 'out': cout, 'start': cs, 'end': ce,
                          'offset': cin - cs})
            if media_path is None:
                media_path = resolve(c, file_map)
            if st is None:
                ste = c.find('sourcetrack/trackindex')
                if ste is not None:
                    st = int(ste.text)
        a_tracks.append((a_idx, tr, clips, st))

    if not media_path or not os.path.exists(media_path):
        print(f"ERROR: 音声メディアが見つからない: {media_path}"); sys.exit(1)
    print(f"  音声メディア: {os.path.basename(media_path)}")

    # ── L/R 解析 ──
    print("[解析] L/R エンベロープ算出...")
    hop = 0.01
    env_L, env_R, hop = decode_stereo_envelope(media_path, sr=8000, hop=hop, win=0.03)
    n = min(len(env_L), len(env_R))
    env_L, env_R = env_L[:n], env_R[:n]

    floor = args.floor

    # ── 純ドミナンス比較（A1 vs A2、大きい方だけ残す）──
    # 被りは本人より必ず小さいので、env_R(ファイルRch) vs env_L(ファイルLch) の直接比較で
    # 自動的に弾ける。旧来の「被り減衰を引いた緩い式」は非優勢側を残しすぎたため廃止。
    def smooth(x, win_s):
        w = max(1, int(win_s / hop)); k = np.ones(w) / w
        return np.convolve(x, k, mode='same')
    diff = smooth(env_R - env_L, args.diff_smooth)   # >0:Rch優勢 / <0:Lch優勢
    hys = args.hysteresis
    sil_hold = max(1, int(args.silence_hold / hop))

    # 状態機械: 'R'=Rch優勢(gray残す) / 'L'=Lch優勢(brown残す) / 'both'=無音(両方ON)
    state = [None] * n
    cur = 'both'; sil = 0
    for i in range(n):
        if env_R[i] < floor and env_L[i] < floor:
            sil += 1
            if sil >= sil_hold:
                cur = 'both'          # 長い無音→両方ON。短い無音はターン継続(cur維持)
        else:
            sil = 0
            if diff[i] > hys:
                cur = 'R'             # Rch(gray)が大→grayを残しbrownをOFF
            elif diff[i] < -hys:
                cur = 'L'             # Lch(brown)が大→brownを残しgrayをOFF
            # ヒステリシス帯(±hys内)は cur 維持
        state[i] = cur

    # min-dwell: 短すぎる優勢状態を吸収（チャタリング除去）
    state = absorb_short_states(state, max(1, int(args.min_dwell / hop)))
    st = np.array(state, dtype=object)
    en_R = (st == 'R') | (st == 'both')   # Rch(gray)マイク有効
    en_L = (st == 'L') | (st == 'both')   # Lch(brown)マイク有効

    # 最終の微小クリップ除去
    min_seg_hops = max(1, int(args.min_seg / hop))
    en_L = drop_short_runs(en_L, min_seg_hops)
    en_R = drop_short_runs(en_R, min_seg_hops)

    reg_L = enabled_regions_frames(en_L, hop, fps)
    reg_R = enabled_regions_frames(en_R, hop, fps)
    off_L = sum(e - s for s, e, v in reg_L if not v)
    off_R = sum(e - s for s, e, v in reg_R if not v)
    print(f"  (全メディア解析上のOFF候補) Lch:{off_L/fps:.1f}s Rch:{off_R/fps:.1f}s "
          f"※実際の無効尺はタイムライン被覆分のみ（下に表示）")

    # ── トラックごとに分割＆enable付与 ──
    print("[再構築] クリップ分割＋enable...")
    clip_counter = [1]
    total_new = 0
    for a_index, tr, clips, st in a_tracks:
        if not clips:
            continue
        # sourcetrack で L/R チャンネルを決定。ペアの左右legは同一stなので必ず同じregs=同一enabled。
        if st == l_src:
            regs = reg_L; ch = 'L'
        elif st == r_src:
            regs = reg_R; ch = 'R'
        else:
            print(f"  A{a_index}: sourcetrack={st} は対象外 → 無変更")
            continue
        new_clips = build_track_clips(clips, regs, file_full, ticks_per_frame, clip_counter)
        # 旧クリップ除去 → 新クリップ追加
        for c in tr.findall('clipitem'):
            tr.remove(c)
        for nc in new_clips:
            tr.append(nc)
        total_new += len(new_clips)
        disabled_f = sum(int(nc.find('end').text) - int(nc.find('start').text)
                         for nc in new_clips if nc.find('enabled').text == 'FALSE')
        print(f"  A{a_index}(sourcetrack={st}→{ch}ch): {len(clips)} → {len(new_clips)} クリップ"
              f"  実無効尺 {disabled_f/fps:.1f}s")

    # ── 出力 ──
    ET.indent(tree, space='\t')
    tree.write(out_xml, encoding='UTF-8', xml_declaration=True)
    with open(out_xml, 'r', encoding='UTF-8') as f:
        s = f.read()
    s = s.replace("<?xml version='1.0' encoding='UTF-8'?>",
                  '<?xml version="1.0" encoding="UTF-8"?>\n<!DOCTYPE xmeml>')
    with open(out_xml, 'w', encoding='UTF-8') as f:
        f.write(s)
    print(f"\n出力: {out_xml}")
    print(f"完了: 新クリップ総数 {total_new}")


def resolve(clip, file_map):
    fe = clip.find('file')
    if fe is None:
        return None
    pu = fe.find('pathurl')
    if pu is not None and pu.text:
        return pathurl_to_filepath(pu.text)
    fid = fe.get('id')
    return file_map.get(fid)


def build_track_clips(clips, regs, file_full, ticks_per_frame, counter):
    """既存クリップ列を enabled 区間で再分割した新クリップ列を返す。位置は不変。"""
    new = []
    file_defined = set()
    for clip in clips:
        cin, cout = clip['in'], clip['out']
        offset = clip['offset']
        # このクリップの source 範囲 [cin,cout) に重なる enabled 区間を列挙
        pieces = []
        for f0, f1, en in regs:
            a = max(cin, f0); b = min(cout, f1)
            if b > a:
                pieces.append((a, b, en))
        if not pieces:
            pieces = [(cin, cout, True)]
        pieces.sort()
        # 隙間（regが全域を覆わない場合）を ON で埋める
        filled = []
        cur = cin
        for a, b, en in pieces:
            if a > cur:
                filled.append((cur, a, True))
            filled.append((a, b, en))
            cur = b
        if cur < cout:
            filled.append((cur, cout, True))

        for a, b, en in filled:
            if b <= a:
                continue
            nc = copy.deepcopy(clip['el'])
            nid = f"clipitem-g{counter[0]}"; counter[0] += 1
            nc.set('id', nid)
            tl_start = a - offset
            tl_end = b - offset
            set_text(nc, 'in', a); set_text(nc, 'out', b)
            set_text(nc, 'start', tl_start); set_text(nc, 'end', tl_end)
            set_text(nc, 'pproTicksIn', a * ticks_per_frame)
            set_text(nc, 'pproTicksOut', b * ticks_per_frame)
            # enabled 付与
            set_text(nc, 'enabled', 'TRUE' if en else 'FALSE')
            # file 参照: トラック内 初回のみ完全、以降は空参照
            fe = nc.find('file')
            if fe is not None:
                fid = fe.get('id')
                if fid in file_defined:
                    for ch in list(fe):
                        fe.remove(ch)
                    fe.text = None
                else:
                    # 完全テンプレで置換（カット済みXMLは2個目以降が空のため）
                    if fid in file_full:
                        full = copy.deepcopy(file_full[fid])
                        nc.remove(fe)
                        # file は元の位置に入れ直す（末尾でもPremiereは解釈する）
                        nc.append(full)
                    file_defined.add(fid)
            # link は独立化のため除去（A1/A2…を独立分割するため）
            for lk in nc.findall('link'):
                nc.remove(lk)
            new.append(nc)
    return new


def set_text(elem, tag, value):
    e = elem.find(tag)
    if e is not None:
        e.text = str(value)


if __name__ == '__main__':
    main()
