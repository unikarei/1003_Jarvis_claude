"""会話ごとのハーネスのセッションと引き継ぎ・道具・記憶のメモ（ADR-0038、../docs/spec.md §3.4.12 G〜I・§3.3.14 I、
T-2402〜T-2407）。本物の LLM は呼ばない（偽のセッション、偽の claude のスクリプト）。"""

import importlib
import io
import json
import os
import stat
import sys
import time

import pytest

import config
import harness_session as hs
import jarvis_mcp
import llm
import orchestrator as orc
import prompts
from ckc.models import Speaker, Turn
from memory import recall, store
from tests.test_route_chat import index  # noqa: F401  索引と DB（振り分けに LLM を呼ばない）


# ---------------------------------------------------------------- 既定と順番（T-2403）

def test_default_harness_is_claude_when_unset(monkeypatch):
    monkeypatch.delenv("JARVIS_HARNESS", raising=False)
    monkeypatch.delenv("JARVIS_HARNESS_SESSION", raising=False)
    fresh = importlib.reload(config)
    try:
        assert fresh.HARNESS == "claude" and fresh.HARNESS_SESSION is True
        assert fresh.WEB_SEARCH is True and fresh.HANDOFF_EXCHANGES == 6
    finally:
        monkeypatch.setenv("JARVIS_HARNESS", "auto")
        monkeypatch.setenv("JARVIS_HARNESS_SESSION", "0")
        importlib.reload(config)


@pytest.mark.parametrize("harness, order", [("claude", ("claude", "codex", "ollama")),
                                            ("codex", ("codex", "claude", "ollama")),
                                            ("ollama", ("ollama", "codex", "claude"))])
def test_chosen_order_is_same_for_both_paths(harness, order):
    ctx = orc.Context(harness=harness)
    assert orc.harness_order(ctx, "chat") == order == orc.harness_order(ctx, "answer")


def test_auto_order_is_unchanged():
    ctx = orc.Context(harness="auto")
    assert orc.harness_order(ctx, "chat") == ("ollama", "codex", "claude")
    assert orc.harness_order(ctx, "answer")[0] == "codex"


def test_renew_context_keeps_switch_and_rest():
    ctx = orc.Context(harness="codex", channel="web")
    ctx.resting["claude"] = (9e9, "limit")
    ctx.history.append(Turn(Speaker.OWNER, "a"))
    new = orc.renew_context(ctx, voice=True, terminal=False)
    assert (new.harness, new.session_id, new.channel, new.voice, new.terminal) == ("codex", ctx.session_id, "web", True, False)
    assert new.resting == ctx.resting and new.resting is not ctx.resting and len(new.history) == 1


# ---------------------------------------------------------------- 引き継ぎメモ（T-2402）

def _turns(n):
    out = []
    for i in range(n):
        out.append(Turn(Speaker.OWNER, f"発言{i}" + "い" * 150))
        out.append(Turn(Speaker.AI, f"答え{i}です。続きの文があります。"))
    return out


def test_handoff_memo_keeps_recent_exchanges_and_shortens_older():
    memo = hs.handoff_memo(_turns(10), exchanges=6)
    assert f"Owner: 発言9{'い' * 150}" in memo and f"Owner: 発言4{'い' * 150}" in memo   # 直近 6 往復はそのまま
    assert "それより前の会話の短いまとめ" in memo and "Owner: 発言0" in memo
    assert f"発言0{'い' * 150}" not in memo                     # 前の Owner の発言は 100 字まで
    assert "JARVIS: 答え0です。" in memo and "答え0です。続き" not in memo   # 答えは最初の 1 文
    assert memo.index("発言0") < memo.index("ここから直近の会話") < memo.index("発言9")


def test_handoff_memo_older_part_is_capped_newest_first():
    memo = hs.handoff_memo(_turns(60), exchanges=0)
    assert len(memo) <= hs.OLDER_TOTAL_CHARS + 200 and "発言59" in memo and "発言0" not in memo


