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


def test_one_failing_feed_does_not_stop_the_others(db_path, monkeypatch, capsys):
    """1 つのフィードの接続が切れても、ほかのフィードは取得する（2026-10-18 の日次の失敗）。"""
    import http.client
    import fetch_news
    db.init_db()
    feeds = [{"name": "壊れた", "url": "https://broken.example/rss", "category": "news"},
             {"name": "正常", "url": "https://ok.example/rss", "category": "news"}]
    monkeypatch.setattr(fetch_news, "RSS_FEEDS", feeds)

    def parse(url):
        if "broken" in url:
            raise http.client.RemoteDisconnected("Remote end closed connection without response")
        return type("P", (), {"entries": [{"link": "https://ok.example/a", "title": "記事", "summary": "本文"}]})()
    monkeypatch.setattr(fetch_news.feedparser, "parse", parse)
    ids = fetch_news.fetch_all()
    assert len(ids) == 1
    assert "フィードを取得できませんでした: 壊れた" in capsys.readouterr().err
