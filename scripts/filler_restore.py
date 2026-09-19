"""一律に消している語（もう・はい・まあ）のうち、意味を持つ箇所だけ字幕用の文字起こしに戻す（Jev・任意機能）。

vendor の whisper_to_srt.remove_fillers は「もう」「はい」「まあ」を正規表現で消す。「もう少し」「もう一回」を
守る先読みはあるが、Whisper の語ごとに当てるため「もう」「ちょっと」が別の語に分かれると効かない。
実データ（2026-09-19・対談1本）では消えた「もう」26件のうち6件が意味を持っていた:
  「ここの部分もうちょっと伸ばして」→「ここの部分ちょっと伸ばして」（同型4件）
  「この説立証もう一回」→「この説立証一回」／「編集者に頼もうってなる」→「編集者に頼ってなる」
Jev に前後の文脈つきで「消してよいか」を聞くと、基準0.5で6件すべてを守り、消せる語40件のうち35件は消せた。

ここでは転写そのもの・キャッシュの仕組みには触れず、転写直後のクリーン版 segments に
「消してはいけない」と判定された語だけを戻す。判定できなかった語は今までどおり消す。
"""

from __future__ import annotations

AMBIGUOUS = {"もう", "はい", "まあ"}
# Cut & SRT (server/) と premiere-skills (scripts/) に同じ内容で置く。正本は Cut & SRT 側。
# 食い違いは ~/ClaudeCode/projects/常時運用/jev/check_copies.py で検知する
CONTEXT_CHARS = 15
_PLACEHOLDER = "\ue000{}\ue001"  # 私用領域の文字（目に見えない）。フィラーの正規表現にも数字にも当たらない


def _corrected_words(w2s, raw_segs: list[dict]) -> list[tuple[int, int, str]]:
    flat = []
    for seg_index, seg in enumerate(raw_segs):
        for word_index, word in enumerate(seg.get("words") or []):
            text = str((word.get("word") if isinstance(word, dict) else "") or "").strip()
            flat.append((seg_index, word_index, w2s.apply_corrections(text)))
    return flat


def find_candidates(w2s, raw_segs: list[dict]) -> list[tuple[int, int, str, str, str]]:
    """丸ごと消される紛らわしい語を (区間番号, 語番号, 語, 前の文脈, 後の文脈) で返す。"""
    flat = _corrected_words(w2s, raw_segs)
    candidates = []
    for k, (seg_index, word_index, text) in enumerate(flat):
        if text in AMBIGUOUS and not w2s.remove_fillers(text):
            before = "".join(t for _, _, t in flat[max(0, k - 10):k])[-CONTEXT_CHARS:]
            after = "".join(t for _, _, t in flat[k + 1:k + 11])[:CONTEXT_CHARS]
            candidates.append((seg_index, word_index, text, before, after))
    return candidates


def _rebuild_text(w2s, raw_text: str, raw_words: list[dict], kept: set[int]) -> tuple[str, set[int]] | None:
    """kept の語を守ってクリーン版の本文を作り直す。本文の中で語の位置を特定できた番号も返す。"""
    corrected = w2s.apply_corrections(str(raw_text or "").strip())
    spans, cursor = [], 0
    for word_index, word in enumerate(raw_words):
        text = w2s.apply_corrections(str((word.get("word") if isinstance(word, dict) else "") or "").strip())
        if not text:
            continue
        found = corrected.find(text, cursor)
        if found < 0:
            continue
        if word_index in kept:
            spans.append((found, found + len(text), word_index))
        cursor = found + len(text)
    if not spans:
        return None
    protected = corrected
    for n, (start, end, _) in enumerate(reversed(spans)):
        protected = protected[:start] + _PLACEHOLDER.format(len(spans) - 1 - n) + protected[end:]
    cleaned = w2s.remove_fillers(protected)
    for n, (start, end, _) in enumerate(spans):
        cleaned = cleaned.replace(_PLACEHOLDER.format(n), corrected[start:end])
    return cleaned, {word_index for _, _, word_index in spans}


