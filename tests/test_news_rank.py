"""ニュースの採点と主要なニュース（T-3300、ワークスペースの testing §33）。一時 DB・一時の data フォルダ・偽の LLM。"""
import hashlib
import json
from datetime import datetime

import pytest

import briefing
import config
import db
import llm
import news_rank
import orchestrator as orc
import run_daily
import skills
from voice.speak import speech_summary

R1003 = "repo-1003-jarvis-claude"
NOW = datetime(2026, 11, 21, 9, 0)


@pytest.fixture
def news(db_path, tmp_path, monkeypatch):
    monkeypatch.setattr(config, "NEWS_DIR", tmp_path / "news")

    def add(title, category="news", day="2026-11-21", feed="TechCrunch AI", summary="要約", body="本文"):
        kid = db.insert_node("Knowledge", category=category, title=title, content=summary, url=f"https://ex.com/{title}")
        db._execute("UPDATE nodes SET created_at = ? WHERE id = ?", (f"{day}T06:00:00", kid))
        sid = db.insert_node("Source", category=category, title=title, content=body, url=f"https://ex.com/{title}",
                             meta={"feed_name": feed})
        db.insert_edge(kid, sid, "derived_from")
        return kid
    return add


def _reply(prompt, scores=None):
    """プロンプトの記事の数だけ、番号の順に点数を返す偽の答え。"""
    count = prompt.count("\n題名: ")
    scores = scores or [50] * count
    return json.dumps([{"n": i + 1, "score": scores[i], "reason": f"理由{i + 1}", "headline": f"見出し{i + 1}"}
                       for i in range(count)], ensure_ascii=False)


def test_scores_only_pending_news_in_batches_and_keeps_db(news, db_path):
    for i in range(30):
        news(f"a{i}")
    news("old", day="2026-11-01")                     # 範囲外
    kid = db.insert_node("Knowledge", category="minpaku", title="Obsidian のノート",
                         url="https://github.com/unikarei/oarai-obsidian/blob/main/x.md")   # ニュースではない
    before = hashlib.sha256(db_path.read_bytes()).hexdigest()
    calls = []

    def fake(prompt, provider):
        calls.append(provider)
        return _reply(prompt)
    assert news_rank.score_pending(days=3, now=NOW, complete=fake) == (30, 0)
    assert calls == ["claude_cli", "claude_cli"]       # 25 件 + 5 件
    rows = [json.loads(line) for line in news_rank.scores_path().read_text(encoding="utf-8").splitlines()]
    assert len(rows) == 30 and {"score", "reason", "headline", "provider", "scored_at"} <= set(rows[0])
    assert kid not in {r["id"] for r in rows}
    assert hashlib.sha256(db_path.read_bytes()).hexdigest() == before        # jarvis.db は変えない
    assert news_rank.score_pending(days=3, now=NOW, complete=fake) == (0, 0)  # 採点済みは採点しない


def test_falls_back_to_next_llm_and_leaves_unscored_when_all_fail(news):
    news("a")
    tried = []

    def wrong_then_ok(prompt, provider):
        tried.append(provider)
        if provider == "claude_cli":
            raise llm.LLMError("利用上限")
        if provider == "codex":
            return '[{"n": 1, "score": 500, "reason": "x", "headline": "y"}]'    # 形が違う
        return _reply(prompt)
    assert news_rank.score_pending(days=1, now=NOW, complete=wrong_then_ok) == (1, 0)
    assert tried == ["claude_cli", "codex", "ollama"]
    news("b")

    def broken(prompt, provider):
        return "採点できません"
    assert news_rank.score_pending(days=1, now=NOW, complete=broken) == (0, 1)
    assert len(news_rank.load_scores()) == 1


@pytest.mark.parametrize("reply", [
    "[]", '[{"n": 1, "score": 50, "reason": "", "headline": "x"}]', '[{"n": 2, "score": 50, "reason": "a", "headline": "b"}]',
    '[{"n": 1, "score": true, "reason": "a", "headline": "b"}]', "点数は 50 です",
])
def test_parse_scores_rejects_wrong_shapes(reply):
    with pytest.raises(ValueError):
        news_rank.parse_scores(reply, 1)


