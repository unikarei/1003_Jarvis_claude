"""振り分けと照合（T-112）のテスト。"""

import pytest

import db
from ckc.reconcile import find_similar
from ckc.route import default_category, route_vault


@pytest.mark.parametrize("projects, expected", [
    (["oarai-minpaku"], "vault-oarai-obsidian"),
    ([], "vault-jarvis-knowledge"),
    (["jarvis"], "vault-jarvis-knowledge"),
    (["turbine-research"], "vault-jarvis-knowledge"),
    (["oarai-minpaku", "jarvis"], "vault-jarvis-knowledge"),
])
def test_route_vault(projects, expected):
    assert route_vault(projects) == expected


def test_default_category():
    assert default_category(["oarai-minpaku"]) == "minpaku"
    assert default_category(["jarvis"]) is None
    assert default_category(["oarai-minpaku", "piano"]) is None


def _knowledge(title, content, ckc_id, status="active"):
    return db.insert_node("Knowledge", category="minpaku", title=title, content=content,
                          meta={"ckc_id": ckc_id, "status": status, "kind": "Decision",
                                "note_relpath": f"_jarvis/knowledge/Decision/{ckc_id}.md",
                                "resource_id": "vault-oarai-obsidian"})


def test_find_similar_ranks_by_keyword_matches(db_path):
    _knowledge("大洗の物件の購入時期", "購入の判断は来年の春に行う", "ckc_a")
    _knowledge("大洗の物件の外構", "外構工事は業者に依頼する", "ckc_b")
    _knowledge("ピアノ教室の集客", "SNS の投稿を増やす", "ckc_c")
    db.insert_node("Knowledge", category="minpaku", title="大洗の物件の購入時期（ニュース）", content="購入 判断")
    result = find_similar("物件の購入時期", "大洗の物件の購入判断を来年にする")
    assert [r["ckc_id"] for r in result] == ["ckc_a", "ckc_b"]
    assert result[0]["note_relpath"] == "_jarvis/knowledge/Decision/ckc_a.md"


def test_find_similar_ignores_superseded_and_unrelated(db_path):
    _knowledge("大洗の物件の購入時期", "購入の判断は来年", "ckc_old", status="superseded")
    assert find_similar("物件の購入時期", "大洗の物件の購入判断") == []
    assert find_similar("天気", "今日は晴れ") == []
