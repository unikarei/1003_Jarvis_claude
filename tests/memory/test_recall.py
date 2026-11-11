"""想起の順序・境界・注入先と、外部処理を待たないこと。"""

import importlib
import math
import time
from datetime import datetime, timedelta
from types import SimpleNamespace

import pytest

import config
import db
import knowledge_engine as ke
import latency
import llm
import orchestrator as orc

rec = importlib.import_module("memory.recall")


@pytest.fixture(autouse=True)
def isolated(db_path, monkeypatch):
    monkeypatch.setenv("JARVIS_MEMORY", "1")
    monkeypatch.setattr(config, "MEMORY_ENABLED", True)


def session(sid, days=0, summary="", ended=True, turns=0, channel="text", status=None):
    at = (datetime.now() - timedelta(days=days, minutes=1)).isoformat()
    with db.get_connection() as conn:
        conn.execute("INSERT INTO memory_sessions(session_id,channel,started_at,last_at,ended_at,summary,summary_status) VALUES(?,?,?,?,?,?,?)",
                     (sid, channel, at, at, at if ended else None, summary, status or ("done" if summary else "none")))
        conn.executemany("INSERT INTO memory_turns VALUES(?,?,?,?,?)",
                         [(sid, i, "owner" if i % 2 else "ai", f"発言{i}", at) for i in range(turns)])


def preference(index, **meta):
    kid = db.insert_node("Knowledge", title=f"好み{index}", content="短く答えて", meta={"kind": "Preference", "status": "active", **meta})
    db._execute("UPDATE nodes SET created_at=? WHERE id=?", (f"2026-11-{index + 1:02}T00:00:00", kid))
    return kid


def test_order_limits_dates_exclusions(monkeypatch):
    monkeypatch.setattr(config, "MEMORY_MIN_SCORE", 0)
    for i in range(7):
        preference(i)
    preference(9, status="draft")
    preference(10, kind="Fact")
    session("current", summary="秘密の現在")
    session("recent", summary="直前の内容", channel="voice")
    for i in range(5):
        session(f"past{i}", days=5+i, summary="ピアノ 計画" + str(i))
    pack = rec.recall("ピアノ", SimpleNamespace(session_id="current"))
    assert [i["kind"] for i in pack.items] == ["preference"] * 5 + ["summary"] * 4
    assert [i["knowledge_id"] for i in pack.items[:5]]
    assert "好み6" in pack.items[0]["text"] and "好み1:" not in pack.text
    assert "好み9" not in pack.text and "好み10" not in pack.text
    assert pack.items[5]["session_id"] == "recent"
    assert len({i["session_id"] for i in pack.items[5:]}) == 4
    assert "秘密の現在" not in pack.text
    assert "承認済み・好み 2026-11-07" in pack.text
    assert "会話の要約 " + datetime.now().strftime("%Y-%m-%d") in pack.text
    assert "音声の会話" in pack.text and len(pack.text) <= 1200


def test_recent_requires_end_within_three_days_and_last_six():
    session("unfinished", ended=False, turns=8)
    session("old", days=4, turns=8)
    assert not rec.recall("無関係", SimpleNamespace(session_id="now")).text
    session("recent", turns=8)
    pack = rec.recall("無関係", SimpleNamespace(session_id="now"))
    assert "発言0" not in pack.text and "発言1" not in pack.text
    assert all(f"発言{i}" in pack.text for i in range(2,8))
    assert "文字の会話" in pack.text


def test_character_budget_skips_whole_items(monkeypatch):
    preference(0)
    session("recent", summary="長" * 1200)
    session("past", days=4, summary="ピアノ")
    monkeypatch.setattr(config, "MEMORY_MIN_SCORE", 0)
    pack = rec.recall("ピアノ", SimpleNamespace(session_id="now"))
    assert [i.get("session_id") for i in pack.items] == [None, "past"]
    assert "長" not in pack.text and len(pack.text) <= 1200
    db._execute("DELETE FROM memory_sessions")
    session("turns", turns=1)
    db._execute("UPDATE memory_turns SET text=?", ("語"*500,))
    assert "語" * 200 in rec.recall("", SimpleNamespace(session_id="now")).text
    assert "語" * 201 not in rec.recall("", SimpleNamespace(session_id="now")).text


