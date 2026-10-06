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

CATEGORY_NEWS = "news"

RSS_FEEDS = [
    {"name": "TechCrunch AI", "url": "https://techcrunch.com/category/artificial-intelligence/feed/", "category": CATEGORY_NEWS},
    {"name": "MIT Technology Review", "url": "https://www.technologyreview.com/feed/", "category": CATEGORY_NEWS},
    {"name": "ITmedia AI+", "url": "https://rss.itmedia.co.jp/rss/2.0/aiplus.xml", "category": CATEGORY_NEWS},
]
