"""JARVIS 設定ファイル。RSSフィード一覧やパス設定をまとめる。フィード追加はここに追記する。"""

import os
from pathlib import Path

from dotenv import load_dotenv

BASE_DIR = Path(__file__).resolve().parent
load_dotenv(BASE_DIR / ".env")

# DB・Vault・CKC のデータ領域は環境変数で差し替えられる（開発・テストで本番を使わないため。
# ../docs/spec.md §3.3.12）。未設定なら本番の既定値。
DB_PATH = Path(os.environ.get("JARVIS_DB_PATH", BASE_DIR / "jarvis.db"))
SCHEMA_PATH = BASE_DIR / "schema.sql"
REPORTS_DIR = BASE_DIR / "reports"

# Obsidian Vault 群の親フォルダ（ADR-0015）。各 Vault は VAULT_ROOT / <Vault 名>
VAULT_ROOT = Path(os.environ.get("JARVIS_VAULT_ROOT", Path.home() / "work" / "obsidian"))
# CKC の会話原文・一時ファイル・バックアップ（Git の外、ADR-0018）
CKC_DATA_DIR = Path(os.environ.get("JARVIS_CKC_DATA_DIR", BASE_DIR / "data" / "ckc"))
# 会話の遅延の記録（latency.py、T-1405）。既定は data/latency/latency.jsonl。CKC と同じ data/ の下に置き、
# テストで JARVIS_CKC_DATA_DIR を一時フォルダにしたときは一緒にそちらへ移る（本番の data/ に書かない）
MEMORY_DIR = Path(os.environ.get("JARVIS_MEMORY_DIR", CKC_DATA_DIR.parent / "memory"))

LATENCY_LOG = Path(os.environ.get("JARVIS_LATENCY_LOG", CKC_DATA_DIR.parent / "latency" / "latency.jsonl"))

# Vault とプロジェクトの正本は Architecture Registry（../jarvis-architecture-registry）。registry.py が
# generated/registry.json を読む（ADR-0019、../docs/architecture.md §6.9）。下の値は、registry.json が
# 読めないときの予備（2026-10-12 時点の Registry の内容）。
REGISTRY_JSON = Path(os.environ.get(
    "JARVIS_REGISTRY_JSON", BASE_DIR.parent / "jarvis-architecture-registry" / "generated" / "registry.json"))
HOST_ID = os.environ.get("JARVIS_HOST_ID", "host-mac-mini")  # Registry の locator を選ぶ端末
# スキルの索引（jarvis-skills の生成物、FR-014）。skills.py が読む。Registry に repo-jarvis-skills があればその場所を優先する
SKILLS_JSON = Path(os.environ.get(
    "JARVIS_SKILLS_JSON", BASE_DIR.parent / "jarvis-skills" / "generated" / "skills.json"))
FALLBACK_VAULTS = {
    "vault-oarai-obsidian": "oarai-obsidian",
    "vault-jarvis-knowledge": "jarvis-knowledge",
}
DEFAULT_VAULT = "vault-jarvis-knowledge"
# project_id → (知識の保存先 Vault, 1003 のカテゴリ)
FALLBACK_PROJECTS = {
    "jarvis": ("vault-jarvis-knowledge", None),
    "oarai-minpaku": ("vault-oarai-obsidian", "minpaku"),
    "piano": ("vault-jarvis-knowledge", "piano"),
    "turbine-research": ("vault-jarvis-knowledge", "manufacturing"),
    "physical-ai": ("vault-jarvis-knowledge", None),
    "ai-news": ("vault-jarvis-knowledge", "news"),
    "trading": ("vault-jarvis-knowledge", "trading"),
    "realestate": ("vault-jarvis-knowledge", "realestate"),
    "personal": ("vault-jarvis-knowledge", None),
}

ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY")

# コード実行サブエージェント用（Phase 4以降で使用。CLAUDE.md「エージェント・APIルーティング方針」参照）
OPENAI_API_KEY = os.environ.get("OPENAI_API_KEY")

# コスト最適化方針（CLAUDE.md参照）: 要約は定型タスクのため軽量レイヤー（Haiku）をデフォルトとする。
# 必要になれば JARVIS_SUMMARY_MODEL で高精度レイヤーに切り替え可能。
SUMMARY_MODEL = os.environ.get("JARVIS_SUMMARY_MODEL", "claude-haiku-4-5-20251001")

# 階層型エスカレーション（CLAUDE.md参照）の高精度レイヤー。軽量レイヤーで検証失敗時、または
# 呼び出し側がforce_escalate=Trueを指定した場合のみ使用する。
ESCALATION_MODEL = os.environ.get("JARVIS_ESCALATION_MODEL", "claude-sonnet-5-5")

