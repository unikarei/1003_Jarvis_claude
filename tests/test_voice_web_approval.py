"""ブラウザでの声の会話の画面での承認（T-1504、AT-06、../docs/testing.md §13）のテスト。

確認の要る偽のスキル（呼ばれた回数だけを数え、本物の書き込みをしない）を一時の索引に置き、orchestrator.handle・
orchestrator.execute の実際の確認の経路を通す。認識・合成・ブラウザは偽物、期限は差し替えた時計で確かめる
（60 秒待たない）。本物の LLM・DB・音声の機器・書き込みのあるスキルを使わない。
"""

import json
import threading
import time

import pytest

import config
import llm
import orchestrator as orc
import skills
from tests.test_voice_web import Fake, _open_events, _read_events, call, make_session, wav
from tests.test_voice_web_speech import Clock, until
from voice import tts
from voice.web import server as web
from voice.web.session import APPROVAL_SECONDS, APPROVAL_SPOKEN, WebSession

R1003 = "repo-1003-jarvis-claude"
WRITE = "書いて保存して"          # 偽の書き込みのスキルに当たる発言（ルールで決まる）
THEME = "テーマ登録: 太陽光"       # 端末の入力を待つスキル（theme-register）


def _s(desc, permission="read", phrases=(), inputs=None):
    return {"description": desc, "status": "active", "permission": permission,
            "entry": {"repo": R1003, "function": "x"}, "inputs": inputs or {}, "phrases": list(phrases)}


INDEX = {
    "knowledge-answer": _s("普通の質問に答える", inputs={"question": {"type": "string", "required": True}}),
    "fake-write": _s("偽の書き込み", "local_write", ["書いて保存"]),
    "theme-register": _s("テーマ登録", "local_write", ["テーマ登録:"],
                         {"theme": {"type": "string", "required": True}}),
}


class Counter:
    """偽のスキル。呼ばれた回数だけを数える（本物の書き込みをしない）。"""

    def __init__(self, message):
        self.message, self.calls = message, 0

    def __call__(self, inputs, ctx, text):
        self.calls += 1
        return self.message, []


@pytest.fixture(autouse=True)
def _isolated(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "LATENCY_LOG", tmp_path / "latency" / "latency.jsonl")
    monkeypatch.setattr(tts, "SETTINGS_PATH", tmp_path / "voice" / "settings.json")


@pytest.fixture
def fake_skills(tmp_path, monkeypatch):
    path = tmp_path / "skills.json"
    path.write_text(json.dumps({"skills": INDEX}, ensure_ascii=False), encoding="utf-8")
    monkeypatch.setenv("JARVIS_SKILLS_JSON", str(path))
    skills.reset_cache()
    write, theme, answer = Counter("書き込みました"), Counter("登録しました"), Counter("答えです")
    monkeypatch.setattr(orc, "HANDLERS", {"fake-write": write, "theme-register": theme, "knowledge-answer": answer})

    def no_llm(*a, **k):
        raise AssertionError("LLM を呼ばない")
    monkeypatch.setattr(llm, "complete", no_llm)
    yield {"write": write, "theme": theme, "answer": answer}
    skills.reset_cache()


def real_handle(text, ctx, ask=None):
    """orchestrator.handle（振り分け → execute の確認 → スキル）をそのまま通す。LLM での振り分けはしない。"""
    return orc.handle(text, ctx, ask=ask, use_llm=False)


def start(text=WRITE, clock=None):
    """発言 text を 1 回送り、承認の要求が出るまで待つ。（会話, イベント, 偽の認識, 時計, 要求）。"""
    clock = clock or Clock()
    s, events, fake = make_session(Fake(text=text), handle=real_handle, clock=clock)
    assert s.submit(wav(), 48000) == 1
    events.wait("approval")
    return s, events, fake, clock, events.of("approval")[0]


def done(events, turn=1):
    return [e["result"] for e in events.of("approval_done") if e["turn"] == turn]


# ---------------------------------------------------------------- 承認の要求

