"""ChatGPT・Claude のエクスポートの取り込み（T-117）のテスト。fixture は架空の会話。"""

import json
import re
from pathlib import Path

import pytest

from ckc.models import Origin, Speaker
from ckc.parsers import parse_chatgpt_export, parse_claude_export

FIXTURES = Path(__file__).parent / "fixtures"
CAPTURED_AT = "2026-10-17T12:00:00+09:00"
_CONV_ID = re.compile(r"^conv_\d{8}_\d{6}_[0-9a-f]{4}$")


def test_chatgpt_converts_current_branch_only():
    result = parse_chatgpt_export(FIXTURES / "chatgpt_conversations.json", CAPTURED_AT)
    by_id = {c.external_id: c for c in result.conversations}
    assert list(by_id) == ["chatgpt-conv-001", "chatgpt-conv-003"]

    conv = by_id["chatgpt-conv-001"]
    assert _CONV_ID.match(conv.conversation_id)
    assert conv.origin == Origin.CHATGPT
    assert conv.title == "架空の旅行計画"
    assert conv.captured_at == CAPTURED_AT
    assert conv.occurred_at == "2025-10-09T08:53:20.500000+00:00"  # 小数秒を保つ
    # 表示されていない枝（n3a）・system・tool・文字列以外の parts は入らない
    assert [(t.speaker, t.text) for t in conv.turns] == [
        (Speaker.OWNER, "架空の町への旅行の候補を出して"),
        (Speaker.AI, "候補は海辺の町と山の町です"),
        (Speaker.OWNER, "海辺の町にします"),
    ]
    assert conv.turns[0].timestamp == "2025-10-09T08:53:30+00:00"


def test_chatgpt_without_current_node_starts_from_last_leaf():
    result = parse_chatgpt_export(FIXTURES / "chatgpt_conversations.json", CAPTURED_AT)
    conv = next(c for c in result.conversations if c.external_id == "chatgpt-conv-003")
    assert conv.title == "無題"
    assert [(t.speaker, t.text) for t in conv.turns] == [(Speaker.OWNER, "題名のない質問"), (Speaker.AI, "答えです")]
    assert conv.turns[1].timestamp is None


def test_chatgpt_skipped_conversations_are_warned():
    result = parse_chatgpt_export(FIXTURES / "chatgpt_conversations.json", CAPTURED_AT)
    assert len(result.warnings) == 2
    assert "mapping" in result.warnings[0]
    assert "chatgpt-conv-004" in result.warnings[1]


def test_claude_converts_conversations():
    result = parse_claude_export(FIXTURES / "claude_conversations.json", CAPTURED_AT)
    by_id = {c.external_id: c for c in result.conversations}
    assert list(by_id) == ["claude-conv-001", "claude-conv-004"]

    conv = by_id["claude-conv-001"]
    assert _CONV_ID.match(conv.conversation_id)
    assert conv.origin == Origin.CLAUDE
    assert conv.title == "架空の料理の相談"
    assert conv.captured_at == CAPTURED_AT
    assert conv.occurred_at == "2026-10-01T09:00:00.000000Z"
    # text が空なら content の text 要素をつなげる。空の発言・system は入らない
    assert [(t.speaker, t.text) for t in conv.turns] == [
        (Speaker.OWNER, "架空の野菜で作れる料理は？"),
        (Speaker.AI, "スープが作れます。\n炒め物もおすすめです。"),
        (Speaker.OWNER, "スープにします"),
    ]
    assert [t.timestamp for t in conv.turns] == [
        "2026-10-01T09:00:01.000000Z", "2026-10-01T09:00:05.000000Z", "2026-10-01T09:02:00.000000Z"]
    assert by_id["claude-conv-004"].title == "無題"


def test_claude_skipped_conversations_are_warned():
    result = parse_claude_export(FIXTURES / "claude_conversations.json", CAPTURED_AT)
    assert len(result.warnings) == 2
    assert "uuid" in result.warnings[0]
    assert "claude-conv-003" in result.warnings[1]


