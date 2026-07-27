#!/usr/bin/env python3
"""
音声トラックベースの無音カット - 全トラック同期編集点
各トラックに複数クリップがある場合も正しく処理する。

--tracks で指定した1本以上のオーディオトラック (既定 A1 のみ、後方互換) の
各クリップの音声で独立に無音判定し、**指定した全トラックが同時に無音の区間だけ**
をカットする (どれか1本でも音が鳴っていれば残す＝積集合)。編集点は全トラックへ
同じタイムライン位置で同期適用する。

ピンマイク2人以上の対話収録で「A1に片方の声しか乗っておらず、A2にもう片方の声が
ある」場合、A1だけを見ると相手が話している区間まで無音としてカットしてしまう。
--tracks A1,A2 のように両方を指定すると、両方が同時に無音の区間だけが残る。
"""

import math
import re
import xml.etree.ElementTree as ET
import subprocess
import copy
import os
import sys
import numpy as np
from urllib.parse import unquote, urlparse

# Premiereのタイムベース非依存の絶対時間単位 (1秒あたりのtick数)。
TICKS_PER_SECOND = 254016000000

_WINDOWS_DRIVE_PATHURL_RE = re.compile(r"^/[A-Za-z]:")

# 終了コード (呼び出し元が原因で分岐できるようにする。1=汎用エラー・2=argparse)
EXIT_NO_CUT = 3          # 無音・カット区間が1つも無い (成功扱いにしない)
EXIT_AUDIO_FAILED = 4    # 基準トラックの音声を1クリップも解析できなかった


class AudioAnalysisError(RuntimeError):
    """音声の抽出・解析に失敗した。**無音0箇所として続行してはいけない**。

    2026-07-25 視聴者報告「新しいシーケンスはできるが中身が未カットのまま」の
    再現で確定した経路の1つ。ffmpegが失敗しても戻り値を検査せずに空のPCMを
    「無音なし」と解釈していたため、カット0件のXMLが正常出力として公開されていた。
    """


def _db(amplitude, full_scale=32768.0):
    """int16フルスケール基準の dBFS。0以下は None (無音そのもの)。"""
    if amplitude is None or amplitude <= 0:
        return None
    return 20.0 * math.log10(amplitude / full_scale)


# ── 複数トラック対応: 区間集合演算 ──────────────────────────────────
# 「選択した全トラックが同時に無音の区間だけをカットする」= 各トラックの無音
# 区間 (半開区間 [start, end) のタイムラインframe) を求め、その積集合を取る。
# 素朴な実装だが区間数は無音候補の数程度 (実素材で数百〜数千止まり) なので
# O(n log n) で十分高速。

def merge_intervals(intervals):
    """半開区間のリストをソートして隣接・重複を1本にマージする。"""
    if not intervals:
        return []
    ordered = sorted(intervals)
    merged = [list(ordered[0])]
    for s, e in ordered[1:]:
        if s <= merged[-1][1]:
            merged[-1][1] = max(merged[-1][1], e)
        else:
            merged.append([s, e])
    return [tuple(x) for x in merged]


def intersect_intervals(a, b):
    """2つのソート・マージ済み半開区間リストの積集合 (両方に含まれる部分だけ)。"""
    result = []
    i = j = 0
    while i < len(a) and j < len(b):
        s = max(a[i][0], b[j][0])
        e = min(a[i][1], b[j][1])
        if s < e:
            result.append((s, e))
        if a[i][1] < b[j][1]:
            i += 1
        else:
            j += 1
    return result


def intersect_all(interval_lists):
    """N個の区間リストの積集合。空リストが1つでもあれば結果は空。"""
    if not interval_lists:
        return []
    result = merge_intervals(interval_lists[0])
    for other in interval_lists[1:]:
        if not result:
            break
        result = intersect_intervals(result, merge_intervals(other))
    return result


def invert_intervals(intervals, lo, hi):
    """[lo, hi) の中で intervals (ソート・マージ済み) に含まれない部分 (=隙間) を返す。

    「トラックにクリップが乗っていない区間」を検出するために使う
    (音が無いので当然だが、無音判定を試みてすらいない=判定不能ではなく、
    明示的に「無音」として扱う必要がある区間)。
    """
    gaps = []
    cur = lo
    for s, e in intervals:
        if s > cur:
            gaps.append((cur, s))
        cur = max(cur, e)
    if cur < hi:
        gaps.append((cur, hi))
    return gaps

# conform_sequence_rate が「宣言レートの丸め誤差」として許容する最大の相対差。
# 実際に確認済みの切り捨てケース (30↔29.97/29, 60↔59.94/59, 24↔23.976/23) は
# いずれも4%以内。これを大きく超える差 (例: 30↔60の50%) は「Premiereの丸め」
# ではなく「呼び出し元が渡したtrue_tbそのものが誤検出」である可能性が高い
# (2026-07-24 実機報告: パネルのフレームレート検出バグで常にtrue_tb=30が送られ、
# 60fpsの正しいXMLを30fpsへ誤って張り替えて壊していた)。この閾値を超えたら
# 張り替えを拒否し、宣言をそのまま残す (盲目的に信用しない安全網)。
MAX_PLAUSIBLE_CONFORM_DEVIATION = 0.08


