"""エージェント・APIルーティングの判断履歴（エスカレーション・サブエージェント委譲）を記録する。"""

import json
from datetime import datetime

from config import BASE_DIR

LOG_PATH = BASE_DIR / "jarvis_agent.log"


def log_event(event_type: str, **fields) -> None:
    """1件のイベントをJSON Lines形式で jarvis_agent.log に追記する。"""
    record = {"timestamp": datetime.now().isoformat(), "event": event_type, **fields}
    with open(LOG_PATH, "a", encoding="utf-8") as f:
        f.write(json.dumps(record, ensure_ascii=False) + "\n")
