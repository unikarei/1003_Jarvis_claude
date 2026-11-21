"""Insight Reporter のレポートを会話で使う（T-3401、ワークスペースの testing §36）。一時 DB・一時の 1004 の reports。"""
import hashlib
import json
from datetime import date

import pytest

import briefing
import config
import db
import ir_reports
import knowledge_engine as ke
import llm
import orchestrator as orc
import run_daily
import skills
from voice.speak import speech_summary

R1003 = "repo-1003-jarvis-claude"
TODAY = date(2026, 11, 21)


def _research(rid, generated, items, name="Physical AI Scan（試験）"):
    return {"report_id": rid, "report_type": rid, "name": name, "generated_at": generated,
            "period": {"start": "2026-11-13", "end": "2026-11-19"}, "overview": ["今週はエージェントの設計が進んだ。"],
            "items": [{"rank": i, "title": f"Article {i} about CAD agents", "source": "arXiv", "published_date": "2026-11-19",
                       "url": f"https://arxiv.org/abs/{i}", "summary_points": [f"要点{i}"], "why_it_matters": "重要",
                       "application_hint": "翼に使える", "caution": "査読前", "reading_value": "今週の最優先"}
                      for i in range(1, items + 1)],
            "closing_sections": [{"heading": "今週の最重要トレンド", "lines": ["エージェントが主役に"]}], "warnings": ["期間外あり"]}


@pytest.fixture
def ir(tmp_path, db_path, monkeypatch):
    root = tmp_path / "1004"
    monkeypatch.setattr(config, "INSIGHT_REPORTER_DIR", root)

    def put(rid, day, report):
        path = root / "reports" / rid / day / "report.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(report, ensure_ascii=False), encoding="utf-8")
        return path
    put("physical_ai", "2026-11-20", _research("physical_ai", "2026-11-20T09:00:00+00:00", 9))
    put("physical_ai", "2026-11-14", _research("physical_ai", "2026-11-14T04:57:42+00:00", 3))
    put("turbine_research", "2026-11-20", _research("turbine_research", "2026-11-20T09:00:00+00:00", 2, name="Turbine Scan"))
    put("piano_growth", "2026-11-20", {"report_id": "piano_growth", "name": "ピアノ教室の週次集客レポート",
                                       "generated_at": "2026-11-20T09:00:10+00:00",
                                       "period": {"search_console": {"current": {"start": "2026-10-20", "end": "2026-11-17"}}},
                                       "summary": ["検索クリック数は91件"], "metrics": {"clicks": 91},
                                       "recommendations": [{"observation": "CTR が低い", "recommended_action": "題名を見直す"}]})
    return put


def _count(url_prefix):
    return db._fetchone("SELECT count(*) c FROM nodes WHERE type='Knowledge' AND url LIKE ?", (url_prefix + "%",))["c"]


def test_ingests_research_runs_and_items_once(ir, db_path, tmp_path):
    before = {p: p.stat().st_mtime_ns for p in (tmp_path / "1004").rglob("*")}
    assert ir_reports.ingest_all() == {"new": 3 + 9 + 3 + 2, "updated": 0, "same": 0}
    assert _count("insight-reporter://physical_ai/") == 1 + 9 + 1 + 3
    assert _count("insight-reporter://piano_growth/") == 0                # ピアノ教室は従来の取り込みのまま
    item = db.find_knowledge_by_url("insight-reporter://physical_ai/2026-11-20T09:00:00+00:00#1")
    assert "要点1" in item["content"] and "翼に使える" in item["content"] and "https://arxiv.org/abs/1" in item["content"]
    assert json.loads(item["meta"])["projects"] == ["physical-ai"]
    summary = db.find_knowledge_by_url("insight-reporter://physical_ai/2026-11-20T09:00:00+00:00")
    assert "## 記事（9 件）" in summary["content"] and "今週の最重要トレンド" in summary["content"]
    assert ir_reports.ingest_all() == {"new": 0, "updated": 0, "same": 17}    # 重ねない
    assert before == {p: p.stat().st_mtime_ns for p in (tmp_path / "1004").rglob("*")}    # 1004 に書かない


def test_rewritten_report_updates_content(ir):
    ir_reports.ingest_all()
    report = _research("physical_ai", "2026-11-20T09:00:00+00:00", 9)
    report["items"][0]["why_it_matters"] = "書き直した"
    ir("physical_ai", "2026-11-20", report)
    counts = ir_reports.ingest_all()
    assert counts["updated"] == 1 and counts["new"] == 0                     # 知識は記事 1 だけ変わる（まとめは同じ）
    item = db.find_knowledge_by_url("insight-reporter://physical_ai/2026-11-20T09:00:00+00:00#1")
    assert "書き直した" in item["content"]
    source = db.find_source_by_url("insight-reporter://physical_ai/2026-11-20T09:00:00+00:00")
    assert "書き直した" in source["content"]                                    # 元データも更新


def test_ingested_items_are_searchable(ir, monkeypatch):
    monkeypatch.setattr(ke, "_category_projects", lambda: {})
    ir_reports.ingest_all()
    hits = ke.search("CAD agents 翼に使える")
    assert hits and hits[0].url.startswith("insight-reporter://")


