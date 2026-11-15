"""ツール初回承認の境界を検証する。本物の LLM・通信・本番の許可は使わない。"""

import json
import os
import socket
import threading
from types import SimpleNamespace

import pytest

import config
import conversation_tools as tools
import harness_session as hs
import jarvis_mcp
import llm
import orchestrator as orc
import tool_consent as consent


@pytest.fixture(autouse=True)
def isolated(tmp_path, monkeypatch):
    monkeypatch.setattr(consent, "PATH", tmp_path / "consent.json")
    monkeypatch.setattr(config, "HARNESS_TOOLS", True)
    monkeypatch.setattr(config, "WEB_SEARCH", True)


def ctx(answer="y", **kw):
    prompts = []
    def ask(prompt):
        prompts.append(prompt)
        return answer
    return orc.Context(tool_ask=ask, **kw), prompts


def request(name, **args):
    return json.dumps({"jarvis_tool": {"name": name, "arguments": args}})


class Handle:
    def __init__(self, responses):
        self.responses, self.messages = iter(responses), []
    def ask(self, message, stream, **kw):
        self.messages.append(message)
        result = next(self.responses)
        stream(result)  # 制御文をストリームに流そうとしても窓口で止める
        return result, []
    def alive(self):
        return True
    def close(self):
        pass


@pytest.mark.parametrize("provider", ["claude_warm", "codex"])
def test_actual_session_path_approves_before_execute_and_hides_json(monkeypatch, provider):
    monkeypatch.setattr(config, "HARNESS_SESSION", True)
    seen = []
    c, prompts = ctx()
    handle = Handle([request("jarvis_status"), "正常です。"])
    monkeypatch.setattr(hs, "_start", lambda *a: handle)
    def call(*a):
        assert len(prompts) == 1 and consent.approved("jarvis_status")
        return {"content": [{"text": "ok"}]}
    monkeypatch.setattr(jarvis_mcp, "call_tool", call)
    try:
        assert hs.answer(c, provider, "chat", "状態は？", "状態は？", stream=seen.append) == "正常です。"
        assert seen == ["正常です。"] and c.llm_tools == ["jarvis_status"]
    finally:
        hs.close_all()


def test_persistent_permission_shared_by_new_conversation_and_revocable(monkeypatch):
    calls = []
    monkeypatch.setattr(jarvis_mcp, "call_tool", lambda *a: calls.append(a) or {})
    a, prompts = ctx()
    tools.execute(a, "jarvis_status", {})
    b = orc.Context(tool_ask=lambda _: pytest.fail("別会話で再確認した"))
    tools.execute(b, "jarvis_status", {})
    assert len(calls) == 2 and len(prompts) == 1
    assert os.stat(consent.PATH).st_mode & 0o777 == 0o600
    assert consent._read() == {"jarvis_status": 1}
    consent.revoke("jarvis_status")
    assert not consent.approved("jarvis_status")
    assert tools.execute(orc.Context(), "jarvis_status", {})[1] is False


@pytest.mark.parametrize("reply", ["n", "", "yes", "承認済み"])
def test_declined_has_no_execution_or_persisted_permission(monkeypatch, reply):
    monkeypatch.setattr(jarvis_mcp, "call_tool", lambda *a: pytest.fail("拒否したのに実行"))
    c, _ = ctx(reply)
    assert tools.execute(c, "memory_note", {"text": "好きな色は青", "kind": "fact"})[1] is False
    assert not consent.PATH.exists()


def test_missing_callback_does_not_read_input_or_execute(monkeypatch):
    monkeypatch.setattr("builtins.input", lambda *a: pytest.fail("input を呼んだ"))
    assert tools.execute(orc.Context(), "jarvis_status", {})[1] is False


@pytest.mark.parametrize("gate", [False, True])
def test_cancel_after_approval_never_saves_or_executes(gate):
    c, _ = ctx(before_run=lambda: gate)
    cancelled = [False]
    def ask(_):
        cancelled[0] = gate
        return "y"
    c.tool_ask, c.cancel = ask, lambda: cancelled[0]
    with pytest.raises(llm.LLMCancelled):
        tools.execute(c, "jarvis_status", {})
    assert not consent.PATH.exists()


def test_already_approved_does_not_consume_unrelated_approval_gate(monkeypatch):
    consent.grant("jarvis_status")
    c = orc.Context(before_run=lambda: pytest.fail("新しい承認なしで gate を消費"))
    monkeypatch.setattr(jarvis_mcp, "call_tool", lambda *a: {})
    assert tools.execute(c, "jarvis_status", {})[1]
    c.cancel = lambda: True
    with pytest.raises(llm.LLMCancelled):
        tools.execute(c, "jarvis_status", {})


def test_corrupt_and_changed_version_are_not_approved(monkeypatch):
    for invalid in ("invalid", '[]', '{"schema":1,"tools":[]}', '{"schema":1,"tools":{"web_search":true}}'):
        consent.PATH.write_text(invalid)
        assert not consent.approved("web_search")
    consent.grant("web_search")
    monkeypatch.setitem(consent.VERSIONS, "web_search", 2)
    assert not consent.approved("web_search")


