"""Google Analytics Data API（GA4）からアクセスデータを取得する（サービスアカウント認証）。"""

from google.analytics.data_v1beta import BetaAnalyticsDataClient
from google.analytics.data_v1beta.types import DateRange, Dimension, Metric, OrderBy, RunReportRequest
from google.oauth2 import service_account

from config import GA4_PROPERTY_ID, GOOGLE_APPLICATION_CREDENTIALS

METRICS = [Metric(name="activeUsers"), Metric(name="sessions"), Metric(name="screenPageViews")]

REPORT_METRICS = [
    Metric(name="activeUsers"),
    Metric(name="sessions"),
    Metric(name="screenPageViews"),
    Metric(name="newUsers"),
    Metric(name="engagementRate"),
    Metric(name="averageSessionDuration"),
]


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


def get_ga4_report(days: int = 7) -> dict:
    """直近days日と直前days日を比較したアクセスレポートを取得する
    （activeUsers/sessions/screenPageViews/newUsers/engagementRate/averageSessionDuration）。
    """
    client = _get_client()
    date_ranges = [
        DateRange(start_date=f"{days}daysAgo", end_date="today", name="current"),
        DateRange(start_date=f"{days * 2}daysAgo", end_date=f"{days + 1}daysAgo", name="previous"),
    ]

    # 複数date_rangesを指定すると、GA4 APIが各行の先頭にdateRange名を自動的に付与する
    # （dimensionsには明示的に含めない。含めると400エラーになる）。
    response = client.run_report(RunReportRequest(
        property=f"properties/{GA4_PROPERTY_ID}",
        metrics=REPORT_METRICS,
        date_ranges=date_ranges,
    ))

    periods = {}
    for row in response.rows:
        periods[row.dimension_values[0].value] = {
            "active_users": int(row.metric_values[0].value),
            "sessions": int(row.metric_values[1].value),
            "page_views": int(row.metric_values[2].value),
            "new_users": int(row.metric_values[3].value),
            "engagement_rate": float(row.metric_values[4].value),
            "avg_session_duration": float(row.metric_values[5].value),
        }

    return {
        "property_id": GA4_PROPERTY_ID,
        "days": days,
        "current": periods.get("current", {}),
        "previous": periods.get("previous", {}),
    }


def get_ga4_top_pages(days: int = 7, limit: int = 20) -> list[dict]:
    """ページ別のページビュー・ユーザー数を取得する（ページビュー降順）。"""
    client = _get_client()
    response = client.run_report(RunReportRequest(
        property=f"properties/{GA4_PROPERTY_ID}",
        dimensions=[Dimension(name="pagePath")],
        metrics=[Metric(name="screenPageViews"), Metric(name="activeUsers")],
        date_ranges=[DateRange(start_date=f"{days}daysAgo", end_date="today")],
        order_bys=[OrderBy(metric=OrderBy.MetricOrderBy(metric_name="screenPageViews"), desc=True)],
        limit=limit,
    ))
    return [
        {
            "page_path": row.dimension_values[0].value,
            "page_views": int(row.metric_values[0].value),
            "active_users": int(row.metric_values[1].value),
        }
        for row in response.rows
    ]


def get_ga4_traffic_sources(days: int = 7, limit: int = 20) -> list[dict]:
    """流入元（セッションソース・メディア）別のセッション数を取得する。"""
    client = _get_client()
    response = client.run_report(RunReportRequest(
        property=f"properties/{GA4_PROPERTY_ID}",
        dimensions=[Dimension(name="sessionSource"), Dimension(name="sessionMedium")],
        metrics=[Metric(name="sessions")],
        date_ranges=[DateRange(start_date=f"{days}daysAgo", end_date="today")],
        order_bys=[OrderBy(metric=OrderBy.MetricOrderBy(metric_name="sessions"), desc=True)],
        limit=limit,
    ))
    return [
        {
            "source": row.dimension_values[0].value,
            "medium": row.dimension_values[1].value,
            "sessions": int(row.metric_values[0].value),
        }
        for row in response.rows
    ]


def discover_ga4_events(days: int = 28, limit: int = 50) -> list[dict]:
    """実際に発生しているイベント名と件数を取得する（問い合わせ系イベント名を推測で決め打ちしないため）。"""
    client = _get_client()
    response = client.run_report(RunReportRequest(
        property=f"properties/{GA4_PROPERTY_ID}",
        dimensions=[Dimension(name="eventName")],
        metrics=[Metric(name="eventCount")],
        date_ranges=[DateRange(start_date=f"{days}daysAgo", end_date="today")],
        order_bys=[OrderBy(metric=OrderBy.MetricOrderBy(metric_name="eventCount"), desc=True)],
        limit=limit,
    ))
    return [
        {"event_name": row.dimension_values[0].value, "event_count": int(row.metric_values[0].value)}
        for row in response.rows
    ]
