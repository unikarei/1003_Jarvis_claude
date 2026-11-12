"""T-106 の候補検証と Owner の根拠の保護を確認する。"""

from copy import deepcopy

import pytest

from ckc.models import Confidence, Conversation, Evidence, Kind, Origin, Speaker, Turn
from ckc.schema import MAX_ITEMS, MAX_TITLE_LEN, ValidatedItem, validate_extraction


def _conversation():
    return Conversation("conv_dummy", Origin.PASTE, "検証用", [
        Turn(Speaker.OWNER, "この 方針\nを　採用する。好みも確認済み。"),
        Turn(Speaker.AI, "代替案を提案します。技術情報です。"),
    ], "2026-10-12T12:00:00+09:00")


def _item(**changes):
    item = {"kind": "Decision", "title": "  題名　", "body": "本文", "projects": ["jarvis"],
            "category": None, "confidence": "confirmed", "evidence": [{"quote": "この方針を採用する", "speaker": "owner"}]}
    item.update(changes)
    return item


def _validate(*items, conversation=None, warnings=None):
    data = {"items": list(items)}
    if warnings is not None:
        data["warnings"] = warnings
    return validate_extraction(data, conversation or _conversation(), {"jarvis", "oarai-minpaku"})


@pytest.mark.parametrize("kind", list(Kind))
def test_each_kind_passes_and_title_is_trimmed(kind):
    result = _validate(_item(kind=kind.value))
    assert not result.rejected
    assert result.items == [ValidatedItem(kind, "題名", "本文", ["jarvis"], None, Confidence.CONFIRMED,
                                         [Evidence("この方針を採用する", Speaker.OWNER)])]


@pytest.mark.parametrize("field,value", [
    ("kind", "Unknown"), ("kind", []), ("kind", None),
    ("title", ""), ("title", "　\n "), ("title", 1), ("title", "題" * (MAX_TITLE_LEN + 1)),
    ("body", ""), ("body", " \n　"), ("body", False),
    ("projects", "jarvis"), ("projects", [3]), ("projects", ["unknown"]),
    ("category", []), ("confidence", "high"), ("confidence", 1),
    ("evidence", []), ("evidence", {}), ("evidence", [None]), ("evidence", [{}]),
    ("evidence", [{"quote": "", "speaker": "owner"}]),
    ("evidence", [{"quote": "　\n ", "speaker": "owner"}]),
    ("evidence", [{"quote": 5, "speaker": "owner"}]),
    ("evidence", [{"quote": "この方針を採用する", "speaker": "system"}]),
    ("evidence", [{"quote": "存在しない引用", "speaker": "owner"}]),
    ("evidence", [{"quote": "この方針を採用する", "speaker": "ai"}]),
])
def test_each_violation_is_rejected(field, value):
    result = _validate(_item(**{field: value}))
    assert result.items == []
    assert result.rejected[0][0] == 0
    assert result.rejected[0][1]
    assert all(isinstance(reason, str) and reason for reason in result.rejected[0][1])


@pytest.mark.parametrize("field", ["kind", "title", "body", "projects", "confidence", "evidence"])
def test_missing_required_fields(field):
    item = _item()
    del item[field]
    assert _validate(item).rejected


def test_missing_category_is_treated_as_null():
    """category は null でよい（LLM が項目ごと省略した場合も同じ扱い）。"""
    item = _item()
    del item["category"]
    result = _validate(item)
    assert not result.rejected and result.items[0].category is None


@pytest.mark.parametrize("kind", ["Decision", "Preference"])
def test_ai_only_cannot_be_owner_decision_or_preference(kind):
    result = _validate(_item(kind=kind, evidence=[{"quote": "代替案を提案します", "speaker": "ai"}]))
    assert not result.items
    assert any("Owner" in reason for reason in result.rejected[0][1])


def test_ai_only_confirmed_finding_is_downgraded_with_warning():
    item = _item(kind="Finding", evidence=[{"quote": "技術情報です", "speaker": "ai"}])
    original = deepcopy(item)
    result = _validate(item, warnings=["LLM の警告"])
    assert result.items[0].confidence == Confidence.UNVERIFIED
    assert len(result.warnings) == 2
    assert result.warnings[0] == "LLM の警告"
    assert "unverified" in result.warnings[1]
    assert item == original


