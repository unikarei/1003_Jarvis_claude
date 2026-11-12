"""会話の記録・ローカル要約・復元の受け入れテスト。"""

import json
import stat
import threading
from datetime import datetime, timedelta

import pytest

import config
import db
import llm
import orchestrator as orc
from ckc.models import Speaker, new_id
from memory import store, summarize
from memory.__main__ import main

_BACKGROUND_START = summarize.start_background_summary


@pytest.fixture(autouse=True)
def isolated(db_path, tmp_path, monkeypatch):
    monkeypatch.setattr(config, "MEMORY_DIR", tmp_path / "memory")
    monkeypatch.setenv("JARVIS_MEMORY", "1")


def conversation(count=2, end=True):
    sid = new_id("sess")
    for i in range(count):
        store.log_turn(sid, "text", Speaker.OWNER, f"発言{i}")
        store.log_turn(sid, "text", Speaker.AI, f"応答{i}")
    if end:
        store.end_session(sid)
    return sid


def test_record_permissions_mask_empty_and_session():
    sid = new_id("sess")
    store.log_turn(sid, "voice", Speaker.OWNER, " ")
    assert store.list_sessions() == []
    secret = "sk-" + "fakekey" * 8
    store.log_turn(sid, "voice", Speaker.OWNER, secret)
    store.log_turn(sid, "voice", Speaker.AI, "応答")
    store.end_session(sid)
    path = store._path(sid)
    records = [json.loads(line) for line in path.read_text().splitlines()]
    assert [r["type"] for r in records] == ["session", "turn", "turn", "end"]
    assert secret not in path.read_text()
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert stat.S_IMODE(path.parent.stat().st_mode) == 0o700
    session = store.get_session(sid)
    assert session["owner_turns"] == 1 and session["channel"] == "voice"
    assert session["turns"][0]["text"] == records[1]["text"]
    assert session["ended_at"]


def test_disabled_writes_nothing(monkeypatch):
    monkeypatch.setenv("JARVIS_MEMORY", "0")
    sid = new_id("sess")
    store.log_turn(sid, "text", "owner", "なし")
    store.end_session(sid)
    store.save_summary(sid, "なし")
    assert summarize.summarize_pending() == 0
    assert not config.MEMORY_DIR.exists()
    assert store.list_sessions() == []


def test_failure_keeps_history_and_handle(monkeypatch, capsys):
    def fail(*args):
        raise OSError("本文を警告に出さない")
    monkeypatch.setattr(store, "_append", fail)
    ctx = orc.Context()
    orc.add_turn(ctx, Speaker.OWNER, "大事な発言")
    result = orc.handle("今のモデルは？", ctx)
    assert result.ok and ctx.history[0].text == "大事な発言"
    assert "本文を警告に出さない" not in capsys.readouterr().err


def test_target_conditions_and_retry_limit():
    ended = conversation()
    current = conversation(end=False)
    idle = conversation(end=False)
    too_short = conversation(count=1)
    failed = conversation()
    for _ in range(3):
        store.mark_summary_failed(failed)
    now = datetime.now()
    with db.get_connection() as conn:
        conn.execute("UPDATE memory_sessions SET last_at=? WHERE session_id=?",
                     ((now - timedelta(minutes=30)).isoformat(), idle))
    assert [r["session_id"] for r in store.sessions_to_summarize(now, current)] == [ended, idle]
    assert store.get_session(too_short)["summary_status"] == "skipped"
    store.save_summary(ended, "完了")
    assert [r["session_id"] for r in store.sessions_to_summarize(now, current)] == [idle]


def test_summary_local_only_validation_and_retry(monkeypatch):
    sid = conversation()
    calls = []
    def complete(prompt, provider=None):
        assert provider == "ollama"
        calls.append(prompt)
        return "\n".join(label + ": " + "長" * 300 for label in summarize.LABELS)
    monkeypatch.setattr(llm, "complete", complete)
    # 外部プロバイダーに到達すれば失敗する。
    def external(*a, **k):
        pytest.fail("外部 LLM を呼んだ")
    for name in ("claude_cli", "anthropic_api", "codex"):
        monkeypatch.setitem(llm.PROVIDERS, name, external)
    assert summarize.summarize_pending() == 1
    summary = store.get_session(sid)["summary"]
    assert len(summary) <= 400 and len(summary.splitlines()) == 4
    assert calls and "Owner: 発言0" in calls[0]
    bad = conversation()
    monkeypatch.setattr(llm, "complete", lambda *a, **k: "話題: 不足")
    for _ in range(4):
        assert summarize.summarize_pending() == 0
    assert store.get_session(bad)["summary_tries"] == 3