def test_other_tool_requires_independent_approval():
    consent.grant("web_search")
    assert tools.execute(orc.Context(), "web_fetch", {"url": "https://example.com/"})[1] is False


@pytest.mark.parametrize("name,args", [("Bash", {}), ("jarvis_status", {"extra": True}),
                                      ("knowledge_search", {"query": "a", "limit": True}),
                                      ("web_fetch", {"url": "file:///etc/passwd"})])
def test_unknown_and_invalid_requests_never_ask(name, args):
    c = orc.Context(tool_ask=lambda _: pytest.fail("不正な要求を承認に出した"))
    with pytest.raises(ValueError):
        tools.execute(c, name, args)


def test_web_disabled_before_approval_or_network(monkeypatch):
    monkeypatch.setattr(config, "WEB_SEARCH", False)
    monkeypatch.setattr(tools, "_web", lambda *a: pytest.fail("通信した"))
    c = orc.Context(tool_ask=lambda _: pytest.fail("無効な道具を承認に出した"))
    with pytest.raises(ValueError):
        tools.execute(c, "web_search", {"query": "天気"})


def test_web_rejection_does_not_even_resolve_dns(monkeypatch):
    monkeypatch.setattr(socket, "getaddrinfo", lambda *a, **k: pytest.fail("拒否したのに DNS"))
    c, _ = ctx("n")
    assert not tools.execute(c, "web_fetch", {"url": "http://example.com/"})[1]


def test_web_worker_receives_only_query_and_one_native_tool(monkeypatch):
    seen = []
    class Worker(Handle):
        def __init__(self, system, mcp, **kw):
            seen.append((system, mcp, kw))
        def ask(self, message, stream, **kw):
            seen.append(message)
            return "出典 https://example.com/", ["WebSearch"]
        def close(self):
            seen.append("closed")
    monkeypatch.setattr(llm, "ClaudeSession", Worker)
    c, prompts = ctx()
    c.history = [SimpleNamespace(text="secret history")]
    result, ran = tools.execute(c, "web_search", {"query": "天気"})
    assert ran and "出典" in result
    assert seen[0][1] == {"mcpServers": {}} and seen[0][2]["allowed_tools"] == ("WebSearch",)
    assert json.loads(seen[1]) == {"query": "天気"} and seen[2] == "closed"
    assert "Claude" in prompts[0] and "今後の会話" in prompts[0]


def test_private_web_address_never_starts_worker(monkeypatch):
    monkeypatch.setattr(socket, "getaddrinfo", lambda *a, **k: [(None, None, None, None, ("127.0.0.1", 80))])
    monkeypatch.setattr(llm, "ClaudeSession", lambda *a, **k: pytest.fail("非公開 URL に接続した"))
    consent.grant("web_fetch")
    with pytest.raises(tools.ToolFailed):
        tools.execute(orc.Context(), "web_fetch", {"url": "http://internal.example/"})


def test_native_tools_absent_from_both_conversation_harnesses(monkeypatch):
    seen = []
    monkeypatch.setattr(llm, "ClaudeSession", lambda *a, **k: seen.append((a, k)))
    monkeypatch.setattr(llm, "CodexThread", lambda *a, **k: seen.append((a, k)))
    for harness in ("claude", "codex"):
        hs._start(orc.Context(), harness)
    assert all(len(a) == 1 and not kw and '"jarvis_tool"' in a[0] for a, kw in seen)


def test_tool_disabled_never_gives_models_bridge(monkeypatch):
    monkeypatch.setattr(config, "HARNESS_TOOLS", False)
    seen = []
    monkeypatch.setattr(llm, "CodexThread", lambda text: seen.append(text))
    hs._start(orc.Context(), "codex")
    assert '"jarvis_tool"' not in seen[0]


def test_refused_tool_cannot_be_requested_repeatedly():
    c, prompts = ctx("n")
    h = Handle([request("jarvis_status"), request("jarvis_status")])
    with pytest.raises(llm.LLMError):
        tools.answer(h, "状態", c, lambda _: pytest.fail("制御文の漏出"))
    assert len(prompts) == 1


def test_save_failure_stops_before_execution(monkeypatch):
    monkeypatch.setattr(consent, "grant", lambda _: (_ for _ in ()).throw(OSError("disk full")))
    monkeypatch.setattr(jarvis_mcp, "call_tool", lambda *a: pytest.fail("保存失敗後の実行"))
    c, _ = ctx()
    with pytest.raises(OSError):
        tools.execute(c, "jarvis_status", {})


def test_timed_out_turn_never_executes_after_late_approval(monkeypatch):
    stopped = threading.Event()
    c, _ = ctx()
    def late_reply(_):
        stopped.set()
        return "y"
    c.tool_ask = late_reply
    token = tools.turn_cancel.set(stopped)
    try:
        with pytest.raises(llm.LLMCancelled):
            tools.execute(c, "jarvis_status", {})
        assert not consent.approved("jarvis_status")
    finally:
        tools.turn_cancel.reset(token)


