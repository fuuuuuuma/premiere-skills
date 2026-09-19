#!/usr/bin/env python3
"""Jev（TypeSafe AI の System One Model）を Cloudflare Workers AI 経由で呼ぶ最小クライアント。

Jev は文章を書かない。state（判断材料のテキスト/JSON）と questions を渡すと、
質問ごとに型の決まった答えを確率つきで返す。
  noul   … はい/いいえ。{"noul": 0.93}（1 に近いほど「はい」）
  choice … criteria のキーから1つ。{"choice": "retake", "probabilities": {...}, "confidence": 0.8}
  score  … 順序のある段階の加重平均。{"score": 1.3, "probabilities": {...}, "confidence": 0.5}

この部品は Cut & SRT・premiere-skills に同じ内容でコピーして使う（配布物が別々のため）。
正本はこのファイル。コピー先との食い違いは check_copies.py で検知する。

認証情報の探し場所（先に見つかったもの）:
  1. ask(..., credentials=(account_id, api_token)) で直接渡す（Cut & SRT はパネル設定から渡す）
  2. 環境変数 JEV_CF_ACCOUNT_ID / JEV_CF_API_TOKEN
  3. ~/.config/jev/credentials（KEY=VALUE の行）
CLOUDFLARE_API_TOKEN は使わない。同じ名前にすると wrangler のデプロイがこのトークンで動いてしまう。

失敗はすべて JevError で返す。呼び出し側は今の方法（ルール・既存の LLM）へ戻すこと。
Python 3.10+ / 標準ライブラリのみ。
"""

from __future__ import annotations

import json
import os
import time
import urllib.error
import urllib.request

MODEL = "typesafe/jev"
ENDPOINT = "https://api.cloudflare.com/client/v4/accounts/{account_id}/ai/run"
CREDENTIALS_FILE = os.path.expanduser("~/.config/jev/credentials")
ENV_ACCOUNT = "JEV_CF_ACCOUNT_ID"
ENV_TOKEN = "JEV_CF_API_TOKEN"
DEFAULT_TIMEOUT = 15.0
# 429（混雑）・5xx・529（過負荷）だけ待って再試行する。4xx の入力エラーは再試行しても同じ。
RETRY_STATUSES = {429, 500, 502, 503, 504, 529}


class JevError(Exception):
    """Jev を使えなかった。status は HTTP ステータス（通信前の失敗は None）。"""

    def __init__(self, message: str, status: int | None = None):
        super().__init__(message)
        self.status = status


def load_credentials(path: str = CREDENTIALS_FILE) -> tuple[str, str] | None:
    """(account_id, api_token) を返す。どこにも無ければ None。"""
    account = os.environ.get(ENV_ACCOUNT, "").strip()
    token = os.environ.get(ENV_TOKEN, "").strip()
    if account and token:
        return account, token
    try:
        with open(path, "r", encoding="utf-8") as handle:
            lines = handle.read().splitlines()
    except OSError:
        return None
    values: dict[str, str] = {}
    for line in lines:
        key, sep, value = line.partition("=")
        if sep:
            values[key.strip()] = value.strip().strip('"').strip("'")
    account = values.get(ENV_ACCOUNT, "")
    token = values.get(ENV_TOKEN, "")
    return (account, token) if account and token else None


def is_configured() -> bool:
    return load_credentials() is not None


def _post(url: str, token: str, body: dict, timeout: float) -> dict:
    request = urllib.request.Request(
        url,
        data=json.dumps(body, ensure_ascii=False).encode("utf-8"),
        headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as error:
        detail = error.read().decode("utf-8", "replace")[:500]
        error.close()
        raise JevError(f"HTTP {error.code}: {detail}", status=error.code) from error
    except (urllib.error.URLError, TimeoutError, OSError) as error:
        raise JevError(f"通信できません: {error}") from error
    except ValueError as error:
        raise JevError(f"応答が JSON ではありません: {error}") from error


def _extract(payload: dict) -> dict:
    """Cloudflare の包みを外して {"answers", "usage", "model"} を返す。

    実測（2026-09-19）の形は二重包み:
      {"success": true, "result": {"state": "Completed", "result": {"model", "answers", "usage"}, "gatewayMetadata"}}
    """
    if isinstance(payload, dict) and payload.get("success") is False:
        raise JevError(f"Cloudflare がエラーを返しました: {json.dumps(payload.get('errors'), ensure_ascii=False)[:500]}")
    result = payload
    for _ in range(3):
        if not isinstance(result, dict) or isinstance(result.get("answers"), dict):
            break
        state = result.get("state")
        if state is not None and state != "Completed":
            raise JevError(f"Jev の処理が終わっていません（state={state}）")
        result = result.get("result")
    if not isinstance(result, dict) or not isinstance(result.get("answers"), dict):
        raise JevError(f"想定外の応答です: {json.dumps(payload, ensure_ascii=False)[:500]}")
    return {"answers": result["answers"], "usage": result.get("usage") or {}, "model": result.get("model")}


def ask(
    state,
    questions: dict,
    *,
    credentials: tuple[str, str] | None = None,
    timeout: float = DEFAULT_TIMEOUT,
    retries: int = 2,
) -> dict:
    """Jev に質問して {"answers", "usage", "latency_s"} を返す。失敗は JevError。

    同じ state への質問は1回にまとめて渡すこと（公式の推奨。呼び出し回数と料金が減る）。
    """
    creds = credentials or load_credentials()
    if not creds:
        raise JevError(
            f"Jev の認証情報がありません（環境変数 {ENV_ACCOUNT}/{ENV_TOKEN} か {CREDENTIALS_FILE}）"
        )
    account, token = creds
    body = {"model": MODEL, "input": {"state": state, "questions": questions}}
    url = ENDPOINT.format(account_id=account)
    started = time.monotonic()
    for attempt in range(retries + 1):
        try:
            payload = _post(url, token, body, timeout)
            break
        except JevError as error:
            # status が None は通信の失敗（時間切れを含む）。まれに1回だけ極端に遅い応答があるので
            # （2026-09-19 実測: 57回中1回が23秒）、短い timeout で打ち切って送り直すのが速い
            retryable = error.status is None or error.status in RETRY_STATUSES
            if not retryable or attempt == retries:
                raise
            time.sleep(0.5 * (2 ** attempt))
    result = _extract(payload)
    result["latency_s"] = round(time.monotonic() - started, 3)
    return result


def noul(answer: dict) -> float:
    """noul の答えから「はい」の確率を取り出す。"""
    return float(answer["noul"])


def choice(answer: dict) -> tuple[str, float]:
    """choice の答えから (選ばれたキー, 確信度) を取り出す。"""
    return str(answer["choice"]), float(answer.get("confidence", 0.0))
