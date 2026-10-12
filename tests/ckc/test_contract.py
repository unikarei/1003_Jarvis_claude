"""CKC のデータ型の契約テスト（T-103）。../docs/tasks.md §1.3 と一致していることを確かめる。

Codex が並行して実装する T-104〜T-106 はこの契約に依存する。契約を変えるときは tasks.md と
このテストを同時に変え、Codex に知らせる。
"""

import dataclasses
import re

from ckc import models as m


def _fields(cls):
    return [(f.name, f.default if f.default is not dataclasses.MISSING else
             ("factory" if f.default_factory is not dataclasses.MISSING else "required"))
            for f in dataclasses.fields(cls)]


def test_enum_values():
    assert [k.value for k in m.Kind] == ["Decision", "Preference", "Hypothesis", "Finding", "Task", "Question"]
    assert [s.value for s in m.Speaker] == ["owner", "ai"]
    assert [c.value for c in m.Confidence] == ["confirmed", "unverified"]
    assert [s.value for s in m.CandidateStatus] == ["pending", "approved", "rejected", "published"]
    assert [s.value for s in m.NoteStatus] == ["active", "superseded"]
    assert [o.value for o in m.Origin] == ["jarvis", "paste", "file", "chatgpt", "claude"]


def test_enums_are_str():
    assert m.Kind("Decision") == "Decision"
    assert m.Speaker.OWNER == "owner"


def test_dataclass_fields():
    assert _fields(m.Turn) == [("speaker", "required"), ("text", "required"), ("timestamp", None)]
    assert _fields(m.Conversation) == [
        ("conversation_id", "required"), ("origin", "required"), ("title", "required"),
        ("turns", "required"), ("captured_at", "required"), ("occurred_at", None), ("external_id", None),
    ]
    assert _fields(m.Evidence) == [("quote", "required"), ("speaker", "required")]
    assert _fields(m.SourceRef) == [
        ("origin", "required"), ("conversation_id", "required"), ("title", "required"),
        ("captured_at", "required"), ("occurred_at", None),
    ]
    assert _fields(m.Candidate) == [
        ("candidate_id", "required"), ("conversation_id", "required"), ("kind", "required"),
        ("title", "required"), ("body", "required"), ("projects", "required"), ("category", "required"),
        ("confidence", "required"), ("evidence", "required"), ("status", m.CandidateStatus.PENDING),
        ("resource_id", None), ("similar", "factory"),
    ]
    assert _fields(m.KnowledgeNote) == [
        ("ckc_id", "required"), ("kind", "required"), ("status", "required"), ("title", "required"),
        ("body", "required"), ("projects", "required"), ("category", "required"), ("confidence", "required"),
        ("source", "required"), ("evidence", "required"), ("created", "required"), ("approved", "required"),
        ("supersedes", "factory"), ("superseded_by", "factory"),
    ]


def test_new_id_format():
    assert re.fullmatch(r"conv_\d{8}_\d{6}_[0-9a-f]{4}", m.new_id("conv"))
    assert re.fullmatch(r"ckc_\d{8}_\d{6}_[0-9a-f]{4}", m.new_id("ckc"))


def _conv(*turns):
    return m.Conversation("conv_x", m.Origin.PASTE, "t", [m.Turn(s, t) for s, t in turns], "2026-10-12T10:00:00+09:00")


def test_text_joins_turns_with_speaker():
    conv = _conv((m.Speaker.OWNER, "  決めた \r\n"), (m.Speaker.AI, "了解"))
    assert conv.text() == "owner: 決めた\nai: 了解"


def test_content_hash_ignores_whitespace_and_newline_style():
    a = _conv((m.Speaker.OWNER, "一行目\n二行目"), (m.Speaker.AI, "返事"))
    b = _conv((m.Speaker.OWNER, "  一行目\r\n二行目\n"), (m.Speaker.AI, "返事 "))
    assert a.content_hash() == b.content_hash()


def test_content_hash_changes_with_text_or_speaker():
    base = _conv((m.Speaker.OWNER, "A"), (m.Speaker.AI, "B"))
    assert base.content_hash() != _conv((m.Speaker.OWNER, "A"), (m.Speaker.AI, "C")).content_hash()
    assert base.content_hash() != _conv((m.Speaker.AI, "A"), (m.Speaker.AI, "B")).content_hash()


def test_llm_output_example_matches_contract():
    """tasks.md §1.3 の LLM 出力例の項目が、候補の項目と対応していること。"""
    item_keys = {"kind", "title", "body", "projects", "category", "confidence", "evidence"}
    candidate_fields = {f.name for f in dataclasses.fields(m.Candidate)}
    assert item_keys <= candidate_fields
    assert {f.name for f in dataclasses.fields(m.Evidence)} == {"quote", "speaker"}