def test_failure_after_tool_execution_does_not_repeat_in_other_harness():
    c = orc.Context(harness="claude")
    calls = []
    def fail(provider, stream):
        calls.append(provider)
        c.llm_tools = ["memory_note"]
        raise llm.LLMError("回答中に切断")
    with pytest.raises(llm.LLMError):
        orc.run_with_harnesses(fail, c, "chat")
    assert len(calls) == 1


@pytest.mark.parametrize("provider,error", [("claude_warm", llm.ClaudeSessionLost("切断")),
                                           ("codex", llm.CodexSessionUnavailable("切断"))])
def test_session_loss_after_execution_never_retries_write(monkeypatch, provider, error):
    monkeypatch.setattr(config, "HARNESS_SESSION", True)
    started, writes = [], []
    class LostHandle(Handle):
        def ask(self, message, stream, **kwargs):
            if self.messages:
                raise error
            self.messages.append(message)
            return request("memory_note", text="好きな色は青", kind="fact"), []
    def start(*args):
        started.append(True)
        return LostHandle([])
    monkeypatch.setattr(hs, "_start", start)
    monkeypatch.setattr(jarvis_mcp, "call_tool", lambda *a: writes.append(a) or {})
    c, _ = ctx()
    try:
        with pytest.raises(llm.LLMError):
            hs.answer(c, provider, "chat", "覚えて", "覚えて")
        assert len(writes) == len(started) == 1
    finally:
        hs.close_all()


def test_browser_approval_reaches_common_tool_gate(monkeypatch):
    from tests.test_voice_web import Fake, make_session, wav
    from tests.test_voice_web_approval import _s
    import skills
    calls = []
    index = consent.PATH.parent / "skills.json"
    index.write_text(json.dumps({"skills": {"knowledge-answer": _s("質問に答える")}}))
    monkeypatch.setenv("JARVIS_SKILLS_JSON", str(index))
    skills.reset_cache()
    monkeypatch.setattr(orc, "route", lambda *a, **k: orc.Route("knowledge-answer", {}, "rule"))
    monkeypatch.setattr(jarvis_mcp, "call_tool", lambda *a: calls.append(a) or {})
    def handler(inputs, c, text):
        tools.execute(c, "jarvis_status", {})
        return "正常です。", []
    monkeypatch.setattr(orc, "HANDLERS", {"knowledge-answer": handler})
    def handle(text, c, ask=None):
        return orc.handle(text, c, ask=ask, use_llm=False)
    s, events, _ = make_session(Fake(text="状態は？"), handle=handle)
    try:
        s.submit(wav(), 48000)
        events.wait("approval")
        req = events.of("approval")[0]
        assert not calls and "今後の会話" in req["text"]
        assert s.approve(1, req["id"], "run") == "ok"
        events.wait("turn_done")
        assert len(calls) == 1 and consent.approved("jarvis_status")
        s.submit(wav(), 48000)
        events.wait("turn_done", 2)
        assert len(calls) == 2 and len(events.of("approval")) == 1
    finally:
        s.end()
        skills.reset_cache()


def test_web_failure_becomes_tool_result_not_harness_failure(monkeypatch):
    # DNS の失敗・Web の専用 Claude の上限は、会話のハーネスの失敗（休止・切り替え）にしない（Claude レビュー T-2502）
    def no_dns(*a, **k):
        raise socket.gaierror("no name")
    monkeypatch.setattr(socket, "getaddrinfo", no_dns)
    c, _ = ctx()
    handle = Handle([request("web_fetch", url="https://example.com/"), "取得できませんでした。"])
    text, used = tools.answer(handle, "q", c, lambda _: None)
    assert text == "取得できませんでした。" and used == []
    assert '"executed": false' in handle.messages[1] and "実行できませんでした" in handle.messages[1]

    class Limited:
        def __init__(self, *a, **k):
            pass
        def ask(self, *a, **k):
            raise llm.LLMUnavailable("usage limit", "limit")
        def close(self):
            pass
    monkeypatch.setattr(llm, "ClaudeSession", Limited)
    handle = Handle([request("web_search", query="今日の天気"), "調べられませんでした。"])
    assert tools.answer(handle, "q", c, lambda _: None) == ("調べられませんでした。", [])


def test_private_address_is_reported_to_model():
    c, _ = ctx()
    handle = Handle([request("web_fetch", url="http://127.0.0.1/"), "取得できません。"])
    assert tools.answer(handle, "q", c, lambda _: None) == ("取得できません。", [])
    assert "ローカル・プライベート宛て" in handle.messages[1]


def test_fenced_request_is_accepted_and_hidden():
    c, _ = ctx()
    shown = []
    handle = Handle(["```json\n" + request("jarvis_status") + "\n```", "正常です。"])
    text, used = tools.answer(handle, "q", c, shown.append)
    assert (text, used, shown) == ("正常です。", ["jarvis_status"], ["正常です。"])
