"""Search Console・GA4への接続を確認する統合テストスクリプト。

実行: python scripts/test_google_integrations.py

JSONキーの中身や秘密情報は一切出力しない。接続エラーはスタックトレースではなく、
原因を分類した短いメッセージで表示する。
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from google.api_core import exceptions as google_exceptions  # noqa: E402
from googleapiclient.errors import HttpError  # noqa: E402

from config import GA4_PROPERTY_ID, GOOGLE_APPLICATION_CREDENTIALS, SEARCH_CONSOLE_SITE_URL  # noqa: E402


def check_credentials_file() -> bool:
    print("[1] Credentials")
    if not GOOGLE_APPLICATION_CREDENTIALS:
        print("[ERROR] GOOGLE_APPLICATION_CREDENTIALS が未設定です")
        print("  -> .env の GOOGLE_APPLICATION_CREDENTIALS を確認")
        return False
    if not Path(GOOGLE_APPLICATION_CREDENTIALS).is_file():
        print("[ERROR] Credential file not found")
        print("  -> GOOGLE_APPLICATION_CREDENTIALS のパスを確認")
        return False
    print("[OK] credentials file found")
    return True


def _classify_http_error(e: HttpError) -> None:
    status = e.resp.status if e.resp else None
    reason = str(e)
    if status == 403 and "accessNotConfigured" in reason:
        print("[ERROR] API disabled")
        print("  -> Google Cloud Consoleで対象APIが有効か確認")
    elif status == 403:
        print("[ERROR] Search Console permission denied")
        print("  -> Search Console の「ユーザーと権限」でサービスアカウントが登録されているか確認")
    elif status == 404:
        print("[ERROR] Invalid Search Console property")
        print("  -> SEARCH_CONSOLE_SITE_URL が Search Console 登録値と完全一致しているか確認")
    else:
        print(f"[ERROR] Search Console API error (status={status})")


def test_search_console() -> bool:
    print()
    print("[2] Search Console")
    if not SEARCH_CONSOLE_SITE_URL:
        print("[ERROR] SEARCH_CONSOLE_SITE_URL が未設定です")
        print("  -> .env の SEARCH_CONSOLE_SITE_URL を確認")
        return False

    try:
        from fetch_search_console import get_search_console_summary
        summary = get_search_console_summary()
    except HttpError as e:
        _classify_http_error(e)
        return False
    except FileNotFoundError:
        print("[ERROR] Credential file not found")
        print("  -> GOOGLE_APPLICATION_CREDENTIALS を確認")
        return False
    except Exception as e:
        print(f"[ERROR] Search Console connection failed ({type(e).__name__})")
        print("  -> 認証・API有効化・依存関係を確認")
        return False

    print("[OK] Google Search Console connection successful")
    print(f"Site: {summary['site_url']}")
    print(f"Period: {summary['start_date']} - {summary['end_date']}")
    print()
    print(f"Total clicks: {summary['total_clicks']}")
    print(f"Total impressions: {summary['total_impressions']}")
    if summary["top_queries"]:
        print()
        print("Top queries:")
        for i, q in enumerate(summary["top_queries"], 1):
            print(f"{i}. {q['query']} (clicks={q['clicks']}, impressions={q['impressions']})")
    return True


def test_ga4() -> bool:
    print()
    print("[3] GA4")
    if not GA4_PROPERTY_ID:
        print("[ERROR] GA4_PROPERTY_ID が未設定です")
        print("  -> .env の GA4_PROPERTY_ID を確認")
        return False

    try:
        from fetch_ga4 import get_ga4_summary
        summary = get_ga4_summary(days=7)
    except google_exceptions.PermissionDenied:
        print("[ERROR] GA4 permission denied")
        print("  -> GA4「プロパティのアクセス管理」でサービスアカウントが「閲覧者」として登録されているか確認")
        return False
    except google_exceptions.NotFound:
        print("[ERROR] Invalid GA4 property")
        print("  -> GA4_PROPERTY_ID を確認")
        return False
    except google_exceptions.Unauthenticated:
        print("[ERROR] GA4 authentication failed")
        print("  -> GOOGLE_APPLICATION_CREDENTIALS を確認")
        return False
    except FileNotFoundError:
        print("[ERROR] Credential file not found")
        print("  -> GOOGLE_APPLICATION_CREDENTIALS を確認")
        return False
    except Exception as e:
        print(f"[ERROR] GA4 connection failed ({type(e).__name__})")
        print("  -> 認証・API有効化・依存関係を確認")
        return False

    print("[OK] GA4 connection successful")
    print(f"Property ID: {summary['property_id']}")
    print()
    print(f"Active users: {summary['total_active_users']}")
    print(f"Sessions: {summary['total_sessions']}")
    print(f"Page views: {summary['total_page_views']}")
    return True


def main() -> None:
    print("=" * 40)
    print("JARVIS Google Integration Test")
    print("=" * 40)
    print()

    creds_ok = check_credentials_file()
    gsc_ok = test_search_console() if creds_ok else False
    ga4_ok = test_ga4() if creds_ok else False

    print()
    print("=" * 40)
    print("ALL TESTS PASSED" if (gsc_ok and ga4_ok) else "SOME TESTS FAILED")
    print("=" * 40)

    sys.exit(0 if (gsc_ok and ga4_ok) else 1)


if __name__ == "__main__":
    main()