def restore(clean_segs: list[dict], raw_segs: list[dict], w2s, judge) -> tuple[list[dict], int]:
    """judge: [(前, 語, 後)] -> [消してよいか (True/False) or None]。戻した語の数も返す。

    クリーン版は区間の start/end で生保全版と対応づける（vendor の _clean_and_raw_entries は
    同じ区間から両方を作る）。区間ごと消えた（全部フィラーだった）区間には戻さない。
    """
    candidates = find_candidates(w2s, raw_segs)
    if not candidates:
        return clean_segs, 0
    try:
        verdicts = judge([(before, text, after) for _, _, text, before, after in candidates])
    except Exception:  # noqa: BLE001 — 判定役が落ちたら今までどおり（消したまま）
        return clean_segs, 0
    keep: dict[int, set[int]] = {}
    for (seg_index, word_index, *_), verdict in zip(candidates, verdicts):
        if verdict is False:
            keep.setdefault(seg_index, set()).add(word_index)
    clean_by_span = {(seg.get("start"), seg.get("end")): seg for seg in clean_segs}
    restored = 0
    for seg_index, kept in keep.items():
        raw = raw_segs[seg_index]
        clean = clean_by_span.get((raw.get("start"), raw.get("end")))
        if clean is None:
            continue
        raw_words = raw.get("words") or []
        rebuilt = _rebuild_text(w2s, raw.get("text", ""), raw_words, kept)
        if rebuilt is None:
            continue
        text, placed = rebuilt
        words = []
        for word_index, word in enumerate(raw_words):
            corrected = w2s.apply_corrections(str(word.get("word") or "").strip())
            cleaned = corrected if word_index in placed else w2s.remove_fillers(corrected)
            if cleaned:
                words.append({"word": cleaned, "start": word["start"], "end": word["end"]})
        clean["text"] = text
        clean["words"] = words
        restored += len(placed)
    return clean_segs, restored


# ── Jev への質問（消してよいか）────────────────────────────────────────
# テスト: 実データ46件（2026-09-19）。基準0.5で、消すと意味が変わる「もう」6件をすべて守り、
# 消せる語40件のうち35件は消せた
DELETE_THRESHOLD = 0.5
_DELETE_QUESTION = {
    "type": "noul",
    "instructions": (
        "Japanese talk-video transcript for subtitles. Can the word in 【】 be deleted as a filler, hesitation, "
        "backchannel, or pure emphasis without changing the meaning of the sentence?"
    ),
    "criteria": {
        "true": "Yes: it is a filler or emphasis (e.g. もう as 'just/really', はい as a backchannel, まあ as hesitation); "
                "the sentence means the same without it.",
        "false": "No: it carries meaning (もうちょっと = a little more, もう一回 = once more, もう = already in context) "
                 "or it is part of another word such as the verb 頼もう; deleting it changes or breaks the sentence.",
    },
}


def delete_verdicts(contexts: list[tuple[str, str, str]], creds=None, concurrency: int = 16) -> list[bool | None]:
    """(前の文脈, 語, 後の文脈) ごとに、消してよいか (True/False)。判定できなかった語は None。"""
    from concurrent.futures import ThreadPoolExecutor

    import jev_client

    creds = creds or jev_client.load_credentials()
    if not creds or not contexts:
        return [None] * len(contexts)

    def one(context):
        before, word, after = context
        try:
            result = jev_client.ask(f"{before}【{word}】{after}", {"q": _DELETE_QUESTION},
                                    credentials=creds, timeout=4.0)
            return jev_client.noul(result["answers"]["q"]) >= DELETE_THRESHOLD
        except (jev_client.JevError, KeyError, TypeError, ValueError):
            return None

    with ThreadPoolExecutor(max_workers=min(concurrency, len(contexts))) as pool:
        return list(pool.map(one, contexts))