@pytest.mark.parametrize("parse", [parse_chatgpt_export, parse_claude_export])
@pytest.mark.parametrize("data", [{"conversations": []}, "text", None])
def test_non_list_file_raises_value_error(tmp_path, parse, data):
    path = tmp_path / "conversations.json"
    path.write_text(json.dumps(data), encoding="utf-8")
    with pytest.raises(ValueError):
        parse(path, CAPTURED_AT)


def _chatgpt_conv(conv_id, role="user", **extra):
    node = {"parent": None, "children": [],
            "message": {"author": {"role": role}, "content": {"parts": ["本文"]}}}
    return {"id": conv_id, "create_time": 1760000000, "mapping": {"a": node}, **extra}


def _claude_conv(conv_id, sender="human", **extra):
    return {"uuid": conv_id, "created_at": "2026-10-01T09:00:00Z",
            "chat_messages": [{"sender": sender, "text": "本文"}], **extra}


def _write(tmp_path, data):
    path = tmp_path / "conversations.json"
    path.write_text(json.dumps(data), encoding="utf-8")
    return path


@pytest.mark.parametrize("value", [None, "1760000000", True, 1e30])
def test_chatgpt_invalid_create_time_is_skipped_with_warning(tmp_path, value):
    bad = _chatgpt_conv("bad")
    if value is None:
        del bad["create_time"]
    else:
        bad["create_time"] = value
    result = parse_chatgpt_export(_write(tmp_path, [bad, _chatgpt_conv("ok")]), CAPTURED_AT)
    assert [c.external_id for c in result.conversations] == ["ok"]
    assert result.conversations[0].occurred_at == "2025-10-09T08:53:20+00:00"
    assert len(result.warnings) == 1 and "bad" in result.warnings[0] and "create_time" in result.warnings[0]


@pytest.mark.parametrize("value", [None, "", "昨日", 1759309200])
def test_claude_invalid_created_at_is_skipped_with_warning(tmp_path, value):
    bad = _claude_conv("bad")
    if value is None:
        del bad["created_at"]
    else:
        bad["created_at"] = value
    result = parse_claude_export(_write(tmp_path, [bad, _claude_conv("ok")]), CAPTURED_AT)
    assert [c.external_id for c in result.conversations] == ["ok"]
    assert result.conversations[0].occurred_at == "2026-10-01T09:00:00Z"
    assert len(result.warnings) == 1 and "bad" in result.warnings[0] and "created_at" in result.warnings[0]


@pytest.mark.parametrize("role", [["user"], {"name": "user"}, 1])
def test_chatgpt_non_string_role_is_excluded(tmp_path, role):
    result = parse_chatgpt_export(_write(tmp_path, [_chatgpt_conv("bad", role), _chatgpt_conv("ok")]), CAPTURED_AT)
    assert [c.external_id for c in result.conversations] == ["ok"]
    assert len(result.warnings) == 1 and "bad" in result.warnings[0]


@pytest.mark.parametrize("sender", [["human"], {"name": "human"}, 1])
def test_claude_non_string_sender_is_excluded(tmp_path, sender):
    result = parse_claude_export(_write(tmp_path, [_claude_conv("bad", sender), _claude_conv("ok")]), CAPTURED_AT)
    assert [c.external_id for c in result.conversations] == ["ok"]
    assert len(result.warnings) == 1 and "bad" in result.warnings[0]


@pytest.mark.parametrize("parse", [parse_chatgpt_export, parse_claude_export])
def test_non_dict_items_are_skipped_with_warning(tmp_path, parse):
    path = tmp_path / "conversations.json"
    path.write_text(json.dumps([1, "x"]), encoding="utf-8")
    result = parse(path, CAPTURED_AT)
    assert result.conversations == [] and len(result.warnings) == 2
