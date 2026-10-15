"""RSS の取り込みの重複排除（T-803）のテスト。feedparser はモック。"""

from types import SimpleNamespace

import db
import fetch_news


def _feed(entries):
    return SimpleNamespace(entries=[{"link": u, "title": t, "summary": "s"} for u, t in entries])


def test_same_title_with_new_url_is_skipped(db_path, monkeypatch):
    feed = {"name": "観光経済", "url": "https://x/rss", "category": "minpaku"}
    monkeypatch.setattr(fetch_news.feedparser, "parse", lambda url: _feed([("https://x/a-2", "記事  A"), ("https://x/b", "記事 B")]))
    assert len(fetch_news.fetch_feed(feed)) == 2
    monkeypatch.setattr(fetch_news.feedparser, "parse", lambda url: _feed([("https://x/a-2-2", "記事 A"), ("https://x/c", "記事 C")]))
    new = fetch_news.fetch_feed(feed)
    assert [db.get_node(i)["title"] for i in new] == ["記事 C"]   # URL が変わっても同じ題名はスキップ


def test_same_title_in_other_category_is_kept(db_path, monkeypatch):
    monkeypatch.setattr(fetch_news.feedparser, "parse", lambda url: _feed([("https://x/a", "同じ題名")]))
    assert len(fetch_news.fetch_feed({"name": "n", "url": "u", "category": "news"})) == 1
    monkeypatch.setattr(fetch_news.feedparser, "parse", lambda url: _feed([("https://y/a", "同じ題名")]))
    assert len(fetch_news.fetch_feed({"name": "n", "url": "u", "category": "trading"})) == 1


def test_stored_title_with_ideographic_space_is_matched(db_path, monkeypatch):
    """空白をそろえる前に保存した題名（全角スペース）とも一致させる（2026-10-16 の再配信のすり抜け）。"""
    db.insert_node(node_type="Source", category="minpaku", title="セミナー。\u3000村上  晴", content="", url="https://x/old")
    monkeypatch.setattr(fetch_news.feedparser, "parse", lambda url: _feed([("https://x/old-2", "セミナー。\u3000村上 晴")]))
    assert fetch_news.fetch_feed({"name": "観光経済", "url": "u", "category": "minpaku"}) == []
