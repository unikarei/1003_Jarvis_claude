"""Insight Reporter（1004）の API クライアント（MAP Phase 5、../docs/architecture.md §13）。

接続先は Architecture Registry の `api-insight-reporter` の `base_url`（/api/v1 の根元）。Registry を読めないときは
config の既定値を使う。1004 の内部関数を import しない（API だけで連携する、JARVIS-MAP-002 §8.1）。

エラーは2種類に分ける:
- ApiUnavailable: つながらない・タイムアウト（1004 が止まっている）。呼び出し側はファイルの読み取りなどに戻れる。
- ApiError: つながったが失敗した（4xx・5xx・JSON でない応答）。status に HTTP の状態コードが入る。
"""

import os

import requests

import config
import registry

RESOURCE_ID = "api-insight-reporter"
TIMEOUT_SECONDS = 10


class ApiUnavailable(Exception):
    pass


class ApiError(Exception):
    def __init__(self, message: str, status: int | None = None):
        super().__init__(message)
        self.status = status


def base_url() -> str:
    data = registry.load()
    url = ((data or {}).get("resources", {}).get(RESOURCE_ID) or {}).get("base_url")
    return (url or config.INSIGHT_REPORTER_API).rstrip("/")


def _headers() -> dict:
    token = os.environ.get("INSIGHT_REPORTER_API_TOKEN")
    return {"Authorization": f"Bearer {token}"} if token else {}


def _get(url: str):
    try:
        res = requests.get(url, headers=_headers(), timeout=TIMEOUT_SECONDS)
    except (requests.ConnectionError, requests.Timeout) as e:
        raise ApiUnavailable(f"Insight Reporter に接続できません（{type(e).__name__}）: {url}") from None
    if res.status_code >= 400:
        raise ApiError(f"Insight Reporter が {res.status_code} を返しました: {url}", res.status_code)
    try:
        return res.json()
    except ValueError:
        raise ApiError(f"Insight Reporter の応答が JSON ではありません: {url}", res.status_code) from None


def health() -> dict:
    """/health（/api/v1 の外。base_url の /api/v1 を取り除いた根元から呼ぶ）。"""
    root = base_url().removesuffix("/v1").removesuffix("/api")
    return _get(f"{root}/health")


def list_reports() -> list[dict]:
    return _get(f"{base_url()}/reports")


def latest_report(report_id: str) -> dict | None:
    """最新のレポート。まだ生成されていなければ None。"""
    try:
        return _get(f"{base_url()}/reports/{report_id}/latest")
    except ApiError as e:
        if e.status == 404:
            return None
        raise
