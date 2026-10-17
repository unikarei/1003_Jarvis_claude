"""Claude（claude.ai）の公式エクスポートの conversations.json を読む。"""

from pathlib import Path

from ckc.models import Conversation, Origin, Speaker, Turn, new_id
from ckc.parsers import ParseResult, as_str, load_conversation_list

_SENDERS = {"human": Speaker.OWNER, "assistant": Speaker.AI}


def _message_text(message: dict) -> str:
    """text が空なら、content の type が text の要素の text をつなげて使う。"""
    text = message.get("text")
    if isinstance(text, str) and text.strip():
        return text.strip()
    content = message.get("content")
    if not isinstance(content, list):
        return ""
    return "\n".join(c["text"] for c in content
                     if isinstance(c, dict) and c.get("type") == "text"
                     and isinstance(c.get("text"), str)).strip()


def _turn(message: object) -> Turn | None:
    """発言にする。ユーザー・AI 以外の発言、空の発言は None。"""
    if not isinstance(message, dict):
        return None
    speaker = _SENDERS.get(message.get("sender"))
    if speaker is None:
        return None
    text = _message_text(message)
    if not text:
        return None
    return Turn(speaker, text, as_str(message.get("created_at")))


def parse_claude_export(path: Path, captured_at: str) -> ParseResult:
    """Claude の conversations.json を Conversation の一覧にする。"""
    result = ParseResult([], [])
    for i, item in enumerate(load_conversation_list(path)):
        if not isinstance(item, dict):
            result.warnings.append(f"Claude の {i} 件目: 会話の形式ではないため飛ばしました")
            continue
        external_id = as_str(item.get("uuid"))
        messages = item.get("chat_messages")
        if external_id is None or not isinstance(messages, list):
            missing = "uuid" if external_id is None else "chat_messages"
            result.warnings.append(f"Claude の {i} 件目: {missing} がないため飛ばしました")
            continue
        turns = [t for t in (_turn(m) for m in messages) if t]
        if not turns:
            result.warnings.append(f"Claude の会話 {external_id}: 発言がないため飛ばしました")
            continue
        result.conversations.append(Conversation(
            new_id("conv"), Origin.CLAUDE, as_str(item.get("name")) or "無題", turns, captured_at,
            as_str(item.get("created_at")), external_id))
    return result