def test_handoff_memo_empty_and_dict_turns():
    assert hs.handoff_memo([]) == "（なし）"
    memo = hs.handoff_memo([{"speaker": "owner", "text": "こんにちは"}, {"speaker": "ai", "text": "はい。"}])
    assert memo == "Owner: こんにちは\nJARVIS: はい。"


def test_canonical_turns_prefers_record(monkeypatch):
    ctx = orc.Context()
    orc.add_turn(ctx, Speaker.OWNER, "記録の発言 sk-ant-api03-" + "x" * 30)
    store.flush()
    turns = hs.canonical_turns(ctx)
    assert isinstance(turns[0], dict) and "sk-ant" not in turns[0]["text"]   # 正本（伏せ字済み）
    monkeypatch.setattr(config, "MEMORY_ENABLED", False)
    assert hs.canonical_turns(ctx) == ctx.history


# ---------------------------------------------------------------- セッション（T-2404・T-2405）

class FakeHandle:
    def __init__(self, kind, log, fail=None):
        self.kind, self.log, self.fail, self.closed, self.messages = kind, log, fail or [], False, []

    def alive(self):
        return not self.closed

    def ask(self, message, on_text, timeout=None, cancel=None):
        self.messages.append(message)
        if self.fail:
            raise self.fail.pop(0)
        on_text(f"{self.kind}の答え{len(self.messages)}")
        return f"{self.kind}の答え{len(self.messages)}", (["mcp__jarvis__memory_note"] if "覚えて" in message else [])

    def close(self):
        self.closed = True
        self.log.append(("close", self.kind))


@pytest.fixture
def sessions(monkeypatch, index):  # noqa: F811
    monkeypatch.setattr(config, "HARNESS_SESSION", True)
    log, handles, fails = [], [], {}

    def start(ctx, harness):
        log.append(("start", harness))
        handle = FakeHandle(harness, log, fails.pop(harness, None))
        handles.append(handle)
        return handle
    monkeypatch.setattr(hs, "_start", start)
    one_shot = []
    monkeypatch.setattr(llm, "complete_stream", lambda prompt, on_text, provider=None, timeout=None, cancel=None:
                        one_shot.append((provider, prompt)) or (on_text(f"{provider}の答え") or f"{provider}の答え"))
    import knowledge_engine as ke
    monkeypatch.setattr(ke, "search", lambda *a, **k: [])
    hs.close_all()
    yield {"log": log, "handles": handles, "fails": fails, "one_shot": one_shot}
    hs.close_all()


def _say(text, ctx):
    out = orc.handle(text, ctx, use_llm=False)
    if not out.private:
        orc.add_turn(ctx, Speaker.OWNER, text)
        if out.ok:
            orc.add_turn(ctx, Speaker.AI, out.text)
    return out


def test_same_conversation_reuses_one_session(sessions):
    ctx = orc.Context(harness="claude", stream=lambda t: None)
    first = _say("こんにちは", ctx)
    second = _say("今日はいい天気ですね", ctx)
    assert [e for e in sessions["log"] if e[0] == "start"] == [("start", "claude")]   # 起動は 1 回
    assert first.text == "claudeの答え1" and second.text == "claudeの答え2"
    m1, m2 = sessions["handles"][0].messages
    assert "# これまでの会話（引き継ぎ）" in m1 and "# Owner の発言\nこんにちは" in m1
    assert "引き継ぎ" not in m2 and "こんにちは" not in m2 and "# Owner の発言\n今日はいい天気ですね" in m2
    assert sessions["one_shot"] == []


def test_prewarmed_session_still_gets_first_message(sessions):
    ctx = orc.Context(harness="claude", stream=lambda t: None)
    ctx.history.append(Turn(Speaker.OWNER, "前もって話したこと"))
    hs.prewarm(ctx).join(timeout=5)
    assert [e for e in sessions["log"] if e[0] == "start"] == [("start", "claude")]
    _say("続き", ctx)
    assert len(sessions["handles"]) == 1                       # 前もって起動したものを使う
    assert "引き継ぎ" in sessions["handles"][0].messages[0] and "前もって話したこと" in sessions["handles"][0].messages[0]
    assert hs.prewarm(orc.Context(harness="ollama")) is None