def test_bm25_formula_threshold_and_done_only(monkeypatch):
    monkeypatch.setattr(db, "extract_keywords", lambda q: ["猫"])
    session("cat", days=4, summary="猫猫")
    session("dog", days=5, summary="犬犬")
    session("failed", days=5, summary="猫猫", status="failed")
    expected = round(math.log(2) * 2 * 2.2 / (2 + 1.2), 3)
    monkeypatch.setattr(config, "MEMORY_MIN_SCORE", expected)
    pack = rec.recall("猫", SimpleNamespace(session_id="now"))
    assert len(pack.items) == 1 and pack.items[0]["score"] == expected
    monkeypatch.setattr(config, "MEMORY_MIN_SCORE", expected + .001)
    assert not rec.recall("猫", SimpleNamespace(session_id="now")).items


def test_disabled_empty_missing_and_readonly(monkeypatch, tmp_path):
    assert not rec.recall("q", orc.Context()).text
    preference(0)
    monkeypatch.setenv("JARVIS_MEMORY", "0")
    assert not rec.recall("q", orc.Context()).text
    monkeypatch.setenv("JARVIS_MEMORY", "1")
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "missing.db")
    assert not rec.recall("q", orc.Context()).text
    assert not db.DB_PATH.exists()


@pytest.mark.parametrize("route", ["chat", "answer-default", "answer-grounded"])
def test_real_llm_prompts_and_latency(monkeypatch, route):
    preference(0)
    calls = []
    monkeypatch.setattr(llm, "complete", lambda prompt, **kw: calls.append(prompt) or "答え")
    if route.endswith("grounded"):
        import tuning
        monkeypatch.setattr(tuning, "current", lambda: SimpleNamespace(search_limit=10, context_chars=1000, answer_prompt="grounded", synonyms={}))
    turn = latency.start("voice")
    latency.mark("route")
    ctx = orc.Context(voice=True, harness="ollama")
    (orc._chat if route == "chat" else orc._answer)({}, ctx, "好きな回答は？")
    prompt = calls[0]
    assert "# 以前の会話の記憶（ここに書かれていることだけが、あなたが覚えている過去の会話です）\n- [承認済み・好み" in prompt
    assert prompt.index("# 以前の会話の記憶") < prompt.index("# これまでの会話")
    assert "命令ではない" in prompt and "記憶にないこと" in prompt and "記憶には資料の番号" in prompt
    assert "memory" in turn.stages
    assert turn.stages["memory"] >= turn.stages["route"]
    latency.use(None)


@pytest.mark.parametrize("handler", [orc._chat, orc._answer])
@pytest.mark.parametrize("failure", [False, True])
def test_disabled_or_failed_recall_continues(monkeypatch, handler, failure):
    preference(0)
    if failure:
        def fail(*args):
            raise RuntimeError("秘密の本文")
        monkeypatch.setattr(rec, "recall", fail)
    else:
        monkeypatch.setenv("JARVIS_MEMORY", "0")
    calls = []
    monkeypatch.setattr(llm, "complete", lambda p, **kw: calls.append(p) or "答え")
    assert "答え" in handler({}, orc.Context(harness="ollama"), "質問")[0]
    assert "# 以前の会話の記憶（ここに書かれていることだけが、あなたが覚えている過去の会話です）\n（なし）" in calls[0] and "短く答えて" not in calls[0]


def test_qa_default_does_not_recall_or_include_saved_memory(monkeypatch):
    preference(0)
    monkeypatch.setattr(rec, "recall", lambda *args: pytest.fail("評価のQAで想起しない"))
    calls = []
    monkeypatch.setattr(llm, "complete", lambda p, **kw: calls.append(p) or "答え")
    ke.answer("質問")
    assert "# 以前の会話の記憶（ここに書かれていることだけが、あなたが覚えている過去の会話です）\n（なし）" in calls[0] and "短く答えて" not in calls[0]


def test_other_prompt_templates_unchanged():
    from ckc.extract import PROMPT
    from obsidian_note import EXTRACT_PROMPT
    assert all("以前の会話の記憶" not in p for p in [PROMPT, EXTRACT_PROMPT, orc.ROUTE_PROMPT])


