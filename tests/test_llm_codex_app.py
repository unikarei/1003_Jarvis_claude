"""常駐の codex app-server（T-2004、../docs/spec.md §3.4.12 D）。本物の codex は呼ばない（偽の app-server のスクリプト）。"""

import json
import os
import sys
import time

import pytest

import llm

FAKE = r"""#!{python}
import json, os, sys, time
log = os.environ["FAKE_CODEX_LOG"]
def note(**k):
    with open(log, "a", encoding="utf-8") as f:
        f.write(json.dumps(k, ensure_ascii=False) + "\n")
note(start=sys.argv[1], pid=os.getpid(), argv=sys.argv[1:],
     keys=sorted(k for k in ("OPENAI_API_KEY", "ANTHROPIC_API_KEY") if k in os.environ))
if sys.argv[1] == "exec":                         # 予備の codex exec
    prompt = sys.stdin.read()
    def out(o):
        print(json.dumps(o, ensure_ascii=False), flush=True)
    out({{"type": "item.completed", "item": {{"id": "m", "type": "agent_message", "text": "exec:" + prompt}}}})
    out({{"type": "turn.completed"}})
    sys.exit(0)
if os.environ.get("FAKE_APP_FAIL_START"):
    sys.exit(3)
def out(o):
    sys.stdout.write(json.dumps(o, ensure_ascii=False) + "\n")
    sys.stdout.flush()
threads = 0
for line in sys.stdin:
    m = json.loads(line)
    method, params, ident = m.get("method"), m.get("params") or {{}}, m.get("id")
    if method == "initialize":
        out({{"id": ident, "result": {{"userAgent": "fake"}}}})
    elif method == "thread/start":
        threads += 1
        note(thread=params)
        out({{"id": ident, "result": {{"thread": {{"id": "t%d" % threads}}}}}})
    elif method == "turn/interrupt":
        note(interrupt=params)
        out({{"method": "turn/completed", "params": {{"threadId": params["threadId"],
             "turn": {{"id": params["turnId"], "status": "interrupted"}}}}}})
    elif method == "turn/start":
        tid = params["threadId"]
        prompt = params["input"][0]["text"]
        out({{"id": ident, "result": {{"turn": {{"id": "u1", "status": "inProgress"}}}}}})
        def ev(meth, **p):
            out({{"method": meth, "params": dict(threadId=tid, turnId="u1", **p)}})
        ev("item/started", item={{"id": "i0", "type": "userMessage"}})
        if prompt == "die":
            sys.exit(5)
        if prompt == "die_mid":
            ev("item/agentMessage/delta", itemId="a", delta="途中")
            sys.exit(5)
        if prompt == "limit":
            ev("error", error={{"message": "You've hit your usage limit."}}, willRetry=False)
            ev("turn/completed", turn={{"id": "u1", "status": "failed", "error": {{"message": "You've hit your usage limit."}}}})
            continue
        if prompt == "tool":
            ev("item/started", item={{"id": "x", "type": "mcpToolCall"}})
            continue                                # 止められるのを待つ
        if prompt == "approval":
            out({{"id": 900, "method": "item/commandExecution/requestApproval", "params": {{"threadId": tid}}}})
            continue
        if prompt == "slow":
            ev("item/agentMessage/delta", itemId="a", delta="最初")
            continue                                # 取り消し・時間切れを待つ
        out({{"method": "item/agentMessage/delta", "params": {{"threadId": "other", "turnId": "z", "itemId": "o", "delta": "別"}}}})
        ev("item/started", item={{"id": "r", "type": "reasoning"}})
        for d in ["答え:", prompt, "@", str(os.getpid())]:
            ev("item/agentMessage/delta", itemId="a", delta=d)
        ev("item/completed", item={{"id": "a", "type": "agentMessage", "text": "答え:" + prompt + "@" + str(os.getpid())}})
        ev("turn/completed", turn={{"id": "u1", "status": "completed"}})
    elif ident is not None and method is None:
        note(reply=m)                               # Jarvis が要求を断った応答
"""


@pytest.fixture
def fake_app(tmp_path, monkeypatch):
    script = tmp_path / "codex"
    script.write_text(FAKE.format(python=sys.executable), encoding="utf-8")
    script.chmod(0o755)
    log = tmp_path / "codex.jsonl"
    monkeypatch.setattr(llm, "CODEX_BIN", str(script))
    monkeypatch.setattr(llm, "CODEX_APP_SERVER", True)
    monkeypatch.setenv("FAKE_CODEX_LOG", str(log))
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test-not-used")
    llm.stop_codex_app_server()

    def entries(key=None, at_least=0):
        deadline = time.monotonic() + 3
        while True:
            rows = [json.loads(x) for x in log.read_text().splitlines()] if log.exists() else []
            if key:
                rows = [r for r in rows if key in r]
            if len(rows) >= at_least or time.monotonic() > deadline:
                return rows
            time.sleep(0.02)
    yield entries
    llm.stop_codex_app_server()


