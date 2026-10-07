"""Google Analytics Data API（GA4）からアクセスデータを取得する（サービスアカウント認証）。"""

from google.analytics.data_v1beta import BetaAnalyticsDataClient
from google.analytics.data_v1beta.types import DateRange, Dimension, Metric, RunReportRequest
from google.oauth2 import service_account

from config import GA4_PROPERTY_ID, GOOGLE_APPLICATION_CREDENTIALS

METRICS = [Metric(name="activeUsers"), Metric(name="sessions"), Metric(name="screenPageViews")]


def _get_client() -> BetaAnalyticsDataClient:
    credentials = service_account.Credentials.from_service_account_file(GOOGLE_APPLICATION_CREDENTIALS)
    return BetaAnalyticsDataClient(credentials=credentials)


def get_ga4_summary(days: int = 7) -> dict:
    """直近days日分のアクセスデータ（合計と日別のユーザー数・セッション数・ページビュー）を取得する。"""
    client = _get_client()
    date_range = [DateRange(start_date=f"{days}daysAgo", end_date="today")]
    property_path = f"properties/{GA4_PROPERTY_ID}"

    totals_resp = client.run_report(RunReportRequest(
        property=property_path, metrics=METRICS, date_ranges=date_range,
    ))
    totals_row = totals_resp.rows[0] if totals_resp.rows else None
    total_users = int(totals_row.metric_values[0].value) if totals_row else 0
    total_sessions = int(totals_row.metric_values[1].value) if totals_row else 0
    total_pageviews = int(totals_row.metric_values[2].value) if totals_row else 0

    daily_resp = client.run_report(RunReportRequest(
        property=property_path,
        dimensions=[Dimension(name="date")],
        metrics=METRICS,
        date_ranges=date_range,
    ))
    daily = [
        {
            "date": row.dimension_values[0].value,
            "active_users": int(row.metric_values[0].value),
            "sessions": int(row.metric_values[1].value),
            "page_views": int(row.metric_values[2].value),
        }
        for row in daily_resp.rows
    ]

    return {
        "property_id": GA4_PROPERTY_ID,
        "days": days,
        "total_active_users": total_users,
        "total_sessions": total_sessions,
        "total_page_views": total_pageviews,
        "daily": daily,
    }