def test_between_turns_and_unsent_memory_only(sessions, monkeypatch):
    ctx = orc.Context(harness="claude", stream=lambda t: None)
    packs = iter([recall.MemoryPack("- 記憶A", [{"text": "- 記憶A"}]),
                  recall.MemoryPack("- 記憶A\n- 記憶B", [{"text": "- 記憶A"}, {"text": "- 記憶B"}])])
    monkeypatch.setattr(orc, "_recall_pack", lambda q, c: next(packs))
    _say("一つ目", ctx)
    orc.add_turn(ctx, Speaker.OWNER, "ナレッジは何件？")        # セッションの外で答えた発言（経路 C など）
    orc.add_turn(ctx, Speaker.AI, "知識は 10 件です。")
    _say("二つ目", ctx)
    m1, m2 = sessions["handles"][0].messages
    assert "- 記憶A" in m1
    assert "# この間の会話" in m2 and "Owner: ナレッジは何件？" in m2 and "JARVIS: 知識は 10 件です。" in m2
    assert "Owner: 一つ目" not in m2 and "- 記憶B" in m2 and "- 記憶A" not in m2


def test_answer_path_puts_sources_in_turn(sessions, monkeypatch):
    import knowledge_engine as ke
    from knowledge_engine import Hit
    hit = Hit("k1", "大洗の物件", 1.0, [], "https://x/1", "2026-11-01", "")
    monkeypatch.setattr(ke, "prepare", lambda q, projects=None: ("default", "[1] 大洗の物件\n本文", [hit]))
    ctx = orc.Context(harness="claude", stream=lambda t: None)
    text, items = orc._answer({"question": "大洗の物件は？"}, ctx, "大洗の物件は？")
    message = sessions["handles"][0].messages[0]
    assert "# 質問\n大洗の物件は？" in message and "[1] 大洗の物件" in message and "資料の使い方" in message


def test_switch_closes_old_session_and_hands_off(sessions):
    ctx = orc.Context(harness="claude", stream=lambda t: None)
    _say("秋祭りの会場はつばめ公園", ctx)
    _say("Codex にして", ctx)
    out = _say("会場はどこ？", ctx)
    assert ("close", "claude") in sessions["log"] and ("start", "codex") in sessions["log"]
    codex_first = sessions["handles"][1].messages[0]
    assert "引き継ぎ" in codex_first and "つばめ公園" in codex_first and out.text == "codexの答え1"
    _say("Claude にして", ctx)
    _say("もう一度", ctx)
    assert [e for e in sessions["log"] if e == ("start", "claude")] == [("start", "claude")] * 2   # 戻るときも新しく
    assert "つばめ公園" in sessions["handles"][2].messages[0]


def test_automatic_switch_hands_off_to_next_harness(sessions):
    ctx = orc.Context(harness="claude", stream=lambda t: None)
    _say("予算は8万円", ctx)
    sessions["handles"][0].fail = [llm.LLMUnavailable("You've hit your usage limit.", "limit")]
    out = _say("予算は？", ctx)
    assert out.text.startswith("Claude の利用枠が上限のため、Codex で答えます。")
    assert "予算は8万円" in sessions["handles"][1].messages[0]
    assert ctx.resting["claude"][1] == "limit"


def test_ollama_has_no_session_and_gets_memo(sessions):
    ctx = orc.Context(harness="ollama", stream=lambda t: None)
    _say("一つ目", ctx)
    _say("二つ目", ctx)
    assert sessions["log"] == [] and [p for p, _ in sessions["one_shot"]] == ["ollama", "ollama"]
    assert "Owner: 一つ目" in sessions["one_shot"][1][1]