def test_prompt_keeps_last_40_and_6000_characters():
    turns = [dict(speaker="owner", text=f"old{i} " + "あ" * 400) for i in range(60)]
    prompt = summarize._prompt(turns)
    assert "old19 " not in prompt and "old59 " in prompt
    body = prompt.split("<会話>\n")[1].split("\n</会話>")[0]
    assert len(body) <= 6000


def test_rebuild_preserves_all_fields_and_last_summary():
    sid = conversation()
    store.mark_summary_failed(sid)
    store.save_summary(sid, "以前")
    store.save_summary(sid, "最後")
    short = conversation(count=1)
    store.sessions_to_summarize()
    before = [store.get_session(row["session_id"]) for row in store.list_sessions()]
    counts = store.stats()
    assert store.rebuild() == counts
    assert [store.get_session(row["session_id"]) for row in store.list_sessions()] == before
    assert store.get_session(short)["summary_status"] == "skipped"
    assert store.get_session(sid)["summary"] == "最後"


@pytest.mark.parametrize("answer, deleted", [("y", True), ("n", False), ("", False), ("yes", False)])
def test_forget_only_y(answer, deleted, monkeypatch, capsys):
    sid = conversation()
    monkeypatch.setattr("builtins.input", lambda prompt: answer)
    assert main(["forget", sid]) == 0
    assert (store.get_session(sid) is None) is deleted
    assert store._path(sid).exists() is not deleted
    if deleted:
        assert capsys.readouterr().err.strip() == f"[記憶] 削除: {sid}"
        assert store.rebuild()["sessions"] == 0


@pytest.mark.parametrize("command", ["list", "show", "stats", "rebuild", "summarize"])
def test_cli_commands(command, monkeypatch, capsys):
    sid = conversation(count=1)
    assert main([command, sid] if command == "show" else [command]) == 0
    assert capsys.readouterr().out


def test_background_excludes_current_and_does_not_raise(monkeypatch):
    sid = conversation()
    calls = []
    monkeypatch.setattr(summarize, "summarize_pending", lambda exclude: calls.append(exclude))
    thread = _BACKGROUND_START(sid)
    thread.join(2)
    assert calls == [sid] and thread.daemon


def test_invalid_id_cannot_escape_memory():
    with pytest.raises(ValueError):
        store.forget("../../outside")


def test_jsonl_survives_index_failure_and_next_turn(monkeypatch):
    sid = conversation(count=1, end=False)
    original = store._apply
    def fail(conn, session_id, record):
        if record['type'] == 'turn' and record['text'] == 'DB失敗':
            raise OSError('索引だけ失敗')
        return original(conn, session_id, record)
    monkeypatch.setattr(store, '_apply', fail)
    store.safe_log_turn(sid, 'text', 'owner', 'DB失敗')
    monkeypatch.setattr(store, '_apply', original)
    store.log_turn(sid, 'text', 'owner', '次の発言')
    # T-2203: 発言のたびに原文を読み直さない。索引だけ失敗した発言は、rebuild で原文から戻る
    store.rebuild()
    after = store.get_session(sid)
    assert [r['text'] for r in after['turns']][-2:] == ['DB失敗', '次の発言']
    path = store._path(sid)
    assert sum('"type": "turn"' in line for line in path.read_text(encoding='utf-8').splitlines()) == len(after['turns'])


def test_chat_records_only_history_eligible_turns(monkeypatch):
    import io
    import sys
    # chat は Windows 用に標準入出力を reconfigure する。
    monkeypatch.setattr(sys, "stdin", io.TextIOWrapper(io.BytesIO(), encoding="utf-8"))
    monkeypatch.setattr(sys, "stdout", io.TextIOWrapper(io.BytesIO(), encoding="utf-8"))
    import chat
    outcomes = iter([
        orc.Outcome("obsidian-note", "rule", {}, "private答え", True, 0, private=True),
        orc.Outcome("knowledge-capture", "rule", {}, "候補", True, 0),
        orc.Outcome("general-chat", "default", {}, "失敗答え", False, 0),
        orc.Outcome("general-chat", "default", {}, "成功答え", True, 0),
    ])
    feed = iter(["private依頼", "ナレッジ化して", "失敗依頼", "通常依頼", "quit"])
    monkeypatch.setattr("builtins.input", lambda prompt: next(feed))
    monkeypatch.setattr(orc, "handle", lambda *a, **kw: next(outcomes))
    monkeypatch.setattr(orc, "start_warm_up", lambda **kw: None)
    monkeypatch.setattr(chat, "record", lambda *a: None)
    chat.main()
    rows = store.list_sessions()
    assert len(rows) == 1 and rows[0]["channel"] == "text" and rows[0]["ended_at"]
    assert [t["text"] for t in store.get_session(rows[0]["session_id"])["turns"]] == ["失敗依頼", "通常依頼", "成功答え"]


