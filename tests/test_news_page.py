"""ニュースの一覧のページ（T-3301、ワークスペースの testing §34）。一時 DB・一時のフォルダで作る。"""
import json
import re
import shutil
import subprocess
from datetime import datetime

import pytest

import config
import db
import news_page
import news_rank
import run_daily

NOW = datetime(2026, 11, 21, 9, 30)


@pytest.fixture
def news(db_path, tmp_path, monkeypatch):
    monkeypatch.setattr(config, "NEWS_DIR", tmp_path / "news-scores")

    def add(title, category="news", day="2026-11-21", feed="TechCrunch AI", summary="要約の文", body="本文",
            url=None):
        url = url or f"https://ex.com/{title}"
        kid = db.insert_node("Knowledge", category=category, title=title, content=summary, url=url)
        db._execute("UPDATE nodes SET created_at = ? WHERE id = ?", (f"{day}T06:00:00", kid))
        sid = db.insert_node("Source", category=category, title=title, content=body, url=url, meta={"feed_name": feed})
        db.insert_edge(kid, sid, "derived_from")
        return kid
    return add


def test_builds_page_index_and_day_files(news, tmp_path):
    a = news("Nvidia CEO resigns", summary="あ" * 150, body="<p>First &amp; <b>bold</b></p><script>alert(1)</script><p>Second</p>")
    b = news("あとふる", category="minpaku", feed="観光経済新聞")
    c = news("未採点の記事", category="trading", feed="Investing.com 日本")
    news("前日の記事", day="2026-11-20")
    news_rank._append([{"id": a, "score": 92, "reason": "世界的", "headline": "NVIDIA の CEO が辞任"},
                       {"id": b, "score": 38, "reason": "地域", "headline": "あとふる"}])
    out = tmp_path / "out"
    page = news_page.build(out, now=NOW)
    html = page.read_text(encoding="utf-8")
    assert '<p class="updated">最終更新 2026-11-21 09:30</p>' in html        # ランチャーが読む
    index = json.loads((out / "data" / "index.json").read_text(encoding="utf-8"))
    assert [d["day"] for d in index["days"]] == ["2026-11-21", "2026-11-20"]
    assert index["days"][0] == {"day": "2026-11-21", "count": 3, "scored": 2}
    assert ["news", "AI・テック"] in index["categories"]
    day = json.loads((out / "data" / "2026-11-21.json").read_text(encoding="utf-8"))["articles"]
    assert [x["id"] for x in day] == [a, b, c]                              # 点数の順、未採点は後ろ
    top = day[0]
    assert top["score"] == 92 and top["reason"] == "世界的" and top["headline"] == "NVIDIA の CEO が辞任"
    assert top["label"] == "AI・テック" and top["feed"] == "TechCrunch AI" and top["url"].startswith("https://")
    assert top["short"] == "あ" * 100 + "…" and top["summary"] == "あ" * 150
    assert top["body"] == "First & bold\nSecond"                             # タグ・script を除く
    assert day[2]["score"] is None and day[2]["label"] == "株式・経済"


def test_page_shows_article_text_as_text_only(news, tmp_path):
    news("<img src=x onerror=alert(1)>", url="javascript:alert(1)")
    out = tmp_path / "out"
    html = news_page.build(out, now=NOW).read_text(encoding="utf-8")
    assert "innerHTML" not in html and "insertAdjacentHTML" not in html and "document.write" not in html
    assert not re.search(r"<script[^>]+src=|<link[^>]+href=|@import|https?://", html)   # 外部の読み込みなし
    item = json.loads((out / "data" / "2026-11-21.json").read_text(encoding="utf-8"))["articles"][0]
    assert item["title"] == "<img src=x onerror=alert(1)>"                  # 文字のまま（ページは textContent）
    assert item["url"] == ""                                                 # http(s) 以外のリンクは出さない


def test_page_script_is_valid_javascript(news, tmp_path):
    node = shutil.which("node")
    if node is None:
        pytest.skip("node がない")
    news("記事")
    html = news_page.build(tmp_path / "out", now=NOW).read_text(encoding="utf-8")
    script = html.split("<script>", 1)[1].split("</script>", 1)[0]
    path = tmp_path / "page.js"
    path.write_text(script, encoding="utf-8")
    assert subprocess.run([node, "--check", str(path)], capture_output=True).returncode == 0


def test_empty_db_builds_empty_index(db_path, tmp_path, monkeypatch):
    monkeypatch.setattr(config, "NEWS_DIR", tmp_path / "news-scores")
    out = tmp_path / "out"
    news_page.build(out, now=NOW)
    assert json.loads((out / "data" / "index.json").read_text(encoding="utf-8"))["days"] == []


def test_plain_text_handles_entities_and_broken_html():
    assert news_page.plain_text("A&nbsp;&lt;b&gt;<br>B") == "A <b>\nB"
    assert news_page.plain_text("<div>unclosed <p>text") == "unclosed\ntext"
    assert news_page.plain_text("") == ""


def test_daily_run_continues_when_page_fails(monkeypatch, capsys):
    monkeypatch.setattr(run_daily, "init_db", lambda: None)
    monkeypatch.setattr(run_daily, "fetch_all", lambda: [])
    monkeypatch.setattr(run_daily, "summarize_all", lambda: [])
    monkeypatch.setattr(news_rank, "score_pending", lambda *a, **k: (0, 0))
    monkeypatch.setattr(news_page, "build", lambda *a, **k: (_ for _ in ()).throw(OSError("書けない")))
    written = []
    monkeypatch.setattr(run_daily, "write_report", lambda category: written.append(category) or f"/tmp/{category}.md")
    monkeypatch.setitem(__import__("sys").modules, "piano_analytics", None)
    run_daily.main()
    out = capsys.readouterr().out
    assert "ニュースのページ: スキップ（OSError: 書けない）" in out and "news" in written