def test_approval_event_has_one_time_id_turn_text_and_expiry(fake_skills):
    s, events, _, _, req = start()
    assert req["turn"] == 1 and req["expires_in"] == APPROVAL_SECONDS == 60
    assert isinstance(req["id"], str) and len(req["id"]) >= 20          # secrets.token_urlsafe(16)
    assert "「fake-write」を実行します" in req["text"] and "書き込みがあります" in req["text"]
    assert "[y/N]" not in req["text"] and not req["text"].endswith(">")
    until(lambda: APPROVAL_SPOKEN in "".join(s.speaker.heard(1)))      # 「画面で確認してください」と読み上げる
    assert s.approve(1, req["id"], "decline") == "ok"
    events.wait("turn_done")


def test_run_within_deadline_executes_once(fake_skills):
    s, events, _, clock, req = start()
    clock.t += APPROVAL_SECONDS - 1                                     # 期限の 1 秒前
    assert s.approve(1, req["id"], "run") == "ok"
    events.wait("turn_done")
    assert fake_skills["write"].calls == 1
    assert done(events) == ["run"]
    assert events.of("answer_done")[0]["text"] == "書き込みました"
    assert s.approve(1, req["id"], "run") == "stale"                    # 使用済みの ID の再送
    assert fake_skills["write"].calls == 1
    # 次の発言は新しい要求（新しい ID）。前の発言の ID では承認できない
    s.submit(wav(), 48000)
    events.wait("approval", 2)
    req2 = events.of("approval")[1]
    assert req2["turn"] == 2 and req2["id"] != req["id"]
    assert s.approve(2, req["id"], "run") == "stale"
    assert s.approve(1, req2["id"], "run") == "stale"                   # 前の発言の番号
    assert s.approve(2, req2["id"], "decline") == "ok"
    events.wait("turn_done", 2)
    assert fake_skills["write"].calls == 1


def test_decline_does_not_execute_and_id_cannot_be_reused(fake_skills):
    s, events, _, _, req = start()
    assert s.approve(1, req["id"], "decline") == "ok"
    assert s.approve(1, req["id"], "run") == "stale"                    # 答えた後の「実行する」
    events.wait("turn_done")
    assert done(events) == ["declined"]
    assert events.of("answer_done")[0]["text"] == "実行しませんでした。"
    assert s.approve(1, req["id"], "run") == "stale"
    assert fake_skills["write"].calls == 0


def test_no_answer_expires_and_run_after_expiry_is_rejected(fake_skills):
    s, events, _, clock, req = start()
    clock.t += APPROVAL_SECONDS                                         # ちょうど期限（期限内ではない）
    assert s.approve(1, req["id"], "run") == "stale"
    events.wait("turn_done")
    assert done(events) == ["expired"]
    assert s.approve(1, req["id"], "run") == "stale"
    assert fake_skills["write"].calls == 0


@pytest.mark.parametrize("turn, change_id", [(2, False), (0, False), (1, True)])
def test_other_turn_or_other_id_is_rejected(fake_skills, turn, change_id):
    s, events, _, clock, req = start()
    other = req["id"][:-1] + ("A" if req["id"][-1] != "A" else "B") if change_id else req["id"]
    assert s.approve(turn, other, "run") == "stale"
    assert s.approve(1, "", "run") == "stale"
    assert s.approve(1, req["id"], "maybe") == "stale"                  # 形の違う答えは使わない（ID も使い切らない）
    clock.t += APPROVAL_SECONDS + 1
    events.wait("turn_done")
    assert done(events) == ["expired"]
    assert fake_skills["write"].calls == 0


def test_interrupt_cancels_pending_approval(fake_skills):
    s, events, _, _, req = start()
    assert s.interrupt(1) == "ok"
    assert done(events) == ["cancelled"]
    assert s.approve(1, req["id"], "run") == "stale"
    until(lambda: not s.speaker.synthesizing())
    # 取り消した後の結果のイベントは出さない（turn_done interrupted の後に何も来ない）
    cancelled = [e for e in events if e["type"] == "approval_done"][0]
    td = [e for e in events if e["type"] == "turn_done"][0]
    assert td.get("interrupted") and events.index(cancelled) < events.index(td)
    time.sleep(0.2)
    assert [e for e in events if e["type"] in ("answer_done", "turn_done")] == [td]
    assert fake_skills["write"].calls == 0