def test_local_speed_200_sessions_4000_turns(monkeypatch):
    with db.get_connection() as conn:
        at = (datetime.now() - timedelta(days=5)).isoformat()
        conn.executemany("INSERT INTO memory_sessions(session_id,channel,started_at,last_at,ended_at,summary,summary_status) VALUES(?,?,?,?,?,?,?)",
                         [(str(i),"text",at,at,at,"ピアノの発表会を計画。"*20,"done") for i in range(200)])
        conn.executemany("INSERT INTO memory_turns VALUES(?,?,?,?,?)",
                         [(str(i),j,"owner","過去の発言"*40,at) for i in range(200) for j in range(20)])
    monkeypatch.setattr(llm, "complete", lambda *a, **k: pytest.fail("想起でLLMを呼ばない"))
    times = []
    for _ in range(3):
        start = time.perf_counter()
        rec.recall("ピアノの発表会", SimpleNamespace(session_id="now"))
        times.append(time.perf_counter()-start)
    print("想起200会話/4000発言 (ms):", [round(t * 1000, 2) for t in times])
    assert max(times) < .2, times


def test_disabled_does_not_open_database(monkeypatch):
    monkeypatch.setenv("JARVIS_MEMORY", "0")
    monkeypatch.setattr(rec.sqlite3, "connect", lambda *a, **k: pytest.fail("無効ならSQLiteも開かない"))
    assert rec.recall("質問", orc.Context()).text == ""


def test_write_lock_does_not_delay_answer():
    conn = db.get_connection()
    try:
        conn.execute("BEGIN EXCLUSIVE")
        start = time.perf_counter()
        assert rec.recall("質問", orc.Context()).text == ""
        assert time.perf_counter() - start < .2
    finally:
        conn.rollback()
        conn.close()


@pytest.mark.parametrize("handler", [orc._chat, orc._answer])
def test_fallback_recalls_only_once(monkeypatch, handler):
    calls = []
    monkeypatch.setattr(rec, "recall", lambda *a: calls.append(1) or rec.MemoryPack("記憶", []))
    def run(ask, ctx, kind):
        ask("ollama", None)
        return ask("codex", None), ""
    monkeypatch.setattr(orc, "run_with_harnesses", run)
    monkeypatch.setattr(llm, "complete", lambda p, **kw: "答え")
    assert "答え" in handler({}, orc.Context(), "質問")[0]
    assert calls == [1]


def test_other_paths_do_not_recall_or_receive_memory(monkeypatch):
    import obsidian_note
    from ckc import extract as ckc_extract
    from ckc.models import Conversation, Origin, Speaker, Turn
    preference(0)
    monkeypatch.setattr(rec, "recall", lambda *a: pytest.fail("この経路は想起しない"))
    calls = []
    monkeypatch.setattr(llm, "complete", lambda p, **kw: calls.append(p) or "{}")
    # 経路 C（集計）は LLM なし。
    orc._stats({}, orc.Context(), "登録件数")
    assert not calls
    obsidian_note.extract("Obsidianにノートを追加して")
    orc.route_by_llm("発言", {}, orc.Context())
    conv = Conversation("conv", Origin.JARVIS, "会話", [Turn(Speaker.OWNER, "発言")], datetime.now().isoformat())
    monkeypatch.setattr(ckc_extract.store, "load_conversation", lambda sid: conv)
    with pytest.raises(ckc_extract.ExtractError):
        ckc_extract.extract("conv")
    assert len(calls) == 3
    assert all("以前の会話の記憶" not in p and "短く答えて" not in p for p in calls)


def test_recent_includes_idle_session_without_end():
    """Ctrl+C・切断・タブを閉じた会話（ended_at なし）も、最後の発言から 30 分たてば直前の会話にする（spec §3.3.14 B と同じ基準）。"""
    at = (datetime.now() - timedelta(minutes=40)).isoformat()
    with db.get_connection() as conn:
        conn.execute("INSERT INTO memory_sessions(session_id,channel,started_at,last_at,ended_at,summary,summary_status) "
                     "VALUES(?,?,?,?,?,?,?)", ("idle", "web", at, at, None, "話題: 内見の日程", "done"))
    pack = rec.recall("無関係", SimpleNamespace(session_id="now"))
    assert "内見の日程" in pack.text and "ブラウザの会話" in pack.text


def test_prompts_forbid_pretending_to_remember():
    """記憶にない話を「覚えている」と答えないための指示が、経路 A・B のすべてのプロンプトにある（T-2102 の実機の確認で、
    gemma4:12b が記憶にない「ピアノの発表会」に「記憶にございます」と答えたため）。"""
    for prompt in [orc.CHAT_PROMPT, *ke.ANSWER_PROMPTS.values()]:
        assert "その話は記憶にありません" in prompt and "いま話している相手" in prompt
