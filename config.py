"""JARVIS 設定ファイル。RSSフィード一覧やパス設定をまとめる。フィード追加はここに追記する。"""

import os
from pathlib import Path

from dotenv import load_dotenv

BASE_DIR = Path(__file__).resolve().parent
load_dotenv(BASE_DIR / ".env")

DB_PATH = BASE_DIR / "jarvis.db"
SCHEMA_PATH = BASE_DIR / "schema.sql"
REPORTS_DIR = BASE_DIR / "reports"

ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY")

# コード実行サブエージェント用（Phase 4以降で使用。CLAUDE.md「エージェント・APIルーティング方針」参照）
OPENAI_API_KEY = os.environ.get("OPENAI_API_KEY")

# コスト最適化方針（CLAUDE.md参照）: 要約は定型タスクのため軽量レイヤー（Haiku）をデフォルトとする。
# 必要になれば JARVIS_SUMMARY_MODEL で高精度レイヤーに切り替え可能。
SUMMARY_MODEL = os.environ.get("JARVIS_SUMMARY_MODEL", "claude-haiku-4-5-20251001")

# 階層型エスカレーション（CLAUDE.md参照）の高精度レイヤー。軽量レイヤーで検証失敗時、または
# 呼び出し側がforce_escalate=Trueを指定した場合のみ使用する。
ESCALATION_MODEL = os.environ.get("JARVIS_ESCALATION_MODEL", "claude-sonnet-5-5")

# ①ピアノ教室: アクセス分析用（Search Console / GA4、サービスアカウント認証）
GOOGLE_APPLICATION_CREDENTIALS = os.environ.get("GOOGLE_APPLICATION_CREDENTIALS")
SEARCH_CONSOLE_SITE_URL = os.environ.get("SEARCH_CONSOLE_SITE_URL")
GA4_PROPERTY_ID = os.environ.get("GA4_PROPERTY_ID")

# クエリ分類用キーワード（部分一致）。地域クエリ・ブランド（教室名）クエリの判定に使う。
PIANO_LOCAL_KEYWORDS = ["水戸", "茨城", "偕楽園"]
PIANO_BRAND_KEYWORDS = ["小野ピアノ", "小野"]

# GA4イベント名の分類用キーワード（部分一致）。問い合わせ・SNS遷移系イベントの発見に使う
# （イベント名は discover_ga4_events() で実データから取得し、ここでは推測で決め打ちしない）。
PIANO_INQUIRY_EVENT_KEYWORDS = ["contact", "form", "phone", "line", "email", "cta", "chatbot", "map", "reserve", "trial", "experience"]
PIANO_SOCIAL_EVENT_KEYWORDS = ["instagram", "youtube", "ameblo", "facebook", "twitter", "x_click"]

# 外部プロジェクト（1004_Insight_Reporter、参照専用）との連携設定。
# 定期情報収集は対応するレポート種別についてはInsight Reporterを上位ソースとし、
# 不足時（レポート不在・古い・フィールド欠落）のみJARVIS独自に取得する。
INSIGHT_REPORTER_DIR = BASE_DIR.parent / "1004_Insight_Reporter"
REPORT_CATEGORY_MAP = {"piano_growth": "piano"}
MAX_REPORT_AGE_DAYS = 10  # 週次レポート想定。これを超えると「不足」扱いでフォールバックする

CATEGORY_NEWS = "news"
CATEGORY_TRADING = "trading"
CATEGORY_REALESTATE = "realestate"
CATEGORY_MANUFACTURING = "manufacturing"
CATEGORY_MINPAKU = "minpaku"

RSS_FEEDS = [
    {"name": "TechCrunch AI", "url": "https://techcrunch.com/category/artificial-intelligence/feed/", "category": CATEGORY_NEWS},
    {"name": "MIT Technology Review", "url": "https://www.technologyreview.com/feed/", "category": CATEGORY_NEWS},
    {"name": "ITmedia AI+", "url": "https://rss.itmedia.co.jp/rss/2.0/aiplus.xml", "category": CATEGORY_NEWS},
    # ③仮想取引（株・FX）
    {"name": "Investing.com 日本", "url": "https://jp.investing.com/rss/news.rss", "category": CATEGORY_TRADING},
    {"name": "ITmedia ビジネス", "url": "https://rss.itmedia.co.jp/rss/2.0/business.xml", "category": CATEGORY_TRADING},
    # ④不動産
    {"name": "SUUMOジャーナル", "url": "https://suumo.jp/journal/feed/", "category": CATEGORY_REALESTATE},
    {"name": "住宅産業新聞", "url": "https://www.housenews.jp/feed", "category": CATEGORY_REALESTATE},
    # ⑤製造業
    {"name": "ITmedia MONOist", "url": "https://rss.itmedia.co.jp/rss/2.0/monoist.xml", "category": CATEGORY_MANUFACTURING},
    # ⑥民泊
    {"name": "Airstair", "url": "https://airstair.jp/rss.xml", "category": CATEGORY_MINPAKU},
    {"name": "観光経済新聞", "url": "https://www.kankokeizai.com/feed/", "category": CATEGORY_MINPAKU},
]