def test_spoken_yes_is_a_new_utterance_not_approval(fake_skills):
    s, events, fake, _, req = start()
    fake.text = "はい"                                                   # 声の「はい」
    t2 = s.submit(wav(), 48000)                                         # 考えている間の新しい発話 → 割り込み
    assert t2 > 1
    events.wait("turn_done", 2)
    assert [e["turn"] for e in events.of("turn_done")] == [1, t2]
    assert done(events) == ["cancelled"]
    recognized = [e["text"] for e in events.of("recognized")]
    assert recognized == [WRITE, "はい"]                                 # 新しい発言として扱う
    assert fake_skills["answer"].calls == 1                              # ふつうの質問として答えた
    assert s.approve(1, req["id"], "run") == "stale"
    assert fake_skills["write"].calls == 0


def test_end_cancels_pending_approval(fake_skills):
    s, events, _, _, req = start()
    s.end()
    events.wait("ended")
    assert done(events) == ["cancelled"]
    types = [e["type"] for e in events]
    assert types.index("approval_done") < types.index("ended")
    assert s.approve(1, req["id"], "run") == "stale"
    until(lambda: fake_skills["write"].calls == 0 and not s.speaker.synthesizing())
    assert fake_skills["write"].calls == 0


def test_ask_after_cut_or_end_does_not_request():
    s, events, _ = make_session()
    s.end()
    assert s._ask(1, "x [y/N] > ") == "n"
    assert not events.of("approval")


# ---------------------------------------------------------------- 端末の入力を待つスキル

def test_terminal_skill_is_not_run_in_browser(fake_skills):
    s, events, _ = make_session(Fake(text=THEME), handle=real_handle)
    s.submit(wav(), 48000)
    events.wait("turn_done")
    assert not events.of("approval")                                     # 確認を求めない
    answer = events.of("answer_done")[0]
    assert answer["text"] == orc.NEEDS_TERMINAL_TEXT and answer["ok"] is True
    assert "chat.py か voice_chat.py" in answer["text"]
    assert fake_skills["theme"].calls == 0


def test_terminal_skill_still_asks_in_chat_and_voice_chat(fake_skills):
    """chat.py・voice_chat.py の Context（既定の terminal=True）では、今までどおり確認を求める。"""
    for ctx in (orc.Context(), orc.Context(voice=True)):
        asked = []
        out = orc.handle(THEME, ctx, ask=lambda p: asked.append(p) or "n", use_llm=False)
        assert asked and out.error == "declined"
        out = orc.handle(THEME, ctx, ask=lambda p: "y", use_llm=False)
        assert out.ok and out.error is None
    assert fake_skills["theme"].calls == 2
    out = orc.handle(THEME, orc.Context(terminal=False), ask=lambda p: pytest.fail("確認しない"), use_llm=False)
    assert out.error == "needs_terminal" and out.ok and fake_skills["theme"].calls == 2


def test_non_interactive_write_skill_still_asks_without_terminal(fake_skills):
    asked = []
    out = orc.handle(WRITE, orc.Context(terminal=False), ask=lambda p: asked.append(p) or "y", use_llm=False)
    assert asked and out.ok and fake_skills["write"].calls == 1


# ---------------------------------------------------------------- HTTP

@pytest.fixture
def approval_server(fake_skills):
    clock = Clock()
    fake = Fake(text=WRITE)
    made = []

    def factory(emit):
        s = WebSession(transcribe=fake.transcribe, handle=real_handle, emit=emit, record=fake.record,
                       synth=lambda sentence, cancel=None: None, clock=clock)
        made.append(s)
        return s

    srv, _ = web.make_server(0, factory, token="test-token-123", ping_seconds=0.2)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        yield srv.server_address[1], made, clock, fake_skills
    finally:
        srv.shutdown()
        srv.server_close()


def _approve(port, sid, data, **kw):
    body = data if isinstance(data, (str, bytes)) else json.dumps(data)
    return call(port, "POST", f"/api/approve?session={sid}", body=body,
                headers={"Content-Type": "application/json"}, **kw)


