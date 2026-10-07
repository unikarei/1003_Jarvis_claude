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