def test_parse_scores_trims_and_accepts_wrapped_json():
    out = news_rank.parse_scores('結果:\n[{"n": 1, "score": 88.0, "reason": "' + "長" * 60 + '", "headline": "見出し"}]', 1)
    assert out[0]["score"] == 88 and len(out[0]["reason"]) == news_rank.REASON_CHARS


@pytest.mark.parametrize("text", [
    "今日の主要なニュースは？", "昨日のニュースのトップ10", "11月20日の重要なニュース", "今日のニュースは何件？",
    "ここ1週間の大きなニュースは？", "AIの主要なニュースは", "上位10件のニュースを言ってくれ",
])
def test_matches_digest_requests(text):
    assert news_rank.matches(text)


@pytest.mark.parametrize("text", [
    "NVIDIA のニュースで大きいのは？", "ニュースって何？", "2700件のニュースってどういう記事が多いのか気になるね。",
    "今日のニュースを保存して", "ビルゲイツの大きな動きのニュースある?", "今日のニュースの要約を15秒ぐらいで喋ってみて",
])
def test_does_not_match_topics_or_other_requests(text):
    assert not news_rank.matches(text)


def test_digest_orders_by_score_with_limit_and_counts(news):
    ids = {name: news(name, category=cat, feed=feed) for name, cat, feed in [
        ("Nvidia CEO resigns", "news", "TechCrunch AI"), ("あとふる導入", "minpaku", "観光経済新聞"),
        ("日経平均が最高値", "trading", "Investing.com 日本"), ("連載第5回", "news", "ITmedia AI+")]}
    news_rank._append([{"id": ids["Nvidia CEO resigns"], "score": 95, "reason": "世界的な経営者の交代", "headline": "NVIDIA の CEO が辞任"},
                       {"id": ids["あとふる導入"], "score": 25, "reason": "地域の小さな話題", "headline": "あとふるが19自治体に"},
                       {"id": ids["日経平均が最高値"], "score": 70, "reason": "市場全体の動き", "headline": "日経平均が最高値"}])
    text = news_rank.digest("今日のニュースのトップ2", now=NOW)
    lines = text.splitlines()
    assert lines[0] == "11月21日（土）に取り込んだニュース 4 件（採点済み 3 件）のうち、点数の高い順に 2 件です。"
    assert lines[1].startswith("1. [95] NVIDIA の CEO が辞任（AI・テック・TechCrunch AI）— 世界的な経営者の交代")
    assert lines[2] == "   原題: Nvidia CEO resigns"
    assert lines[3].startswith("2. [70] 日経平均が最高値（株式・経済")
    assert "分類別: AI・テック 2 件、株式・経済 1 件、民泊・観光 1 件" in text and "未採点 1 件" in text
    spoken = speech_summary(news_rank.SKILL, text)
    assert spoken == "11月21日のニュース 4 件のうち、1 位、NVIDIA の CEO が辞任。2 位、日経平均が最高値。"


def test_digest_filters_category(news):
    a, b = news("AI の話"), news("株の話", category="trading")
    news_rank._append([{"id": a, "score": 60, "reason": "r", "headline": "AI"}, {"id": b, "score": 90, "reason": "r", "headline": "株"}])
    text = news_rank.digest("AIの主要なニュースは", now=NOW)
    assert "AI・テックのニュース 1 件" in text and "] 株" not in text


def test_digest_falls_back_to_latest_day_and_reports_unscored(news):
    news("前日の記事", day="2026-11-20")
    text = news_rank.digest("今日の主要なニュースは？", now=NOW)
    assert text.startswith("11月21日（土）に取り込んだニュースはまだありません。いちばん新しい11月20日（金）の分です。")
    assert "まだ採点していません" in text and "分類別: AI・テック 1 件" in text
    assert news_rank.digest("11月10日の重要なニュース", now=NOW) == "11月10日（火）に取り込んだニュースはありません。"