@pytest.mark.parametrize("text", [
    "インサイトレポートの一覧", "11月20日のフィジカルAIスキャンの題目を全部並べて", "最新のタービンのレポートのまとめ",
    "インサイトレポーターの中身ってどんなのあるんだっけ", "ピアノのレポートの中身",
])
def test_matches(text):
    assert ir_reports.matches(text)


@pytest.mark.parametrize("text", [
    "インサイトレポーターの中でCADのエージェントの記事について議論したい", "インサイトレポートを保存して",
    "週1のインサイトレポートって何が動かしてるの", "タービンの話を教えて", "フィジカルAIについて教えて",
])
def test_does_not_match(text):
    assert not ir_reports.matches(text)


def test_digest_lists_all_titles_in_rank_order(ir):
    text = ir_reports.digest("11月20日のフィジカルAIスキャンの題目を全部", TODAY)
    lines = text.splitlines()
    assert lines[0] == "Physical AI Scan（試験）、11月20日（金）の回（対象 2026-11-13〜2026-11-19）の記事は 9 件です。"
    titles = [line for line in lines if line[:2].rstrip(".").isdigit()]
    assert [t.split(".")[0] for t in titles] == [str(i) for i in range(1, 10)]
    assert "1. Article 1 about CAD agents（arXiv、2026-11-19）— 今週の最優先" in lines
    assert "今週の最重要トレンド: エージェントが主役に" in text
    assert speech_summary(ir_reports.SKILL, text) == "Physical AI Scan、11月20日の回の記事は 9 件です。題目は画面に並べました。"


def test_digest_list_latest_previous_piano_and_missing(ir):
    listing = ir_reports.digest("インサイトレポートの一覧", TODAY)
    assert "- Physical AI Scan（試験）: 最新 11月20日（金）（記事 9 件）。前の回: 11月14日（土）" in listing
    assert "ピアノ教室の週次集客レポート: 最新 11月20日（金）（数値のレポート）" in listing
    assert speech_summary(ir_reports.SKILL, listing) == "Insight Reporter のレポートは 3 種類です。一覧を画面に出しました。"
    assert ir_reports.digest("先週のフィジカルAIのレポートの題目", TODAY).startswith("Physical AI Scan（試験）、11月14日（土）の回")
    piano = ir_reports.digest("ピアノのレポートの中身", TODAY)
    assert "（対象 2026-10-20〜2026-11-17）" in piano and "- CTR が低い → 題名を見直す" in piano
    assert ir_reports.digest("11月1日のフィジカルAIスキャンの題目", TODAY).startswith("11月1日のレポートはありません。ある日:")


@pytest.fixture
def index(tmp_path, monkeypatch):
    def s(desc, phrases=(), inputs=None):
        return {"description": desc, "status": "active", "permission": "read", "entry": {"repo": R1003, "function": "x"},
                "inputs": inputs or {}, "phrases": list(phrases)}
    path = tmp_path / "skills.json"
    path.write_text(json.dumps({"skills": {
        "knowledge-answer": s("答える", inputs={"question": {"type": "string", "required": True}}),
        "report-digest": s("レポートの一覧", ["インサイトレポート"]),
        "news-digest": s("主要なニュース", ["主要なニュース"]),
    }}, ensure_ascii=False), encoding="utf-8")
    monkeypatch.setenv("JARVIS_SKILLS_JSON", str(path))
    skills.reset_cache()
    yield
    skills.reset_cache()


def test_route_and_execute_without_llm(index, ir, monkeypatch):
    def refuse(*a, **k):
        raise AssertionError("LLM を呼ばない")
    monkeypatch.setattr(llm, "complete", refuse)
    ctx = orc.Context()
    r = orc.route("インサイトレポートの一覧", ctx)
    assert r.skill == "report-digest" and r.method == "rule"
    out = orc.execute(r, ctx, "インサイトレポートの一覧")
    assert out.ok and "Physical AI Scan（試験）" in out.text
    # 発言の例だけでは回さない（話題の議論は検索で答える）
    assert orc.route("インサイトレポートのCADの記事について議論したい", ctx).skill != "report-digest"


def test_briefing_has_reports(ir, monkeypatch):
    lines = ir_reports.briefing_lines()
    assert any("最新 2026-11-20（記事 9 件）" in line for line in lines)
    assert "## Insight Reporter のレポート" in briefing.build()


def test_daily_run_continues_when_ingest_fails(monkeypatch, capsys):
    monkeypatch.setattr(run_daily, "init_db", lambda: None)
    monkeypatch.setattr(run_daily, "fetch_all", lambda: [])
    monkeypatch.setattr(run_daily, "summarize_all", lambda: [])
    import news_page
    import news_rank
    monkeypatch.setattr(news_rank, "score_pending", lambda *a, **k: (0, 0))
    monkeypatch.setattr(news_page, "build", lambda *a, **k: "page")
    monkeypatch.setattr(ir_reports, "ingest_all", lambda: (_ for _ in ()).throw(OSError("読めない")))
    written = []
    monkeypatch.setattr(run_daily, "write_report", lambda category: written.append(category) or f"/tmp/{category}.md")
    monkeypatch.setitem(__import__("sys").modules, "piano_analytics", None)
    run_daily.main()
    out = capsys.readouterr().out
    assert "Insight Reporter の調査のレポート: スキップ（OSError: 読めない）" in out and "news" in written