def _start_http(port):
    sid = json.loads(call(port, "POST", "/api/session")[1])["session"]
    conn, resp = _open_events(port, sid)
    r, _ = call(port, "POST", f"/api/utterance?session={sid}&rate=48000", body=wav(),
                headers={"Content-Type": "audio/wav"})
    assert r.status == 202
    events, _ = _read_events(resp, "approval")
    req = events[-1]
    assert req["type"] == "approval"
    return sid, conn, resp, req


def test_approve_endpoint_checks_and_runs_once(approval_server):
    port, made, _, counters = approval_server
    sid, conn, resp, req = _start_http(port)
    ok = {"turn": req["turn"], "id": req["id"], "decision": "run"}
    # 確認（Origin・見出し・Cookie）は他の POST と同じ
    assert _approve(port, sid, ok, origin=False)[0].status == 403
    assert _approve(port, sid, ok, origin="http://evil.example")[0].status == 403
    assert _approve(port, sid, ok, csrf=False)[0].status == 403
    assert _approve(port, sid, ok, cookie=False)[0].status == 403
    assert _approve(port, sid, ok, host="evil.example")[0].status == 400
    # 形が違えば 400
    for bad in ("not json", "[1]", {"turn": "1", "id": req["id"], "decision": "run"},
                {"turn": True, "id": req["id"], "decision": "run"}, {"turn": 1, "id": 3, "decision": "run"},
                {"turn": 1, "id": req["id"], "decision": "yes"}, {"turn": 1, "id": "x" * 65, "decision": "run"},
                {"turn": 1, "decision": "run"}):
        assert _approve(port, sid, bad)[0].status == 400, bad
    # 違う ID・違う番号・違う会話は 409
    assert _approve(port, sid, dict(ok, id="wrong-id"))[0].status == 409
    assert _approve(port, sid, dict(ok, turn=2))[0].status == 409
    assert _approve(port, "other", ok)[0].status == 409
    assert counters["write"].calls == 0
    r, body = _approve(port, sid, ok)
    assert r.status == 200 and json.loads(body) == {"ok": True}
    assert _approve(port, sid, ok)[0].status == 409                     # 使用済み
    events, _ = _read_events(resp, "turn_done")
    assert [e["result"] for e in events if e["type"] == "approval_done"] == ["run"]
    assert counters["write"].calls == 1
    conn.close()


def test_approve_after_expiry_is_409(approval_server):
    port, made, clock, counters = approval_server
    sid, conn, resp, req = _start_http(port)
    clock.t += APPROVAL_SECONDS + 1
    assert _approve(port, sid, {"turn": req["turn"], "id": req["id"], "decision": "run"})[0].status == 409
    events, _ = _read_events(resp, "turn_done")
    assert [e["result"] for e in events if e["type"] == "approval_done"] == ["expired"]
    assert counters["write"].calls == 0
    conn.close()


def test_disconnect_cancels_and_approval_after_it_is_409(approval_server):
    port, made, _, counters = approval_server
    sid, conn, resp, req = _start_http(port)
    resp.close()                                                        # SSE が切れた
    conn.close()
    until(lambda: made[0].closed)
    assert made[0]._approval is None
    assert _approve(port, sid, {"turn": req["turn"], "id": req["id"], "decision": "run"})[0].status == 409
    assert made[0].approve(req["turn"], req["id"], "run") == "stale"
    assert counters["write"].calls == 0


def test_previous_session_id_cannot_approve_new_session(approval_server):
    port, made, _, counters = approval_server
    sid1, conn1, _, req1 = _start_http(port)
    assert call(port, "POST", f"/api/end?session={sid1}")[0].status == 200
    conn1.close()
    sid2, conn2, resp2, req2 = _start_http(port)
    assert req2["turn"] == req1["turn"] == 1 and req2["id"] != req1["id"]
    old = {"turn": 1, "id": req1["id"], "decision": "run"}
    assert _approve(port, sid1, old)[0].status == 409                   # 前の会話へ
    assert _approve(port, sid2, old)[0].status == 409                   # 今の会話に前の会話の ID
    assert counters["write"].calls == 0
    assert _approve(port, sid2, {"turn": 1, "id": req2["id"], "decision": "decline"})[0].status == 200
    _read_events(resp2, "turn_done")
    assert counters["write"].calls == 0
    conn2.close()