def test_streams_deltas_and_reuses_one_process(fake_app):
    got = []
    first = llm.complete_stream("一", got.append, provider="codex")
    second = llm.complete("二", provider="codex")
    starts = fake_app("start")
    assert [s["start"] for s in starts] == ["app-server"]            # 2 回目は起動しない
    pid = starts[0]["pid"]
    assert first == f"答え:一@{pid}" and got == ["答え:", "一", "@", str(pid)]   # 別のスレッドの差分は渡さない
    assert second == f"答え:二@{pid}"
    threads = fake_app("thread", at_least=2)
    assert len(threads) == 2                                          # 答えごとに新しいスレッド
    params = threads[0]["thread"]
    assert params["sandbox"] == "read-only" and params["approvalPolicy"] == "never" and params["ephemeral"] is True
    assert params["config"]["web_search"] == "disabled"


def test_command_disables_tools_and_has_no_api_key(fake_app):
    llm.complete("問い", provider="codex")
    start = fake_app("start")[0]
    argv = start["argv"]
    assert argv[:3] == ["app-server", "--listen", "stdio://"]
    assert {argv[i + 1] for i, a in enumerate(argv) if a == "--disable"} == set(llm.CODEX_DISABLED_FEATURES)
    assert start["keys"] == []


def test_warm_up_starts_server_once(fake_app):
    assert llm.warm_up_codex() is True and llm.warm_up_codex() is True
    assert len(fake_app("start")) == 1


def test_tool_item_is_stopped_and_interrupted(fake_app):
    started = time.monotonic()
    with pytest.raises(llm.LLMError, match="mcpToolCall"):
        llm.complete("tool", provider="codex")
    assert time.monotonic() - started < 3
    assert fake_app("interrupt", at_least=1)


def test_approval_request_is_rejected(fake_app):
    with pytest.raises(llm.LLMError, match="requestApproval"):
        llm.complete("approval", provider="codex")
    replies = fake_app("reply", at_least=1)
    assert replies[0]["reply"]["id"] == 900 and "error" in replies[0]["reply"]


def test_cancel_interrupts_turn(fake_app):
    got = []
    with pytest.raises(llm.LLMCancelled):
        llm.complete_stream("slow", got.append, provider="codex", cancel=lambda: bool(got))
    assert got == ["最初"] and fake_app("interrupt", at_least=1)
    assert llm.complete("次", provider="codex").startswith("答え:次@")   # 次の答えは同じ app-server で


def test_timeout(fake_app):
    with pytest.raises(llm.LLMError, match="タイムアウト"):
        llm.complete_stream("slow", lambda _t: None, provider="codex", timeout=1)


def test_usage_limit_is_unavailable_without_exec(fake_app):
    with pytest.raises(llm.LLMUnavailable) as e:
        llm.complete("limit", provider="codex")
    assert e.value.reason == "limit"
    assert [s["start"] for s in fake_app("start")] == ["app-server"]   # 同じアカウントなので exec にしない


def test_falls_back_to_exec_when_server_dies_before_answer(fake_app):
    assert llm.complete("die", provider="codex") == "exec:die"
    assert [s["start"] for s in fake_app("start", at_least=2)] == ["app-server", "exec"]
    assert llm.complete("次", provider="codex").startswith("答え:次@")   # 次は起動し直す
    assert [s["start"] for s in fake_app("start", at_least=3)] == ["app-server", "exec", "app-server"]


def test_no_fallback_after_partial_answer(fake_app):
    got = []
    with pytest.raises(llm.LLMError, match="途中"):
        llm.complete_stream("die_mid", got.append, provider="codex")
    assert got == ["途中"] and [s["start"] for s in fake_app("start")] == ["app-server"]


def test_falls_back_to_exec_when_server_cannot_start(fake_app, monkeypatch):
    monkeypatch.setenv("FAKE_APP_FAIL_START", "1")
    assert llm.complete("問い", provider="codex") == "exec:問い"
    assert llm.warm_up_codex() is False


def test_busy_server_uses_exec_without_stopping(fake_app):
    server = llm._take_app_server()
    server.turn_lock.acquire()
    try:
        assert llm.complete("問い", provider="codex") == "exec:問い"
    finally:
        server.turn_lock.release()
    assert llm._app_server is server and server.alive()


def test_disabled_setting_uses_exec_only(fake_app, monkeypatch):
    monkeypatch.setattr(llm, "CODEX_APP_SERVER", False)
    assert llm.complete("問い", provider="codex") == "exec:問い"
    assert llm.warm_up_codex() is False
    assert [s["start"] for s in fake_app("start")] == ["exec"]


def test_stop_closes_server(fake_app):
    llm.complete("問い", provider="codex")
    server = llm._app_server
    llm.stop_codex_app_server()
    assert llm._app_server is None and not server.alive() and not os.path.exists(server.workdir.name)