def test_owner_finding_remains_confirmed_and_unverified_ai_finding_has_no_warning():
    result = _validate(_item(kind="Finding"), _item(kind="Finding", confidence="unverified", evidence=[{"quote": "技術情報です", "speaker": "ai"}]))
    assert [item.confidence for item in result.items] == [Confidence.CONFIRMED, Confidence.UNVERIFIED]
    assert result.warnings == []


def test_quote_whitespace_normalization_and_same_quote_for_both_speakers():
    conv = _conversation()
    conv.turns.append(Turn(Speaker.AI, "この方針を採用する"))
    result = _validate(_item(evidence=[{"quote": " この\t方針　を\n採用する ", "speaker": "owner"}]), conversation=conv)
    assert len(result.items) == 1
    result = _validate(_item(kind="Hypothesis", evidence=[{"quote": "この方針を採用する", "speaker": "ai"}]), conversation=conv)
    assert len(result.items) == 1


def test_quote_cannot_span_multiple_turns():
    conv = _conversation()
    conv.turns = [Turn(Speaker.OWNER, "前半"), Turn(Speaker.OWNER, "後半")]
    result = _validate(_item(evidence=[{"quote": "前半後半", "speaker": "owner"}]), conversation=conv)
    assert result.rejected
    assert any("会話の中にない" in reason for reason in result.rejected[0][1])


def test_invalid_candidate_does_not_affect_valid_candidates_and_reasons_accumulate():
    result = _validate(_item(title="first"), _item(title="", body="", kind="unknown"), 4, _item(title="last"))
    assert [item.title for item in result.items] == ["first", "last"]
    assert [position for position, _ in result.rejected] == [1, 2]
    assert len(result.rejected[0][1]) >= 3


def test_multiple_evidence_requires_every_quote_to_be_valid():
    evidence = _item()["evidence"] + [{"quote": "存在しない発言", "speaker": "ai"}]
    assert _validate(_item(evidence=evidence)).rejected


def test_twenty_one_items_are_truncated_before_validation():
    items = [_item(title=str(i)) for i in range(MAX_ITEMS)] + [None]
    result = _validate(*items)
    assert [item.title for item in result.items] == [str(i) for i in range(MAX_ITEMS)]
    assert result.rejected == []
    assert len(result.warnings) == 1
    assert str(MAX_ITEMS) in result.warnings[0]


def test_boundary_title_empty_projects_and_string_category():
    result = _validate(_item(title="  " + "題" * MAX_TITLE_LEN + "　", projects=[], category="minpaku"))
    assert len(result.items[0].title) == MAX_TITLE_LEN
    assert result.items[0].projects == []
    assert result.items[0].category == "minpaku"


@pytest.mark.parametrize("data", [None, [], "text", {}, {"items": {}}, {"items": None},
    {"items": [], "warnings": None}, {"items": [], "warnings": "text"}, {"items": [], "warnings": [1]}])
def test_invalid_overall_shape_raises_value_error(data):
    with pytest.raises(ValueError):
        validate_extraction(data, _conversation(), set())


def test_empty_items_and_warning_copy():
    warnings = ["元の警告"]
    result = _validate(warnings=warnings)
    result.warnings.append("追加")
    assert warnings == ["元の警告"]
    assert result.items == result.rejected == []

@pytest.mark.parametrize('kind', ['Finding', 'Task', 'Hypothesis'])
def test_ai_only_system_state_is_rejected(kind):
    conv = _conversation()
    conv.turns.append(Turn(Speaker.AI, 'Obsidianのツールが無効で保存できません。'))
    result = _validate(_item(kind=kind, title='保存ツールの状態', body='ツールが無効なので接続を調査する',
                            evidence=[{'quote': 'Obsidianのツールが無効で保存できません。', 'speaker': 'ai'}]),
                       conversation=conv)
    assert not result.items and 'AI の発言だけ' in result.rejected[0][1][0]


def test_owner_system_report_and_investigation_remain_candidates():
    conv = _conversation()
    conv.turns.append(Turn(Speaker.OWNER, 'Obsidianへの保存ができない原因を調べて。'))
    result = _validate(_item(kind='Task', title='保存の調査',
                            evidence=[{'quote': 'Obsidianへの保存ができない原因を調べて。', 'speaker': 'owner'}]),
                       conversation=conv)
    assert len(result.items) == 1