def pathurl_to_filepath(pathurl):
    parsed = urlparse(pathurl)
    p = unquote(parsed.path)
    # Windowsのドライブレター付きパス (file://localhost/C:/Users/... 等) は
    # urlparse().path が '/C:/Users/...' というドライブ非認識の不正パスを返す。
    # 先頭の '/' を1文字落として C:/Users/... に正規化する。
    if _WINDOWS_DRIVE_PATHURL_RE.match(p):
        p = p[1:]
    return p


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
    """書き出しXMLの宣言レートがPremiereの報告する実レートと違うとき、出力全体を
    実レートのグリッドへ一貫して張り替える。

    背景 (実機): iPhone等の素材は29.998fps (名目30fps)。Premiereは画面に
    「30.00」と出すが、FCP XML書き出し時に29.998を切り捨てて timebase=29 と書く。
    シーケンスもクリップも <rate>=29。silence_cut はこの29でタイムライン位置・
    素材in/out・pproTicksを計算するため、Premiereが実素材(30fps)で取り込むと
    「pproTicksが指す実時間 × 実fps」と in/out(29基準) がズレ、素材に同期ズレの
    赤バッジ (+17/+18…) が出る。

    そこで宣言レート全体 (start/end/in/out・全<rate>宣言・pproTicks) を true rate
    へ揃える。フレーム値は時刻を保ったまま scale 倍し、<rate>宣言を書き換え、
    pproTicksは張り替え後のin/outと true rate で再計算する (フレームと同一基準に
    保つ = バッジが消える)。素材の<duration> (実フレーム数) は実体の性質なので
    触らない。宣言と実レートが一致していれば完全な無変更。

    注: 全クリップが同じ切り捨てを受けている前提 (シーケンス自体が切り捨てられた
    ときだけ発動)。真に別レートのクリップが混在する編集では、そのクリップも
    シーケンスレートへ寄せる (このワークフローの素材は単一カメラのため実害なし)。

    安全網 (2026-07-24): true_tb/true_ntsc が宣言レートと大きくかけ離れている
    (MAX_PLAUSIBLE_CONFORM_DEVIATION超) 場合は張り替えを拒否する。呼び出し元の
    検出バグが疑わしいときに、正しい宣言を盲目的に壊さないための最終防御。
    """
    declared_fps = declared_tb * 1000 / 1001 if declared_ntsc else float(declared_tb)
    true_fps = true_tb * 1000 / 1001 if true_ntsc else float(true_tb)
    if abs(declared_fps - true_fps) < 1e-9:
        return False

    scale = true_fps / declared_fps
    if abs(scale - 1.0) > MAX_PLAUSIBLE_CONFORM_DEVIATION:
        print(f"  WARNING: 指定されたシーケンスの実レート {true_fps:.4f}fps が"
              f" 書き出しXMLの宣言 {declared_fps:.4f}fps と{abs(scale - 1.0) * 100:.0f}%"
              "もかけ離れているため、張り替えを行いません"
              " (通常の丸め誤差にはあり得ない差 — 呼び出し元の検出結果を疑い、"
              "XML自身の宣言を優先します)")
        return False
    print(f"  WARNING: 書き出しXMLの宣言レート {declared_fps:.4f}fps が"
          f" Premiereの報告する {true_fps:.4f}fps と違います"
          f" → 出力全体を {true_fps:.4f}fps へ揃えます (時刻は保持・同期ズレ防止)")

    def _rescale_frame(text):
        try:
            value = int((text or '').strip())
        except ValueError:
            return None
        # -1 はトランジション用の番兵値。そのまま残す
        return value if value < 0 else int(round(value * scale))

    # タイムライン位置(start/end)を張り替える (独立丸め=同一フレーム値は常に同じ
    # 結果になるため、隣接クリップの端同士が接している関係は保たれる)。
    for tag in ('start', 'end'):
        for elem in sequence.iter(tag):
            rescaled = _rescale_frame(elem.text)
            if rescaled is not None:
                elem.text = str(rescaled)

    # 素材位置(in/out)はクリップ単位で「区間の長さ」を保存して張り替える。
    # start/endと同じ独立丸めをin/outにも適用すると、speed=100%のクリップで
    # (end-start)と(out-in)が本来一致するはずなのに丸め誤差で最大2フレーム
    # ずれ、SRT側の速度変更判定を誤爆させる (2026-07-24 実機報告の残存分)。
    # in を四捨五入した位置を基準に、out は in + round(区間長×scale) とし、
    # 区間長の丸めを1回だけにする (in/outは常に同一クリップ内の値でしか
    # 使われないため、タイムライン側のような隣接一致の制約は無い)。
    for elem in sequence.iter():
        in_elem = elem.find('in')
        out_elem = elem.find('out')
        if in_elem is None or out_elem is None:
            continue
        try:
            in_value = int((in_elem.text or '').strip())
            out_value = int((out_elem.text or '').strip())
        except ValueError:
            continue
        if in_value < 0 or out_value < 0:
            continue  # 番兵値等はそのまま (通常のclipitemでは発生しない)
        new_in = int(round(in_value * scale))
        new_out = new_in + int(round((out_value - in_value) * scale))
        in_elem.text = str(new_in)
        out_elem.text = str(new_out)

    duration_elem = sequence.find('duration')
    if duration_elem is not None and (duration_elem.text or '').strip().isdigit():
        duration_elem.text = str(int(round(int(duration_elem.text) * scale)))

    # 全ての<rate>宣言 (シーケンス/クリップ/ファイル/タイムコード) を true へ。
    # クリップのrateが29のままだとPremiereがpproTicks×実fpsと突き合わせて
    # 同期ズレと判定する。
    for rate_elem in sequence.iter('rate'):
        tb_elem = rate_elem.find('timebase')
        if tb_elem is not None:
            tb_elem.text = str(true_tb)
        ntsc_elem = rate_elem.find('ntsc')
        if ntsc_elem is not None:
            ntsc_elem.text = 'TRUE' if true_ntsc else 'FALSE'

    # pproTicksを張り替え後のin/outとtrue rateで再計算し、フレームと同一基準に保つ。
    for clip in sequence.iter('clipitem'):
        for frame_tag, ticks_tag in (('in', 'pproTicksIn'), ('out', 'pproTicksOut')):
            frame_elem = clip.find(frame_tag)
            ticks_elem = clip.find(ticks_tag)
            if frame_elem is None or ticks_elem is None:
                continue
            try:
                frame = int((frame_elem.text or '').strip())
            except ValueError:
                continue
            if frame >= 0:
                ticks_elem.text = str(round(frame / true_fps * TICKS_PER_SECOND))
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

    返り値は (silences, levels)。
      silences: (start_sec, end_sec) のリスト（絶対時間・秒）
      levels:   音声の実測値 {'peakDb','rmsDb','quietRatio','suggestDb','seconds'}
                — 「なぜ切れなかったのか」を後から追える診断値。

    音声を取り出せなかった場合は AudioAnalysisError を送出する
    (無音0箇所として黙って返すと、カット0件のXMLが「成功」として出てしまう)。
    numpy必須。
    """
    cmd = [
        'ffmpeg', '-hide_banner', '-v', 'error',
        '-ss', str(start_sec),
        '-t', str(duration_sec),
        '-i', audio_file,
        '-vn', '-ac', '1', '-ar', str(sr),
        '-f', 's16le', '-'
    ]
    proc = subprocess.run(cmd, capture_output=True, timeout=3600)
    if proc.returncode != 0:
        detail = (proc.stderr or b'').decode('utf-8', 'replace').strip()
        raise AudioAnalysisError(
            f"ffmpegでの音声抽出に失敗しました (exit {proc.returncode}): "
            f"{os.path.basename(audio_file)}"
            + (f"\n    ffmpeg: {detail[-400:]}" if detail else "")
        )
    x = np.frombuffer(proc.stdout, dtype=np.int16).astype(np.float64)
    if x.size == 0:
        raise AudioAnalysisError(
            f"音声を1サンプルも取り出せませんでした: {os.path.basename(audio_file)} "
            f"({start_sec:.2f}s から {duration_sec:.2f}s)。"
            "音声トラックを持たない素材、または対応コーデックが無い可能性があります"
        )

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

    # ── 診断値 ───────────────────────────────────────────────────
    # 「-40dB以下の無音があるのに切れない」の切り分けは、素材の実レベルが
    # 分からないと不可能。閾値をどこまで上げれば検出できるかまで出す。
    levels = {
        'seconds': x.size / sr,
        'peakDb': _db(float(np.abs(x).max())),
        'rmsDb': _db(float(np.sqrt(np.mean(x * x)))),
        'quietRatio': float(np.mean(quiet)),
        'suggestDb': None,
    }
    # min_len 長のブロックに区切り、各ブロック内エンベロープの最大値のうち
    # 最小のものを取る。その値を閾値にすれば「そのブロックは丸ごと閾値以下」
    # = 最小無音長の無音が必ず1つ成立する (十分条件なので安全側の目安)。
    n_blocks = x.size // min_len
    if n_blocks >= 1:
        block_max = env[:n_blocks * min_len].reshape(n_blocks, min_len).max(axis=1)
        levels['suggestDb'] = _db(float(block_max.min()))
    return silences, levels


def _clip_coverage_intervals(clips, lo, hi):
    """[lo, hi) の中で、いずれかのクリップが乗っている区間の和集合 (ソート・マージ済み)。"""
    ivs = []
    for c in clips:
        s = max(lo, c['tl_start'])
        e = min(hi, c['tl_end'])
        if e > s:
            ivs.append((s, e))
    return merge_intervals(ivs)


def analyze_track_silence(track_info, timebase, threshold_db, min_silence,
                          min_silence_frames, probe_cache):
    """1トラック分の全クリップを解析し、(無音区間リスト, 統計dict) を返す。

    区間リストはクリップ**内**で検出した無音のみ (タイムラインframe、半開区間、
    マージ済み)。トラックにクリップが乗っていない区間 (隙間) はここには含まない
    — 呼び出し元が invert_intervals で明示的に無音として補う
    (「データが無い＝判定不能」と取り違えないため、この関数の責務からは分離する)。

    音声抽出に1クリップでも失敗したら (AudioAnalysisError)、他のトラックの
    解析へ進まず即座にエラー終了する (fail-closed。1本でも取りこぼすと
    その区間が「無音なし=全部残す」に静かに倒れて結果が間違う)。
    """
    label = track_info['label']
    silence_intervals = []
    analyzed_count = 0
    skipped_reasons = []
    peak_db = None
    energy = 0.0
    seconds = 0.0
    quiet_seconds = 0.0
    suggest_db = None

    for ci, clip in enumerate(track_info['clips']):
        audio_file = clip['filepath']
        if not audio_file:
            # <file> を持たないクリップ (ネストシーケンス・マルチカム・
            # 合成クリップ等)。音声の実体パスが無いので解析できない。
            skipped_reasons.append(
                f"{label}クリップ{ci+1}: 音声ファイルの参照がありません "
                f"(ネストシーケンス・マルチカム等はカットの基準にできません)")
            print(f"  WARNING: {skipped_reasons[-1]}")
            continue
        if not os.path.exists(audio_file):
            skipped_reasons.append(
                f"{label}クリップ{ci+1}: 音声ファイルが見つかりません "
                f"({audio_file}) — 素材の移動・リンク切れの可能性")
            print(f"  WARNING: {skipped_reasons[-1]}")
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
        # in/out はクリップ自身のrate単位なので、素材内の時刻もそのrateで割る
        clip_fps = clip.get('clip_fps') or timebase
        in_sec = clip['in_sec']
        dur_sec = (clip['out_frame'] - clip['in_frame']) / clip_fps
        # 解析窓を実メディア長でクランプ（窓が実体を超過して末尾を取りこぼすのを防ぐ安全網）
        if media_dur is not None:
            max_dur = media_dur - in_sec
            if max_dur > 0 and dur_sec > max_dur + 0.5:
                print(f"    ⚠ 解析窓 {in_sec + dur_sec:.1f}s が実メディア長 {media_dur:.1f}s を超過 → クランプ")
                dur_sec = max_dur
        print(f"  {label} クリップ{ci+1}: {fname}")
        if real_fps and abs(real_fps - timebase) > 0.01:
            print(f"    実fps={real_fps:.4f}（宣言timebase={timebase:.4f}）→ 換算は宣言timebase基準（音声は実時間配置）")
        print(f"    解析範囲: {in_sec:.2f}s ～ {in_sec + dur_sec:.2f}s ({dur_sec:.1f}s)")

        # 前後を同一基準で検出（中央窓RMSエンベロープの閾値dB交差）。
        # silencedetectは減衰する発話末尾の検出が約1f遅れ前後非対称になるため使わない。
        # 抽出失敗は AudioAnalysisError で上がる (無音0箇所へ倒さない)。
        try:
            silences, levels = detect_silence_envelope(
                audio_file, in_sec, dur_sec, threshold_db, min_silence)
        except AudioAnalysisError as exc:
            # 音声抽出の失敗は必ず止める。1クリップでも取り落とすと、
            # その区間は「無音なし=全部残す」になり結果が静かに間違う。
            print(f"\nERROR: 音声の抽出に失敗しました ({label}クリップ{ci+1})")
            print(f"  〖症状〗{exc}")
            print("  〖なぜ〗ffmpegが素材の音声を読み出せませんでした "
                  "(コーデック未対応・ファイル破損・アクセス権・素材の差し替え)")
            print("  〖次の一手〗①Premiereで該当クリップが正常に再生できるか確認"
                  " ②`ffmpeg -i \"<素材のパス>\"` をターミナルで実行してエラー内容を確認"
                  " ③ffmpegを最新版へ更新 (Mac: brew upgrade ffmpeg /"
                  " Windows: winget upgrade Gyan.FFmpeg)")
            print(f"[診断] 音声抽出に失敗したトラック: {label}")
            sys.exit(EXIT_AUDIO_FAILED)
        analyzed_count += 1
        if levels['peakDb'] is not None:
            peak_db = (levels['peakDb'] if peak_db is None
                      else max(peak_db, levels['peakDb']))
        if levels['rmsDb'] is not None and levels['seconds'] > 0:
            energy += (10 ** (levels['rmsDb'] / 10.0)) * levels['seconds']
        seconds += levels['seconds']
        quiet_seconds += levels['quietRatio'] * levels['seconds']
        if levels['suggestDb'] is not None:
            suggest_db = (levels['suggestDb'] if suggest_db is None
                         else min(suggest_db, levels['suggestDb']))
        peak_text = ('—' if levels['peakDb'] is None
                     else f"{levels['peakDb']:.1f}dBFS")
        rms_text = ('—' if levels['rmsDb'] is None
                    else f"{levels['rmsDb']:.1f}dBFS")
        print(f"    音声レベル: peak {peak_text} / RMS {rms_text} "
              f"/ 閾値以下 {levels['quietRatio'] * 100:.1f}%")
        print(f"    検出無音: {len(silences)}箇所")

        # 検出秒（素材内の実時間）→ タイムラインframe。
        # 素材内の経過時間 (s - in_sec) をシーケンスtimebaseで刻み、クリップの
        # タイムライン開始位置へ足す。素材側の単位 (clip_fps) はin_secに畳んで
        # あるため、ここは常にシーケンス基準の整数フレームになる。
        for s_start, s_end in silences:
            tf_start = max(
                clip['tl_start'] + int(round((s_start - in_sec) * timebase)),
                clip['tl_start'])
            tf_end = min(
                clip['tl_start'] + int(round((s_end - in_sec) * timebase)),
                clip['tl_end'])
            if tf_end - tf_start >= min_silence_frames:
                silence_intervals.append((tf_start, tf_end))

    rms_db = (10.0 * math.log10(energy / seconds)
              if energy > 0 and seconds > 0 else None)
    quiet_ratio = (quiet_seconds / seconds) if seconds > 0 else 0.0
    stats = {
        'label': label,
        'clipCount': len(track_info['clips']),
        'analyzedCount': analyzed_count,
        'skippedReasons': skipped_reasons,
        'peakDb': peak_db,
        'rmsDb': rms_db,
        'quietRatio': quiet_ratio,
        'suggestDb': suggest_db,
        'seconds': seconds,
        'energy': energy,
        'quietSeconds': quiet_seconds,
    }
    return merge_intervals(silence_intervals), stats


DEFAULT_TRACKS = ("A1",)  # 後方互換の既定値 (従来のA1専用挙動と完全に一致させる)
_TRACK_LABEL_RE = re.compile(r"^A\d+$")


def parse_track_labels(raw):
    """--tracks の生文字列 (カンマ区切り) を正規化したラベルのタプルへ変換する。

    大文字化・空白除去・重複除去 (順序は保持)。空文字列や不正形式 (A1以外の
    "A<数字>" でないもの) は呼び出し元の argparse エラーとして扱えるよう
    ValueError を送出する。
    """
    labels = []
    seen = set()
    for token in (raw or "").split(","):
        label = token.strip().upper()
        if not label:
            continue
        if not _TRACK_LABEL_RE.match(label):
            raise ValueError(
                f"--tracks の指定が不正です: '{token.strip()}' "
                f"(A1, A2 のような形式で指定してください)")
        if label not in seen:
            seen.add(label)
            labels.append(label)
    if not labels:
        raise ValueError("--tracks に有効なトラックが1つも指定されていません")
    return tuple(labels)


def main():
    import argparse
    parser = argparse.ArgumentParser(
        description="音声トラックベース 無音カット（全トラック同期編集点）",
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
    parser.add_argument("--tracks", default=",".join(DEFAULT_TRACKS),
                        help="無音判定に使うオーディオトラックをカンマ区切りで指定 (例: A1,A2)。"
                             "指定した全トラックが同時に無音の区間だけをカットする"
                             "(どれか1本でも音が鳴っていれば残す＝積集合)。"
                             "既定は A1 のみ (従来と同じ挙動)")
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
    parser.add_argument("--allow-no-cut", action="store_true",
                        help="カット箇所が0件でも、そのままXMLを出力して正常終了する。"
                             "既定はエラー終了 (無音が1件も見つからないのに"
                             "「カット済み」シーケンスを作ると、原因不明のまま"
                             "未カットの中身が出来上がるため)")
    parser.add_argument("--no-fit-scale", action="store_true",
                        help="素材解像度がシーケンスと異なるクリップへの自動フィット"
                             "スケール付与を無効化 (「フレームサイズに合わせる」フラグは"
                             "XMLに保存されないため、既定では自動付与して見た目を保つ)")
    args = parser.parse_args()

    try:
        TRACK_LABELS = parse_track_labels(args.tracks)
    except ValueError as exc:
        print(f"ERROR: {exc}")
        sys.exit(2)

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

    print("=" * 60)
    print("音声トラックベース 無音カット（全トラック同期）")
    print("=" * 60)
    print(f"入力: {input_xml}")
    print(f"出力: {output_xml}")
    print(f"判定トラック: {'+'.join(TRACK_LABELS)}"
          f"{' (複数トラックの積集合＝全て同時に無音の区間だけカット)' if len(TRACK_LABELS) > 1 else ''}")
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

    # ── 選択トラックの無音検出 ──
    base_track_label = '+'.join(TRACK_LABELS)
    print(f"\n[3/4] {base_track_label} の音声で無音検出...")

    tracks_by_label = {t['label']: t for t in tracks}
    existing_labels = [l for l in TRACK_LABELS if l in tracks_by_label]
    missing_labels = [l for l in TRACK_LABELS if l not in tracks_by_label]
    single_track_mode = len(TRACK_LABELS) == 1

    if not existing_labels:
        print(f"ERROR: 指定したトラック ({base_track_label}) が見つかりません"
              " (クリップが1つも乗っていません)")
        sys.exit(1)
    if single_track_mode and missing_labels:
        # 後方互換: 従来の「A1トラックが見つかりません」と完全に同じ扱い
        # (単一トラック選択時は積集合の概念が無く、そのトラックが無ければ
        # 判定材料そのものが無い)。
        print(f"ERROR: {missing_labels[0]}トラックが見つかりません")
        sys.exit(1)
    for label in missing_labels:
        # 複数トラック選択時のみ到達する。クリップが無い=音が鳴りようがない
        # ので「無音」として扱う (「データが無い＝判定不能」と取り違えない)。
        # 積集合上は制約を課さない (他の選択トラックの判定がそのまま通る)。
        print(f"  WARNING: {label}: クリップが1つもありません"
              " → 全区間を無音として扱います (このトラックはカットの妨げになりません)")

    # タイムライン全体の範囲 (実在する選択トラックの和集合)
    tl_total_start = min(
        max(0, min(c['tl_start'] for c in tracks_by_label[l]['clips']))
        for l in existing_labels)
    tl_total_end = max(
        max(c['tl_end'] for c in tracks_by_label[l]['clips'])
        for l in existing_labels)
    tl_duration = tl_total_end - tl_total_start

    per_track_stats = []       # トラックごとの診断値 (複数トラック時のみ出力)
    track_silence_sets = []    # 各トラックの無音区間 (積集合の入力)
    all_skipped_reasons = []
    total_analyzed = 0
    total_clip_count = 0
    combined_peak_db = None
    combined_energy = 0.0
    combined_seconds = 0.0
    combined_quiet_seconds = 0.0
    combined_suggest_db = None

    for label in TRACK_LABELS:
        if label in missing_labels:
            per_track_stats.append({
                'label': label, 'missing': True, 'clipCount': 0,
                'analyzedCount': 0, 'skippedReasons': [],
                'peakDb': None, 'rmsDb': None, 'quietRatio': None,
            })
            track_silence_sets.append([(tl_total_start, tl_total_end)])
            continue

        track_info = tracks_by_label[label]
        clip_silence, stats = analyze_track_silence(
            track_info, timebase, THRESHOLD_DB, MIN_SILENCE,
            min_silence_frames, probe_cache)

        # このトラックの音声を1つも解析できていないなら、ここで止める。
        # 従来はWARNINGを出して続行し「無音0箇所 = カット無し」のXMLを正常出力して
        # いたため、利用者には「新シーケンスはできたが未カット」としか見えなかった。
        # 複数トラック選択時にこれを黙って「常に無音」へ倒すと、実際には解析
        # できていないのに判定に使えたかのように見えてしまう (fail-closed)。
        if stats['analyzedCount'] == 0:
            print(f"\nERROR: トラック {label} の音声を1クリップも解析できませんでした")
            print(f"  〖なぜ〗{label}の全クリップで音声ファイルを読めませんでした:")
            for reason in stats['skippedReasons']:
                print(f"    - {reason}")
            print(f"  〖次の一手〗①{label}トラックに音声クリップ(素材の音声)が乗っているか確認"
                  " ②素材のリンク切れ(?マーク)がないか確認"
                  f" ③ネスト/マルチカムのクリップは解除して素材を直接{label}へ置く")
            print(f"[診断] 解析不能トラック: {label}")
            sys.exit(EXIT_AUDIO_FAILED)

        per_track_stats.append(stats)
        total_analyzed += stats['analyzedCount']
        total_clip_count += stats['clipCount']
        all_skipped_reasons.extend(stats['skippedReasons'])
        if stats['peakDb'] is not None:
            combined_peak_db = (stats['peakDb'] if combined_peak_db is None
                                else max(combined_peak_db, stats['peakDb']))
        combined_energy += stats['energy']
        combined_seconds += stats['seconds']
        combined_quiet_seconds += stats['quietSeconds']
        if stats['suggestDb'] is not None:
            combined_suggest_db = (stats['suggestDb'] if combined_suggest_db is None
                                   else min(combined_suggest_db, stats['suggestDb']))

        if single_track_mode:
            # 後方互換: 従来通りクリップ内で検出した無音のみを使う
            # (トラック全体に対する隙間の無音合成はしない＝完全に同じ結果)。
            track_silence_sets.append(clip_silence)
        else:
            # 「トラックにクリップが乗っていない区間」も無音として明示的に扱う
            # (音が無いので当然だが、判定不能と取り違えやすいため明示処理する)。
            coverage = _clip_coverage_intervals(
                track_info['clips'], tl_total_start, tl_total_end)
            gaps = invert_intervals(coverage, tl_total_start, tl_total_end)
            track_silence_sets.append(merge_intervals(list(clip_silence) + gaps))

    # 積集合: 選択した全トラックが同時に無音の区間だけを最終的な無音とする
    # (単一トラック選択時は積集合が1本だけなので、従来と完全に同じ結果になる)。
    all_silence_tl_frames = intersect_all(track_silence_sets) if track_silence_sets else []

    # パディング適用 → カット区間
    cut_regions = []
    for tf_start, tf_end in all_silence_tl_frames:
        cs = tf_start + PADDING_FRAMES
        ce = tf_end - PADDING_FRAMES
        if ce > cs:
            cut_regions.append((cs, ce))

    # ── 診断値 (毎回必ず出す) ──────────────────────────────────────
    # 「カットされない」の切り分けに必要な数字を全部ログへ残す。
    # 行頭タグ [診断] は呼び出し元 (cut_job.py) が機械的に読み取る契約。
    # 単一トラック選択時は全ての値が従来のA1専用実装と完全に同じ計算になる
    # (pool対象が1トラックだけになるため)。
    silence_total_frames = sum(e - s for s, e in all_silence_tl_frames)
    combined_rms_db = (10.0 * math.log10(combined_energy / combined_seconds)
                       if combined_energy > 0 and combined_seconds > 0 else None)
    combined_quiet_ratio = (
        combined_quiet_seconds / combined_seconds if combined_seconds > 0 else 0.0)
    print(f"[診断] 基準トラック: {base_track_label} / クリップ {total_clip_count}本"
          f" (解析 {total_analyzed} / スキップ {len(all_skipped_reasons)})")
    print(f"[診断] 使用した設定: 閾値 {THRESHOLD_DB}dB / 最小無音 {MIN_SILENCE}s"
          f" ({min_silence_frames}f) / パディング {PADDING_FRAMES}f")
    print(f"[診断] 音声レベル実測: peak"
          f" {'—' if combined_peak_db is None else f'{combined_peak_db:.1f}dBFS'} / RMS"
          f" {'—' if combined_rms_db is None else f'{combined_rms_db:.1f}dBFS'}"
          f" / 閾値以下の割合 {combined_quiet_ratio * 100:.1f}%")
    if len(TRACK_LABELS) > 1:
        # 複数トラック選択時のみ、積集合の根拠が追えるようトラック別の実測値を出す。
        for stats in per_track_stats:
            label = stats['label']
            if stats.get('missing'):
                print(f"[診断] トラック{label}: クリップなし → 全区間を無音として扱います")
                continue
            peak_text = ('—' if stats['peakDb'] is None
                        else f"{stats['peakDb']:.1f}dBFS")
            rms_text = ('—' if stats['rmsDb'] is None
                       else f"{stats['rmsDb']:.1f}dBFS")
            print(f"[診断] トラック{label}: peak {peak_text} / RMS {rms_text}"
                  f" / 閾値以下 {stats['quietRatio'] * 100:.1f}%"
                  f" (解析 {stats['analyzedCount']} / スキップ {len(stats['skippedReasons'])})")
    print(f"[診断] 検出した無音: {len(all_silence_tl_frames)}箇所"
          f" / 合計 {silence_total_frames / timebase:.2f}秒")
    print(f"[診断] カット区間: {len(cut_regions)}箇所")
    # 実測エンベロープから「この値まで上げれば最小無音長の無音が必ず1つ成立する」
    # 閾値を出す。1dBの余裕を足し、パネルの入力範囲 (-80〜-10) へ丸める。
    recommend_db = None
    if combined_suggest_db is not None:
        recommend_db = max(-80, min(-10, int(math.ceil(combined_suggest_db + 1))))
        print(f"[診断] 無音を検出できる閾値の目安: {recommend_db}dB"
              f" (実測エンベロープ最小 {combined_suggest_db:.1f}dB)")
    # 一部だけ解析できなかった場合、その区間は無音ゼロ扱い = カットされない。
    # 全滅なら上で停止しているので、ここは「部分的に取りこぼした」の可視化。
    for reason in all_skipped_reasons:
        print(f"[注意] {reason} → この区間はカットされません")

    # カット区間が1つも無いなら「成功」にしない (沈黙の失敗の根治)。
    if not cut_regions:
        print("\nERROR: カットする箇所が1つもありませんでした")
        if not all_silence_tl_frames:
            print(f"  〖なぜ〗{base_track_label}の音声から、閾値 {THRESHOLD_DB}dB 以下が"
                  f" {MIN_SILENCE}秒以上続く区間を検出できませんでした"
                  f" (実測: peak"
                  f" {'—' if combined_peak_db is None else f'{combined_peak_db:.1f}dBFS'} / RMS"
                  f" {'—' if combined_rms_db is None else f'{combined_rms_db:.1f}dBFS'})")
            hint = (f"①無音とみなす音量を {recommend_db}dB へ上げる"
                    f" (実測に基づく目安。パネルの「無音とみなす音量 (dB)」)"
                    if recommend_db is not None
                    else "①無音とみなす音量を上げる (-48 → -40 → -35)")
            print(f"  〖次の一手〗{hint}"
                  " ②最小無音長を短くする"
                  f" ③カットしたい無音が{base_track_label}の音声に入っているか確認"
                  " (BGMや環境音が常に鳴っていると無音になりません。BGM/音楽トラックを"
                  "判定対象に選ぶと常に「音がある」判定になり何もカットされなくなります)")
        else:
            print(f"  〖なぜ〗無音は {len(all_silence_tl_frames)}箇所"
                  f" 検出しましたが、前後に残す量 (パディング {PADDING_FRAMES}f×2 ="
                  f" {PADDING_FRAMES * 2}f) が無音の長さを上回るため、"
                  "カット区間が残りませんでした")
            print(f"  〖次の一手〗①前後に残す量を減らす (推奨: 最小無音長"
                  f" {min_silence_frames}f の半分未満 = {max(0, min_silence_frames // 2 - 1)}f 以下)"
                  " ②最小無音長を長くする")
        if not args.allow_no_cut:
            print("  (--allow-no-cut を付けると、カット0件でもそのまま出力します)")
            sys.exit(EXIT_NO_CUT)
        print("  --allow-no-cut 指定のため、カットせずそのまま出力します")

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
    # link解決の統計 (2026-07-27 実機報告「カット後にオーディオチャンネル割り当てが
    # 壊れる」の再発防止用)。dangling=リンク先クリップが丸ごとカットされ消滅した
    # (想定内)。no_exact_match=リンク先トラックにクリップは残っているが位置が
    # 完全一致しない (想定外・危険信号)。後者は必ず警告として出す。
    dropped_links_dangling = 0
    dropped_links_no_exact_match = 0
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
            #
            # 2026-07-27 実機報告「ピンマイク2本を別チャンネルに録った素材で
            # カット後にオーディオチャンネル割り当てが壊れる」の修正:
            # 以前は完全一致する対応クリップが無い場合、「最も重なりが大きい
            # クリップ」へ近似フォールバックしていた。これは別のステレオ振り分け
            # ペア (例: 別トラックの別マイク用クリップ) を誤って同一リンクグループへ
            # 繋いでしまう恐れがあり、Premiereインポート時に
            # 「本来のペアが孤立し (どのソースにも繋がらない)、無関係な
            # クリップ同士が誤って繋がる」形でオーディオチャンネル表示が
            # 壊れる (Modify Clip > Audio Channels が「カスタム」化し、
            # 割り当てが入れ替わる/消える)。真にリンクされたステレオ展開ペア
            # (currentExplodedTrackIndex) は同じキープ区間から生成されるため、
            # 正常な入力なら常に完全一致する。完全一致が無い＝入力側の時点で
            # ペアの境界が食い違っている (カット処理の責任範囲外) ので、
            # 近似せずリンクを除去し、警告として明示する
            # (チャンネル配線に関わる情報は、保持できないなら黙って作り直さない)。
            for link in new_clip.findall('link'):
                linkref = link.find('linkclipref')
                if linkref is None or linkref.text not in old_id_to_track:
                    # リンク先クリップが丸ごとカットされて消滅した (想定内)。
                    new_clip.remove(link)
                    dropped_links_dangling += 1
                    continue
                other_track_idx = old_id_to_track[linkref.text]
                # 同じタイムライン位置に完全一致する対応クリップだけを対応付ける
                # (近似フォールバックはしない)。
                target_sub_idx = find_matching_sub_idx(
                    new_clips_per_track[other_track_idx],
                    nc['new_tl_start'], nc['new_tl_end']
                )
                if target_sub_idx is None:
                    new_clip.remove(link)
                    dropped_links_no_exact_match += 1
                    continue
                linkref.text = new_id_map[(other_track_idx, target_sub_idx)]
                clipindex_elem = link.find('clipindex')
                if clipindex_elem is not None:
                    clipindex_elem.text = str(target_sub_idx + 1)

            track_elem.append(new_clip)

    # オーディオ/ビデオの<link>が完全一致で解決できなかった件数を警告する。
    # 通常のステレオ振り分け(音声チャンネルマッピング)クリップは常に完全一致
    # するため、ここが1件でもあれば「入力側で既にリンクペアの境界が食い違って
    # いた」ことを意味し、そのクリップのチャンネル割り当てがPremiere上で
    # 意図通りに再現されない可能性がある (黙って近似しない代わりに、
    # ユーザーが実機で確認すべき箇所として明示する)。
    if dropped_links_no_exact_match:
        print(f"  WARNING: リンク{dropped_links_no_exact_match}件で対応するクリップの"
              "位置が完全一致せず、リンクを除去しました"
              " (元の素材でリンク済みクリップ同士の境界が食い違っていた可能性があります。"
              "ステレオ振り分け・オーディオチャンネルマッピングを使ったクリップは"
              "Premiereで Modify Clip > Audio Channels の割り当てを確認してください)")

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
    """タイムライン位置が完全一致する対応クリップのインデックスを返す。

    2026-07-27: 以前あった「完全一致が無ければ最も重なりが大きいクリップへ
    近似する」フォールバックは意図的に削除した。真にリンクされた
    ステレオ展開ペア (音声チャンネル振り分け) は同じキープ区間の同じ位置に
    生成されるため常に完全一致し、近似が必要になることはない。近似は
    「別のペアの別クリップ」を誤って同一グループへリンクし、Premiereの
    オーディオチャンネル割り当て (Modify Clip > Audio Channels) を壊す
    リスクがある。完全一致が無ければ None を返し、呼び出し元でリンクを
    除去・警告させる (チャンネル配線に関わる対応付けを推測しない)。
    """
    for idx, nc in enumerate(other_new_clips):
        if nc['new_tl_start'] == tl_start and nc['new_tl_end'] == tl_end:
            return idx
    return None


if __name__ == '__main__':
    main()
