"""Google Search Console APIから検索パフォーマンスデータを取得する（サービスアカウント認証）。"""

from datetime import date, timedelta

from google.oauth2 import service_account
from googleapiclient.discovery import build

from config import GOOGLE_APPLICATION_CREDENTIALS, SEARCH_CONSOLE_SITE_URL

SCOPES = ["https://www.googleapis.com/auth/webmasters.readonly"]


def _get_service():
    credentials = service_account.Credentials.from_service_account_file(
        GOOGLE_APPLICATION_CREDENTIALS, scopes=SCOPES
    )
    return build("searchconsole", "v1", credentials=credentials)


def _query(start_date: str, end_date: str, dimensions: list[str] | None = None, row_limit: int = 50) -> list[dict]:
    """指定期間・ディメンションでSearch Analyticsを取得する（内部ヘルパー）。"""
    body = {"startDate": start_date, "endDate": end_date, "rowLimit": row_limit}
    if dimensions:
        body["dimensions"] = dimensions

    service = _get_service()
    response = service.searchanalytics().query(siteUrl=SEARCH_CONSOLE_SITE_URL, body=body).execute()
    rows = response.get("rows", [])

    if not dimensions:
        return rows

    return [
        {
            **{dim: row["keys"][i] for i, dim in enumerate(dimensions)},
            "clicks": row["clicks"],
            "impressions": row["impressions"],
            "ctr": row["ctr"],
            "position": row["position"],
        }
        for row in rows
    ]


def get_query_performance(start_date: str, end_date: str, row_limit: int = 50) -> list[dict]:
    """クエリ別の検索パフォーマンスを取得する。"""
    return _query(start_date, end_date, dimensions=["query"], row_limit=row_limit)


def get_page_performance(start_date: str, end_date: str, row_limit: int = 50) -> list[dict]:
    """ページ別の検索パフォーマンスを取得する。"""
    return _query(start_date, end_date, dimensions=["page"], row_limit=row_limit)


def get_device_performance(start_date: str, end_date: str) -> list[dict]:
    """デバイス別の検索パフォーマンスを取得する。"""
    return _query(start_date, end_date, dimensions=["device"], row_limit=10)


def get_search_console_report(days: int = 28, lag_days: int = 3, row_limit: int = 50) -> dict:
    """直近days日と直前days日を比較した検索パフォーマンスレポートを取得する。
    クエリ単位でclicks/impressions/ctr/positionの差分を計算する。
    """
    end_date = date.today() - timedelta(days=lag_days)
    start_date = end_date - timedelta(days=days)
    prev_end_date = start_date - timedelta(days=1)
    prev_start_date = prev_end_date - timedelta(days=days - 1)

    current_queries = get_query_performance(start_date.isoformat(), end_date.isoformat(), row_limit)
    previous_queries = get_query_performance(prev_start_date.isoformat(), prev_end_date.isoformat(), row_limit)
    previous_by_query = {q["query"]: q for q in previous_queries}

    queries = []
    for q in current_queries:
        prev = previous_by_query.get(q["query"])
        queries.append({
            **q,
            "prev_clicks": prev["clicks"] if prev else None,
            "prev_impressions": prev["impressions"] if prev else None,
            "prev_ctr": prev["ctr"] if prev else None,
            "prev_position": prev["position"] if prev else None,
        })

    current_totals = _query(start_date.isoformat(), end_date.isoformat())
    previous_totals = _query(prev_start_date.isoformat(), prev_end_date.isoformat())
    current_total = current_totals[0] if current_totals else {}
    previous_total = previous_totals[0] if previous_totals else {}

    return {
        "site_url": SEARCH_CONSOLE_SITE_URL,
        "current_period": {"start": start_date.isoformat(), "end": end_date.isoformat()},
        "previous_period": {"start": prev_start_date.isoformat(), "end": prev_end_date.isoformat()},
        "total_clicks": current_total.get("clicks", 0),
        "total_impressions": current_total.get("impressions", 0),
        "total_ctr": current_total.get("ctr", 0),
        "total_position": current_total.get("position", 0),
        "prev_total_clicks": previous_total.get("clicks", 0),
        "prev_total_impressions": previous_total.get("impressions", 0),
        "prev_total_ctr": previous_total.get("ctr", 0),
        "prev_total_position": previous_total.get("position", 0),
        "queries": queries,
    }


def get_search_console_summary(days: int = 28, lag_days: int = 3, row_limit: int = 10) -> dict:
    """直近days日分（データ反映の遅延を考慮しlag_days日前を終了日とする）の検索パフォーマンスを取得する。"""
    end_date = date.today() - timedelta(days=lag_days)
    start_date = end_date - timedelta(days=days)
    date_range = {"startDate": start_date.isoformat(), "endDate": end_date.isoformat()}

    service = _get_service()

    totals_resp = service.searchanalytics().query(
        siteUrl=SEARCH_CONSOLE_SITE_URL, body=date_range
    ).execute()
    totals_rows = totals_resp.get("rows", [])
    totals = totals_rows[0] if totals_rows else {}

    query_resp = service.searchanalytics().query(
        siteUrl=SEARCH_CONSOLE_SITE_URL,
        body={**date_range, "dimensions": ["query"], "rowLimit": row_limit},
    ).execute()
    top_queries = [
        {
            "query": row["keys"][0],
            "clicks": row["clicks"],
            "impressions": row["impressions"],
            "ctr": row["ctr"],
            "position": row["position"],
        }
        for row in query_resp.get("rows", [])
    ]

    return {
        "site_url": SEARCH_CONSOLE_SITE_URL,
        "start_date": start_date.isoformat(),
        "end_date": end_date.isoformat(),
        "total_clicks": totals.get("clicks", 0),
        "total_impressions": totals.get("impressions", 0),
        "total_ctr": totals.get("ctr", 0),
        "total_position": totals.get("position", 0),
        "top_queries": top_queries,
    }