def test_week_range(news):
    a, b = news("週の始め", day="2026-11-15"), news("範囲外", day="2026-11-14")
    news_rank._append([{"id": a, "score": 80, "reason": "r", "headline": "週の始め"}, {"id": b, "score": 99, "reason": "r", "headline": "範囲外"}])
    text = news_rank.digest("ここ1週間の大きなニュースは？", now=NOW)
    assert text.startswith("11月15日（日）〜11月21日（土）に取り込んだニュース 1 件") and "範囲外" not in text
    assert speech_summary(news_rank.SKILL, text).startswith("11月15日から11月21日のニュース 1 件のうち、1 位、週の始め")


@pytest.fixture
def index(tmp_path, monkeypatch):
    def s(desc, phrases=(), inputs=None):
        return {"description": desc, "status": "active", "permission": "read", "entry": {"repo": R1003, "function": "x"},
                "inputs": inputs or {}, "phrases": list(phrases)}
    path = tmp_path / "skills.json"
    path.write_text(json.dumps({"skills": {
        "knowledge-answer": s("普通の質問に答える", inputs={"question": {"type": "string", "required": True}}),
        "knowledge-search": s("知識を検索する", ["を探して"], {"query": {"type": "string", "required": True}}),
        "knowledge-stats": s("件数", ["件数", "何件"]),
        "news-digest": s("主要なニュース", ["主要なニュース"]),
    }}, ensure_ascii=False), encoding="utf-8")
    monkeypatch.setenv("JARVIS_SKILLS_JSON", str(path))
    skills.reset_cache()
    yield
    skills.reset_cache()


def test_route_and_execute_without_llm(index, news, monkeypatch):
    def refuse(*a, **k):
        raise AssertionError("LLM を呼ばない")
    monkeypatch.setattr(llm, "complete", refuse)
    news("記事")
    ctx = orc.Context()
    r = orc.route("今日のニュースは何件？", ctx)
    assert r.skill == "news-digest" and r.method == "rule"
    out = orc.execute(r, ctx, "今日のニュースは何件？")
    assert out.ok and "ニュース 1 件" in out.text
    assert orc.route("NVIDIA のニュースで大きいのは？", ctx).skill != "news-digest"


def test_not_routed_when_skill_inactive(news, tmp_path, monkeypatch):
    path = tmp_path / "skills0.json"
    path.write_text(json.dumps({"skills": {}}), encoding="utf-8")
    monkeypatch.setenv("JARVIS_SKILLS_JSON", str(path))
    skills.reset_cache()
    assert orc.route("今日の主要なニュースは？", orc.Context(), use_llm=False).skill != "news-digest"
    skills.reset_cache()


def test_briefing_has_news_facts(news, monkeypatch):
    news("記事1", day="2026-11-21")
    news("記事2", day="2026-11-20")
    lines = news_rank.briefing_lines(now=NOW)
    text = "\n".join(lines)
    assert "取り込みを始めた日: 2026-11-20" in text and "ニュースの記事 2 件、うち採点済み 0 件" in text
    assert "11/21 1、11/20 1、11/19 0" in text and "TechCrunch AI" in text and "「今日の主要なニュースは？」" in text
    full = briefing.build()
    assert "## ニュース" in full and len(full) <= briefing.MAX_CHARS


def test_daily_run_continues_when_scoring_fails(monkeypatch, capsys):
    monkeypatch.setattr(run_daily, "init_db", lambda: None)
    monkeypatch.setattr(run_daily, "fetch_all", lambda: [])
    monkeypatch.setattr(run_daily, "summarize_all", lambda: [])
    monkeypatch.setattr(news_rank, "score_pending", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("壊れた")))
    written = []
    monkeypatch.setattr(run_daily, "write_report", lambda category: written.append(category) or f"/tmp/{category}.md")
    monkeypatch.setitem(__import__("sys").modules, "piano_analytics", None)
    run_daily.main()
    out = capsys.readouterr().out
    assert "ニュースの採点: スキップ（RuntimeError: 壊れた）" in out and "news" in written