def test_end_session_and_idle_close(sessions, monkeypatch):
    ctx = orc.Context(harness="claude", stream=lambda t: None)
    _say("一つ目", ctx)
    from memory import summarize
    monkeypatch.setattr(summarize, "start_detached", lambda: None)
    orc.end_session(ctx)
    assert hs.current(ctx.session_id) is None and ("close", "claude") in sessions["log"]
    ctx2 = orc.Context(harness="claude", stream=lambda t: None)
    t = {"v": 1000.0}
    monkeypatch.setattr(hs, "clock", lambda: t["v"])
    _say("a", ctx2)
    t["v"] += config.SESSION_IDLE_MINUTES * 60 + 1
    _say("b", ctx2)
    assert [e for e in sessions["log"] if e[0] == "start"] == [("start", "claude")] * 3   # 会話 2 つ＋無発言の後の作り直し


def test_cancel_and_lost_rebuild(sessions):
    ctx = orc.Context(harness="claude", stream=lambda t: None)
    _say("一つ目", ctx)
    sessions["handles"][0].fail = [llm.ClaudeSessionLost("落ちた")]
    out = _say("二つ目", ctx)                                   # 送る前に落ちていた → その場で作り直して答える
    assert out.text == "claudeの答え1" and len(sessions["handles"]) == 2
    assert "Owner: 一つ目" in sessions["handles"][1].messages[0]
    sessions["handles"][1].fail = [llm.LLMCancelled("取り消されました")]
    with pytest.raises(llm.LLMCancelled):
        hs.answer(ctx, "claude_warm", "chat", "三つ目", "三つ目", stream=lambda t: None)
    assert hs.current(ctx.session_id) is None                 # 取り消したら捨てる


def test_old_worker_failure_does_not_close_new_session(sessions):
    """声の割り込みで裏に残った前の答えが失敗しても、同じ会話で新しく作ったセッションは閉じない。"""
    ctx = orc.Context(harness="claude", stream=lambda t: None)
    _say("一つ目", ctx)
    old = hs.current(ctx.session_id)
    old.busy.acquire()                                        # 前の答えがまだ動いている
    _say("二つ目", ctx)                                       # 使用中 → 捨てて新しいセッションで答える
    new = hs.current(ctx.session_id)
    assert new is not old and new.handle.messages
    old.busy.release()
    hs._discard(old)                                          # 前の答えの失敗の後片付け
    assert hs.current(ctx.session_id) is new and not new.handle.closed


def test_codex_unavailable_falls_back_to_one_shot(sessions, monkeypatch):
    def start(ctx, harness):
        raise llm.CodexSessionUnavailable("使用中")
    monkeypatch.setattr(hs, "_start", start)
    ctx = orc.Context(harness="codex", stream=lambda t: None)
    out = _say("こんにちは", ctx)
    assert out.text == "codexの答え" and sessions["one_shot"][0][0] == "codex"


def test_tools_used_are_recorded_by_name(sessions):
    ctx = orc.Context(harness="claude", stream=lambda t: None)
    out = orc.handle("これ覚えておいて: 好きな色は群青色", ctx, use_llm=False)
    assert out.llm["tools"] == ["mcp__jarvis__memory_note"] and out.llm["harness"] == "claude"


# ---------------------------------------------------------------- 会話の間保つ claude -p（偽のスクリプト）

