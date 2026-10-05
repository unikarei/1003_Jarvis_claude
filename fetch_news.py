"""RSSフィードから新着記事を取得し、Sourceノードとして保存する（URL重複は排除）。"""

import feedparser

from config import RSS_FEEDS
from db import find_source_by_url, insert_node


def fetch_feed(feed: dict) -> list[str]:
    """1フィードを取得し、新規保存したSourceノードIDの一覧を返す。"""
    parsed = feedparser.parse(feed["url"])
    new_source_ids = []
    for entry in parsed.entries:
        url = entry.get("link")
        if not url:
            continue
        if find_source_by_url(url) is not None:
            continue
        title = entry.get("title", "")
        summary = entry.get("summary", "") or entry.get("description", "")
        source_id = insert_node(
            node_type="Source",
            category=feed["category"],
            title=title,
            content=summary,
            url=url,
            meta={"feed_name": feed["name"]},
        )
        new_source_ids.append(source_id)
    return new_source_ids


def fetch_all() -> list[str]:
    """設定済みの全フィードを取得し、新規Sourceノードのidリストを返す。"""
    all_new_ids = []
    for feed in RSS_FEEDS:
        all_new_ids.extend(fetch_feed(feed))
    return all_new_ids


if __name__ == "__main__":
    from db import init_db

    init_db()
    new_ids = fetch_all()
    print(f"新規Source: {len(new_ids)}件")
    for sid in new_ids:
        print(f"  {sid}")
