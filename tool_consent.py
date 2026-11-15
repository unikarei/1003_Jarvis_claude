"""会話のツールの初回承認。許可は道具・権限版ごとに保存する（上位 spec §3.4.12 J）。"""

import argparse
import json
import os
import tempfile
import threading
from pathlib import Path

import config

PATH = Path(os.environ.get("JARVIS_TOOL_CONSENT_PATH", str(config.BASE_DIR / "data" / "tool-consent.json")))
# 安全性に関わる実装・権限の変更時は該当ツールの版を上げる。
VERSIONS = {name: 1 for name in ("knowledge_search", "memory_recall", "memory_note", "jarvis_status",
                                "web_search", "web_fetch")}
_lock = threading.RLock()


def _read():
    try:
        value = json.loads(PATH.read_text(encoding="utf-8"))
        if not isinstance(value, dict) or value.get("schema") != 1 or not isinstance(value.get("tools"), dict):
            return {}
        return {k: v for k, v in value["tools"].items()
                if k in VERSIONS and type(v) is int and v == VERSIONS[k]}
    except (OSError, ValueError):
        return {}


def _write(tools):
    PATH.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=".tool-consent-", dir=PATH.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as out:
            json.dump({"schema": 1, "tools": tools}, out)
            out.flush()
            os.fsync(out.fileno())
        os.replace(tmp, PATH)
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)


def approved(name):
    with _lock:
        return name in VERSIONS and _read().get(name) == VERSIONS[name]


def grant(name):
    with _lock:
        tools = _read()
        tools[name] = VERSIONS[name]
        _write(tools)


def revoke(name=None):
    with _lock:
        tools = _read()
        if name is None:
            tools.clear()
        else:
            tools.pop(name, None)
        _write(tools)


def main():
    parser = argparse.ArgumentParser(description="Jarvis のツールの初回承認を表示・取り消す")
    parser.add_argument("--revoke", choices=[*VERSIONS, "all"])
    args = parser.parse_args()
    if args.revoke:
        revoke(None if args.revoke == "all" else args.revoke)
    for name in VERSIONS:
        print(f"{name}: {'承認済み' if approved(name) else '未承認'}")


if __name__ == "__main__":
    main()