FAKE_SESSION_CLAUDE = r'''#!{python}
import json, os, sys
with open(os.environ["FAKE_CLAUDE_LOG"], "a") as f:
    f.write(json.dumps({{"pid": os.getpid(), "argv": sys.argv[1:], "key": os.environ.get("ANTHROPIC_API_KEY")}}) + "\n")
n = 0
for line in sys.stdin:
    n += 1
    text = json.loads(line)["message"]["content"]
    def out(e):
        sys.stdout.write(json.dumps(e, ensure_ascii=False) + "\n"); sys.stdout.flush()
    if "道具" in text:
        out({{"type": "stream_event", "event": {{"type": "message_start"}}}})
        out({{"type": "stream_event", "event": {{"delta": {{"type": "text_delta", "text": "調べます。"}}}}}})
        name = "Bash" if "禁止" in text else "mcp__jarvis__jarvis_status"
        out({{"type": "assistant", "message": {{"content": [{{"type": "tool_use", "name": name, "input": {{}}}}]}}}})
        out({{"type": "stream_event", "event": {{"type": "message_start"}}}})
    out({{"type": "stream_event", "event": {{"delta": {{"type": "text_delta", "text": f"答え{{n}}@{{os.getpid()}}"}}}}}})
    if "落ちる" in text:
        sys.exit(1)
    out({{"type": "result", "subtype": "success", "is_error": False, "result": f"答え{{n}}@{{os.getpid()}}"}})
'''


@pytest.fixture
def fake_session_claude(tmp_path, monkeypatch):
    script = tmp_path / "claude"
    script.write_text(FAKE_SESSION_CLAUDE.format(python=sys.executable), encoding="utf-8")
    script.chmod(0o755)
    log = tmp_path / "starts.jsonl"
    monkeypatch.setattr(llm, "CLAUDE_BIN", str(script))
    monkeypatch.setenv("FAKE_CLAUDE_LOG", str(log))
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test-not-used")
    def starts(at_least=1):
        deadline = time.monotonic() + 5
        while True:
            entries = [json.loads(x) for x in log.read_text().splitlines()] if log.exists() else []
            if len(entries) >= at_least or time.monotonic() > deadline:
                return entries
            time.sleep(0.02)
    return starts


def test_claude_session_keeps_one_process(fake_session_claude):
    session = llm.ClaudeSession("決まり", {"mcpServers": {}}, web_search=True)
    try:
        got = []
        first, _ = session.ask("一", got.append)
        second, _ = session.ask("二", got.append)
        starts = fake_session_claude()
        assert len(starts) == 1 and first == f"答え1@{starts[0]['pid']}" and second == f"答え2@{starts[0]['pid']}"
        argv = starts[0]["argv"]
        for flag in ("--restricted", "--strict-mcp-config", "--no-session-persistence", "--disable-slash-commands"):
            assert flag in argv
        assert argv[argv.index("--permission-mode") + 1] == "dontAsk"
        assert argv[argv.index("--tools") + 1] == "WebSearch,WebFetch"
        assert set(argv[argv.index("--allowedTools") + 1].split(",")) == {*llm.CLAUDE_MCP_TOOLS, *llm.CLAUDE_WEB_TOOLS}
        assert argv[argv.index("--system-prompt") + 1] == "決まり"
        assert starts[0]["key"] is None                              # API キーを渡さない（ADR-0020）
        config_path = argv[argv.index("--mcp-config") + 1]
        assert stat.S_IMODE(os.stat(config_path).st_mode) == 0o600
    finally:
        session.close()
    assert not session.alive()


def test_claude_session_tools_and_guard(fake_session_claude):
    session = llm.ClaudeSession("決まり", {"mcpServers": {}}, web_search=False)
    try:
        text, tools = session.ask("道具を使って", lambda t: None)
        assert tools == ["mcp__jarvis__jarvis_status"] and text.startswith("調べます。\n答え1")
        with pytest.raises(llm.LLMError, match="許可していない道具"):
            session.ask("禁止の道具を使って", lambda t: None)
    finally:
        session.close()
    argv = fake_session_claude()[0]["argv"]
    assert argv[argv.index("--tools") + 1] == "" and "WebSearch" not in argv[argv.index("--allowedTools") + 1]


def test_claude_session_without_tools_and_lost(fake_session_claude):
    session = llm.ClaudeSession("決まり", None)
    try:
        assert "--mcp-config" not in fake_session_claude()[0]["argv"]
        with pytest.raises(llm.ClaudeSessionLost):
            session.ask("落ちる", lambda t: None)
        assert not session.alive()
    finally:
        session.close()


