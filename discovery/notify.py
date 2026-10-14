"""定期処理の失敗の通知（T-203、../docs/architecture.md §11.5）。

Guardian がエラー（停止したジョブ・応答しない API・存在しない資産など）を見つけたら、Mac の通知センターに出す。
外部のサービスには送らない（2026-10-12 Owner 決定: 通知は status.html と Mac の通知）。同じ内容は1日1回だけ通知する。
"""

import hashlib
import json
import subprocess
from datetime import date
from pathlib import Path

import config
from discovery.guardian import Report

STATE_FILE = config.CKC_DATA_DIR.parent / "notify" / "last.json"


def run(cmd: list[str]) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, capture_output=True, text=True, timeout=10)


def _osascript_string(text: str) -> str:
    return '"' + text.replace("\\", "\\\\").replace('"', '\\"') + '"'


def notify_errors(report: Report, today: date | None = None, state_file: Path = STATE_FILE) -> bool:
    """エラーがあれば通知する。通知したら True。"""
    errors = [f for f in report.findings if f.severity == "error"]
    if not errors:
        return False
    today = (today or date.today()).isoformat()
    digest = hashlib.sha256("\n".join(f"{f.resource_id}:{f.message}" for f in errors).encode()).hexdigest()
    try:
        last = json.loads(state_file.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        last = {}
    if last.get("date") == today and last.get("digest") == digest:
        return False
    first = errors[0]
    body = f"{first.resource_id or '-'}: {first.message}" + (f"（ほか {len(errors) - 1} 件）" if len(errors) > 1 else "")
    script = (f"display notification {_osascript_string(body[:200])} "
              f"with title {_osascript_string(f'Jarvis Guardian: エラー {len(errors)} 件')} "
              f"subtitle {_osascript_string('Registry の generated/guardian-report.md を確認')}")
    run(["osascript", "-e", script])
    state_file.parent.mkdir(parents=True, exist_ok=True)
    state_file.write_text(json.dumps({"date": today, "digest": digest}), encoding="utf-8")
    return True


def notify_failure(job: str, detail: str) -> None:
    """定期ジョブそのものが失敗したことを通知する（毎回。外部のサービスには送らない）。"""
    script = (f"display notification {_osascript_string(detail[:200])} "
              f"with title {_osascript_string(f'Jarvis: {job}の失敗')} "
              f"subtitle {_osascript_string('~/Library/Logs/jarvis/ のログを確認')}")
    run(["osascript", "-e", script])
