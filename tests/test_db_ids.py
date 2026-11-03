"""ノードの ID の作り直し（同じ秒に作った ID の短縮部分が重なったとき）のテスト。DB は一時フォルダ。"""

import sqlite3

import pytest

import db


def test_generated_id_collision_is_retried(db_path, monkeypatch):
    ids = iter(["knowledge_news_20261103_000000_aaaa", "knowledge_news_20261103_000000_aaaa",
                "knowledge_news_20261103_000000_bbbb"])
    monkeypatch.setattr(db, "generate_id", lambda *a, **k: next(ids))
    first = db.insert_node("Knowledge", category="news", title="1")
    second = db.insert_node("Knowledge", category="news", title="2")
    assert first.endswith("_aaaa") and second.endswith("_bbbb")
    assert db.get_node(second)["title"] == "2"


def test_given_id_duplicate_still_fails(db_path):
    db.insert_node("Knowledge", category="news", title="1", node_id="knowledge_fixed")
    with pytest.raises(sqlite3.IntegrityError):
        db.insert_node("Knowledge", category="news", title="2", node_id="knowledge_fixed")