# llm.complete() のプロバイダー（../docs/decisions.md ADR-0020）。既定は Claude Code CLI（サブスクリプション内）。
# "anthropic_api" は従量課金のため、既定にする変更は Owner の個別承認が必要。失敗時の自動切り替えはしない。
LLM_PROVIDER = os.environ.get("JARVIS_LLM_PROVIDER", "claude_cli")
CLAUDE_BIN = os.environ.get("CLAUDE_BIN", str(Path.home() / ".local" / "bin" / "claude"))
CLAUDE_CLI_TIMEOUT_SECONDS = int(os.environ.get("JARVIS_CLAUDE_CLI_TIMEOUT", "300"))
# Codex（codex exec、ChatGPT のサブスクリプション。ADR-0035、../docs/spec.md §3.4.12）
CODEX_BIN = os.environ.get("JARVIS_CODEX_BIN", "")          # 空なら PATH → VS Code の拡張機能に同梱の版を探す
CODEX_MODEL = os.environ.get("JARVIS_CODEX_MODEL", "")      # 空なら Codex の既定
CODEX_REASONING = os.environ.get("JARVIS_CODEX_REASONING", "low")
CODEX_TIMEOUT_SECONDS = int(os.environ.get("JARVIS_CODEX_TIMEOUT", "300"))
# 常駐の codex app-server（標準入出力）で答えを逐次表示する（T-2004）。0 なら codex exec だけ
CODEX_APP_SERVER = os.environ.get("JARVIS_CODEX_APP_SERVER", "1").strip() != "0"
# 会話の LLM（ハーネス）の切り替え（ADR-0035、../docs/spec.md §3.4.12）
HARNESS = os.environ.get("JARVIS_HARNESS", "auto").strip().lower() or "auto"           # auto・ollama・claude・codex
HEAVY_HARNESS = os.environ.get("JARVIS_HEAVY_HARNESS", "codex").strip().lower() or "codex"   # おまかせの経路 B の最初
HARNESS_COOLDOWN_MINUTES = float(os.environ.get("JARVIS_HARNESS_COOLDOWN_MIN", "30"))
# 声の会話（voice_chat.py・python -m voice.web）のプロセスで claude -p に渡すモデル（2026-11-03 Owner 指示、
# 最初の文字を速くする）。空にすると CLI の既定のモデル。chat.py・定期ジョブは CLI の既定のまま
VOICE_CLAUDE_MODEL = os.environ.get("JARVIS_VOICE_CLAUDE_MODEL", "claude-sonnet-5-5")
# ローカルの Ollama（llm の "ollama" プロバイダー、ADR-0027、T-1402）。無料・最初の文字まで約 0.5 秒
OLLAMA_URL = os.environ.get("JARVIS_OLLAMA_URL", "http://127.0.0.1:11434").rstrip("/")
CHAT_MODEL = os.environ.get("JARVIS_CHAT_MODEL", "gemma4:12b")
OLLAMA_TIMEOUT_SECONDS = int(os.environ.get("JARVIS_OLLAMA_TIMEOUT", "120"))
# 会話の振り分けに LLM を使うか（FR-014、T-1212）。claude -p の起動に約 3 秒かかるため既定は使わない
ROUTE_WITH_LLM = os.environ.get("JARVIS_ROUTE_WITH_LLM", "0") == "1"
# ナレッジの質問とみなす検索の点数（BM25 の最上位）。これ未満で語にも当たらなければ通常の会話（ADR-0027、T-1403）
KNOWLEDGE_SCORE_THRESHOLD = float(os.environ.get("JARVIS_KNOWLEDGE_SCORE_THRESHOLD", "8.0"))

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
INSIGHT_REPORTER_DIR = BASE_DIR.parent / "1004_insight_reporter"
# Insight Reporter の API（MAP Phase 5）。正本は Registry の api-insight-reporter.base_url。これは Registry を読めないときの予備
INSIGHT_REPORTER_API = os.environ.get("INSIGHT_REPORTER_API", "http://127.0.0.1:18080/api/v1")
REPORT_CATEGORY_MAP = {"piano_growth": "piano"}
MAX_REPORT_AGE_DAYS = 10  # 週次レポート想定。これを超えると「不足」扱いでフォールバックする

# ⑥民泊: 大洗Project OS（unikarei/oarai-project-os-since2026、private）との連携設定。
# JARVISはdata/*.json・docs/**/*.mdを読み取るのみで、直接書き換えやGitHubへの
# ステージングコミットは行わない（runtime/は同リポジトリの.gitignore対象のため）。
OARAI_PROJECT_OS_REPO = "unikarei/oarai-project-os-since2026"
# 提案の送信先。Xserver の公開サイトは静的モードで API がないため、Mac mini のローカルで動かす API に送る
# （../docs/project-os-api-restart-plan.md 案 A、2026-10-16 Owner 承認）。止まっていれば 1003 の DB に保留する。
OARAI_PROJECT_OS_API_BASE = os.environ.get("JARVIS_OARAI_API_BASE", "http://127.0.0.1:8010").rstrip("/")

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