def test_web_records_only_eligible_turns_and_heard_audio():
    from tests.test_voice_web import make_session
    from voice.web.session import _TurnState
    session, events, fake = make_session()
    try:
        session.speaker.heard = lambda turn: ["聞こえた部分"]
        for skill, ok, private in [("obsidian-note", True, True), ("knowledge-capture", True, False),
                                   ("general-chat", False, False), ("general-chat", True, False)]:
            state = _TurnState(1, None)
            state.text = skill + str(ok)
            state.outcome = orc.Outcome(skill, "rule", {}, "回答全体", ok, 0, private=private)
            session._remember(state)
        store.flush()                     # 記録は裏のスレッドで書く（T-2203）
        row = store.get_session(session.ctx.session_id)
        assert row["channel"] == "web"
        assert [t["text"] for t in row["turns"]] == ["general-chatFalse", "general-chatTrue", "聞こえた部分"]
        session.end(capture=True)
        assert store.get_session(session.ctx.session_id)["ended_at"]
    finally:
        session.end()


def test_summary_validation_allows_blank_lines_and_fullwidth_colon():
    from memory import summarize
    text = "話題：記憶\n\n決まったこと: Mem0 は後で\n残っていること: なし\nOwner の好みや事情：簡単な構成\n"
    assert summarize.validate(text).splitlines() == [
        "話題: 記憶", "決まったこと: Mem0 は後で", "残っていること: なし", "Owner の好みや事情: 簡単な構成"]


# ---------------------------------------------------------------- T-2203 会話を待たせない

def test_add_turn_does_not_wait_for_slow_or_failing_writes(monkeypatch):
    """記録の書き込みが遅くても失敗しても、add_turn はすぐ戻り、履歴は入る（ブラウザの会話のロックの中で呼ばれるため）。"""
    import time as _time
    gate = threading.Event()

    def slow_log(*args):
        gate.wait(5)
        raise RuntimeError("書けない")
    monkeypatch.setattr(store, "log_turn", slow_log)
    ctx = orc.Context()
    started = _time.monotonic()
    orc.add_turn(ctx, "owner", "こんにちは")
    assert _time.monotonic() - started < 0.5 and ctx.history[-1].text == "こんにちは"
    gate.set()
    assert store.flush()


def test_end_session_writes_rest_then_starts_detached_summary(monkeypatch):
    from memory import summarize
    calls = []
    monkeypatch.setattr(summarize, "start_detached", lambda: calls.append(store.list_sessions()))
    ctx = orc.Context()
    orc.add_turn(ctx, "owner", "一つ目")
    orc.add_turn(ctx, "owner", "二つ目")
    orc.end_session(ctx)
    assert len(calls) == 1 and calls[0][0]["ended_at"] and calls[0][0]["owner_turns"] == 2   # 書き終えてから要約


def test_warm_up_and_web_start_do_not_summarize(monkeypatch):
    from memory import summarize
    calls = []
    monkeypatch.setattr(summarize, "start_background_summary", lambda *a, **k: calls.append("bg"))
    monkeypatch.setattr(summarize, "start_detached", lambda *a, **k: calls.append("detached"))
    monkeypatch.setattr(orc, "warm_up", lambda: None)
    orc.start_warm_up().join(5)
    from tests.test_voice_web import make_session
    session, _, _ = make_session()
    session.end()
    assert calls == []


def test_start_detached_runs_memory_summarize_in_new_session(monkeypatch):
    import importlib
    import subprocess
    summarize = importlib.reload(importlib.import_module("memory.summarize"))   # conftest の差し替えを外した本物
    seen = {}
    monkeypatch.setattr(subprocess, "Popen", lambda args, **kw: seen.update(args=args, **kw))
    summarize.start_detached()
    assert seen["args"][1:] == ["-m", "memory", "summarize"] and seen["start_new_session"] is True