def test_claude_session_missing_binary(monkeypatch, tmp_path):
    monkeypatch.setattr(llm, "CLAUDE_BIN", str(tmp_path / "none"))
    with pytest.raises(llm.LLMUnavailable) as e:
        llm.ClaudeSession("決まり")
    assert llm.failure_reason(e.value) == "missing"


def test_mcp_config_points_to_current_paths(monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "sk-not-passed")
    cfg = hs._mcp_config("sess_20261114_210000_abcd")["mcpServers"]["jarvis"]
    import db
    assert cfg["args"][-2:] == ["--session", "sess_20261114_210000_abcd"] and cfg["args"][0].endswith("jarvis_mcp.py")
    assert cfg["env"]["JARVIS_DB_PATH"] == str(db.DB_PATH) and cfg["env"]["JARVIS_MEMORY_DIR"] == str(config.MEMORY_DIR)
    assert "OPENAI_API_KEY" not in cfg["env"] and "ANTHROPIC_API_KEY" not in cfg["env"]


# ---------------------------------------------------------------- プロンプト（T-2407）

def test_tool_rules_describe_tools_and_keep_honesty():
    text = prompts.rules(tools=True)
    for word in ("knowledge_search", "memory_recall", "memory_note", "jarvis_status", "ナレッジ化して",
                 "その話は記憶にありません", "いま話している相手", "『Codex にして』", "操作を引き受けたように言わない"):
        assert word in text
    assert "道具を使えない" not in text
    plain = prompts.rules(tools=False)
    assert "道具を使えない" in plain and "memory_note" not in plain and "Obsidian" in plain


# ---------------------------------------------------------------- 記憶のメモ（T-2406）

def test_note_is_masked_stored_and_rebuilt():
    sid = "sess_20261114_210000_abcd"
    note = store.add_note(sid, "decision", "来年の旅行は奥入瀬。鍵は sk-ant-api03-" + "y" * 30)
    assert "sk-ant" not in note["text"] and "奥入瀬" in note["text"]
    path = config.MEMORY_DIR / "notes" / note["at"][:4] / f"{note['at'][5:7]}.jsonl"
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert [n["note_id"] for n in store.list_notes(sid)] == [note["note_id"]]
    assert store.rebuild()["notes"] >= 1 and store.count_notes(sid) == 1
    with pytest.raises(ValueError):
        store.add_note(sid, "secret", "x")
    with pytest.raises(ValueError):
        store.add_note(sid, "fact", "あ" * 201)
    assert store.forget_note(note["note_id"]) and store.count_notes(sid) == 0
    assert note["note_id"] not in path.read_text()


def test_forget_session_removes_its_notes():
    ctx = orc.Context()
    orc.add_turn(ctx, Speaker.OWNER, "発言")
    store.flush()
    store.add_note(ctx.session_id, "fact", "消える事実")
    other = store.add_note("", "fact", "残る事実")
    assert store.forget(ctx.session_id)
    assert [n["note_id"] for n in store.list_notes()] == [n["note_id"] for n in store.list_notes() if n["text"] == "残る事実"]
    assert any(n["note_id"] == other["note_id"] for n in store.list_notes())


def test_recall_puts_notes_before_summaries():
    sid_old = "sess_20261101_100000_0001"
    store.log_turn(sid_old, "text", "owner", "旅行の話")
    store.log_turn(sid_old, "text", "owner", "二つ目")
    store.end_session(sid_old)
    store.save_summary(sid_old, "話題: 旅行\n決まったこと: なし\n残っていること: なし\nOwner の好みや事情: なし")
    store.add_note("", "preference", "好きな和菓子は水ようかん")
    pack = recall.recall("旅行", orc.Context())
    assert "[記憶のメモ" in pack.text and pack.text.index("記憶のメモ") < pack.text.index("会話の要約")


def test_recall_survives_old_db_without_notes_table(monkeypatch):
    import sqlite3
    import db
    with sqlite3.connect(db.DB_PATH) as conn:
        conn.execute("DROP TABLE IF EXISTS memory_notes")
    try:
        assert isinstance(recall.recall("何か", orc.Context()), recall.MemoryPack)
    finally:
        db.init_db()


