"""ChatGPT・Claude の公式のデータエクスポート（conversations.json）を Conversation に変換する。

エクスポートの形式は公開された仕様ではなく変わることがあるため、未知の項目は無視し、
必要な項目がない会話は飛ばして警告に残す（../docs/spec.md §3.3.6）。ZIP の展開は呼び出し側が行う。
"""

import json
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from ckc.models import Conversation


@dataclass
class ParseResult:
    conversations: list[Conversation]
    warnings: list[str] = field(default_factory=list)


def load_conversation_list(path: Path) -> list:
    """conversations.json を読む。全体が会話の配列でなければ ValueError。"""
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    if not isinstance(data, list):
        raise ValueError(f"{path} は会話の配列ではありません")
    return data


def unix_to_iso(value) -> str | None:
    """UNIX 時刻（秒、小数あり）を ISO 8601（UTC）にする。数値でなければ None。"""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    try:
        return datetime.fromtimestamp(value, timezone.utc).isoformat(timespec="seconds")
    except (OverflowError, OSError, ValueError):
        return None


def as_str(value) -> str | None:
    """空でない文字列ならそのまま、それ以外は None。"""
    return value if isinstance(value, str) and value.strip() else None


from ckc.parsers.chatgpt_export import parse_chatgpt_export  # noqa: E402
from ckc.parsers.claude_export import parse_claude_export  # noqa: E402

__all__ = ["ParseResult", "parse_chatgpt_export", "parse_claude_export"]
