"""ChatGPT の公式エクスポートの conversations.json を読む。

各会話の mapping（ノード ID → {message, parent, children}）を current_node から parent へたどり、
逆順にしたものを会話の本筋とする（表示されていない分岐の枝を含めない）。
"""

from pathlib import Path

from ckc.models import Conversation, Origin, Speaker, Turn, new_id
from ckc.parsers import ParseResult, as_key, as_str, load_conversation_list, unix_to_iso

_ROLES = {"user": Speaker.OWNER, "assistant": Speaker.AI}


def _start_node(mapping: dict, current: object) -> str | None:
    """たどり始めるノード。current_node がなければ children が空のノードのうち最後のもの。"""
    if isinstance(current, str) and current in mapping:
        return current
    leaves = [k for k, v in mapping.items() if isinstance(v, dict) and not v.get("children")]
    return leaves[-1] if leaves else None


def _branch(mapping: dict, start: str) -> list[dict]:
    """start からルートまでたどり、ルート側から並べたノードを返す（循環していても止まる）。"""
    nodes: list[dict] = []
    seen: set[str] = set()
    node_id = start
    while isinstance(node_id, str) and node_id in mapping and node_id not in seen:
        seen.add(node_id)
        node = mapping[node_id]
        if not isinstance(node, dict):
            break
        nodes.append(node)
        node_id = node.get("parent")
    return nodes[::-1]


def _turn(node: dict) -> Turn | None:
    """ノードを発言にする。ユーザー・AI 以外の発言、空の発言は None。"""
    message = node.get("message")
    if not isinstance(message, dict):
        return None
    author = message.get("author")
    speaker = _ROLES.get(as_key(author.get("role"))) if isinstance(author, dict) else None
    if speaker is None:
        return None
    content = message.get("content")
    parts = content.get("parts") if isinstance(content, dict) else None
    if not isinstance(parts, list):
        return None
    text = "\n".join(p for p in parts if isinstance(p, str)).strip()
    if not text:
        return None
    return Turn(speaker, text, unix_to_iso(message.get("create_time")))


def parse_chatgpt_export(path: Path, captured_at: str) -> ParseResult:
    """ChatGPT の conversations.json を Conversation の一覧にする。"""
    result = ParseResult([], [])
    for i, item in enumerate(load_conversation_list(path)):
        if not isinstance(item, dict):
            result.warnings.append(f"ChatGPT の {i} 件目: 会話の形式ではないため飛ばしました")
            continue
        external_id = as_str(item.get("id")) or as_str(item.get("conversation_id"))
        mapping = item.get("mapping")
        if external_id is None or not isinstance(mapping, dict):
            missing = "id" if external_id is None else "mapping"
            result.warnings.append(f"ChatGPT の {i} 件目: {missing} がないため飛ばしました")
            continue
        occurred_at = unix_to_iso(item.get("create_time"))
        if occurred_at is None:
            result.warnings.append(f"ChatGPT の会話 {external_id}: create_time がないか不正なため飛ばしました")
            continue
        start = _start_node(mapping, item.get("current_node"))
        turns = [t for t in (_turn(n) for n in _branch(mapping, start)) if t] if start else []
        if not turns:
            result.warnings.append(f"ChatGPT の会話 {external_id}: 発言がないため飛ばしました")
            continue
        result.conversations.append(Conversation(
            new_id("conv"), Origin.CHATGPT, as_str(item.get("title")) or "無題", turns, captured_at,
            occurred_at, external_id))
    return result