def test_search_finds_current_conversation_turns():
    ctx = orc.Context()
    orc.add_turn(ctx, Speaker.OWNER, "結婚式の前日は新千歳空港に着く")
    store.flush()
    items = recall.search("新千歳空港", ctx.session_id)
    assert items and "今の会話の発言" in items[0]["text"] and "新千歳" in items[0]["text"]


# ---------------------------------------------------------------- Jarvis の MCP サーバー（T-2407）

def _rpc(lines, session="sess_20261114_210000_abcd"):
    out = io.StringIO()
    jarvis_mcp.serve(io.StringIO("\n".join(json.dumps(x, ensure_ascii=False) for x in lines) + "\n"), out, session)
    return [json.loads(x) for x in out.getvalue().splitlines()]


def test_mcp_lists_only_four_tools():
    replies = _rpc([{"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {"protocolVersion": "2025-06-18"}},
                    {"jsonrpc": "2.0", "method": "notifications/initialized"},
                    {"jsonrpc": "2.0", "id": 2, "method": "tools/list"},
                    {"jsonrpc": "2.0", "id": 3, "method": "resources/list"}])
    assert len(replies) == 3 and replies[0]["result"]["protocolVersion"] == "2025-06-18"
    assert [t["name"] for t in replies[1]["result"]["tools"]] == list(jarvis_mcp.TOOL_NAMES)
    assert set(jarvis_mcp.TOOL_NAMES) == {"knowledge_search", "memory_recall", "memory_note", "jarvis_status"}
    assert replies[2]["error"]["code"] == -32601


def test_mcp_memory_note_writes_and_checks(monkeypatch):
    call = lambda i, name, args: {"jsonrpc": "2.0", "id": i, "method": "tools/call",  # noqa: E731
                                  "params": {"name": name, "arguments": args}}
    replies = _rpc([call(1, "memory_note", {"text": "好きな色は群青色", "kind": "preference"}),
                    call(2, "memory_note", {"text": "x", "kind": "secret"}),
                    call(3, "memory_note", {"text": "あ" * 300, "kind": "fact"}),
                    call(4, "rm_rf", {}),
                    call(5, "memory_recall", {"query": "群青色"})])
    assert replies[0]["result"]["isError"] is False and "群青色" in replies[0]["result"]["content"][0]["text"]
    assert [r["result"]["isError"] for r in replies[1:4]] == [True, True, True]
    assert "群青色" in replies[4]["result"]["content"][0]["text"]
    assert [n["text"] for n in store.list_notes("sess_20261114_210000_abcd")] == ["好きな色は群青色"]


def test_mcp_note_limit_per_session(monkeypatch):
    monkeypatch.setattr(jarvis_mcp, "NOTES_PER_SESSION", 1)
    sid = "sess_20261114_220000_beef"
    call = lambda i: {"jsonrpc": "2.0", "id": i, "method": "tools/call",  # noqa: E731
                      "params": {"name": "memory_note", "arguments": {"text": f"事実{i}", "kind": "fact"}}}
    replies = _rpc([call(1), call(2)], session=sid)
    assert [r["result"]["isError"] for r in replies] == [False, True]


def test_mcp_status_and_search_are_read_only(index):  # noqa: F811
    import db
    before = db.DB_PATH.stat().st_mtime_ns
    replies = _rpc([{"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {"name": "jarvis_status", "arguments": {}}},
                    {"jsonrpc": "2.0", "id": 2, "method": "tools/call",
                     "params": {"name": "knowledge_search", "arguments": {"query": "大洗"}}}])
    status = replies[0]["result"]["content"][0]["text"]
    assert "ナレッジの接続状況" in status and "記憶のメモ" in status and "Web 検索" in status
    assert replies[1]["result"]["isError"] is False
    assert db.DB_PATH.stat().st_mtime_ns == before
