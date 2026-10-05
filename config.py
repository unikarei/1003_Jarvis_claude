"""JARVIS 設定ファイル。RSSフィード一覧やパス設定をまとめる。フィード追加はここに追記する。"""

import os
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent
DB_PATH = BASE_DIR / "jarvis.db"
SCHEMA_PATH = BASE_DIR / "schema.sql"
REPORTS_DIR = BASE_DIR / "reports"

ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY")
SUMMARY_MODEL = os.environ.get("JARVIS_SUMMARY_MODEL", "claude-sonnet-5")

CATEGORY_NEWS = "news"

RSS_FEEDS = [
    {"name": "TechCrunch AI", "url": "https://techcrunch.com/category/artificial-intelligence/feed/", "category": CATEGORY_NEWS},
    {"name": "MIT Technology Review", "url": "https://www.technologyreview.com/feed/", "category": CATEGORY_NEWS},
    {"name": "ITmedia AI+", "url": "https://rss.itmedia.co.jp/rss/2.0/aiplus.xml", "category": CATEGORY_NEWS},
]
