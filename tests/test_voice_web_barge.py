"""ブラウザでの声の会話の割り込み・回り込みの測定・切断（T-1503、../docs/testing.md §13）のテスト。

偽の合成・偽のブラウザ・偽の handle・偽の認識・一時の遅延の記録で動かし、本物の LLM・Whisper・音声の機器・
音声エンジンを使わない。
"""

import json
import threading
import time

import pytest

import config
import latency
import llm
import voice_chat
from ckc.models import Speaker as Who
from tests.test_voice_web import (Browser, Fake, Synth, _open_events, _read_events, _utter, call, make_session,
                                  outcome, served, wav)  # noqa: F401  served は fixture
from tests.test_voice_web_speech import Clock, until
from voice import tts
from voice.web import server as web
from voice.web.session import CALIBRATION_TEXT, Busy

MARK = voice_chat.INTERRUPTED_MARK


@pytest.fixture(autouse=True)
def _isolated(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "LATENCY_LOG", tmp_path / "latency" / "latency.jsonl")
    monkeypatch.setattr(tts, "SETTINGS_PATH", tmp_path / "voice" / "settings.json")   # 本番の設定を読まない


class Gen:
    """偽の逐次生成の handle。文の前に ctx.cancel() を確かめ（llm の取り消しと同じ）、真なら LLMCancelled で止まる。
    gate_after の文の前で gate を待つ（呼び出しごとに gates から 1 つ使う）。"""

    def __init__(self, sentences, gate_after=None, skill="general-chat"):
        self.sentences, self.gate_after, self.skill = sentences, gate_after, skill
        self.gate = threading.Event()
        self.cancelled, self.calls = [], 0
        self.done = threading.Event()

    def __call__(self, text, ctx, ask=None):
        self.calls += 1
        try:
            ctx.shown = ""
            for i, s in enumerate(self.sentences):
                if self.gate_after is not None and i == self.gate_after:
                    assert self.gate.wait(5)
                if ctx.cancel is not None and ctx.cancel():
                    self.cancelled.append(i)
                    raise llm.LLMCancelled("取り消されました")
                ctx.stream(s)
                ctx.shown += s
            return outcome("".join(self.sentences), skill=self.skill)
        finally:
            self.done.set()


def after(events, ev):
    """イベントの記録で ev より後のもの（偽のブラウザの記録を除く）。"""
    sent = [e for e in events if e["type"] != "_played"]
    return sent[sent.index(ev) + 1:]


def interrupted_done(events, turn):
    return [e for e in events.of("turn_done") if e["turn"] == turn and e.get("interrupted")]


def _records():
    return latency.load(config.LATENCY_LOG)


# ---------------------------------------------------------------- 回り込みの測定

def test_calibration_greeting_is_sent_once_with_flag_and_not_in_history():
    synth = Synth()
    s, events, _ = make_session(synth=synth)
    assert s.calibrate() is True
    audio = events.of("audio")
    assert len(audio) == 1
    assert audio[0]["calibration"] is True and audio[0]["turn"] == 0 and audio[0]["text"] == CALIBRATION_TEXT
    assert synth.said == [CALIBRATION_TEXT]
    until(lambda: s.player.held() == (0, 0))            # 偽のブラウザの start・end を受けて消える（409 にならない）
    with pytest.raises(Busy):                           # 1 回だけ
        s.calibrate()
    assert s.ctx.history == []
    s.submit(wav(), 48000)
    events.wait("turn_done")
    assert [(t.speaker, t.text) for t in s.ctx.history] == [(Who.OWNER, "こんにちは"), (Who.AI, "答えです")]
    assert all(not e.get("calibration") for e in events.of("audio")[1:])
    assert _records() and all(r["mode"] == "web" for r in _records()) and len(_records()) == 1   # あいさつは記録しない


def test_calibration_without_audio_returns_false_and_reports_tts():
    s, events, _ = make_session(synth=Synth(silent=True))
    assert s.calibrate() is False
    assert not events.of("audio")
    errors = events.of("error")
    assert len(errors) == 1 and errors[0]["kind"] == "tts" and errors[0]["turn"] == 0
    s.submit(wav(), 48000)                              # 測定の後でも会話は続く
    events.wait("turn_done")
    with pytest.raises(Busy):                           # 発言の後は測らない
        s.calibrate()


def test_calibrate_endpoint(served):
    port, fake, made = served
    fake.synth = Synth()
    sid = json.loads(call(port, "POST", "/api/session")[1])["session"]
    conn, resp = _open_events(port, sid)
    r, body = call(port, "POST", f"/api/calibrate?session={sid}")
    assert r.status == 200 and json.loads(body) == {"audio": True}
    events, _ = _read_events(resp, "audio")
    assert events[-1]["calibration"] is True and events[-1]["turn"] == 0
    r, body = call(port, "POST", f"/api/calibrate?session={sid}")
    assert r.status == 409 and json.loads(body) == {"error": "calibrated"}
    assert call(port, "POST", "/api/calibrate?session=other")[0].status == 404
    assert call(port, "POST", f"/api/calibrate?session={sid}", origin=False)[0].status == 403
    # あいさつの再生の知らせも受ける
    hdr = {"Content-Type": "application/json"}
    for step in ("start", "end"):
        body = json.dumps({"turn": 0, "id": events[-1]["id"], "event": step})
        assert call(port, "POST", f"/api/played?session={sid}", body=body, headers=hdr)[0].status == 200
    assert made[0].player.held() == (0, 0)
    conn.close()


# ---------------------------------------------------------------- 割り込み

def test_interrupt_while_speaking_discards_audio_cancels_generation_and_keeps_heard():
    browser = Browser(play=1)                           # 1 文目だけ再生し終え、2 文目は再生中のまま
    handle = Gen(["一文目です。", "二文目です。", "三文目です。"], gate_after=2)
    synth = Synth()
    s, events, fake = make_session(handle=handle, browser=browser, synth=synth)
    assert s.submit(wav(), 48000) == 1
    until(lambda: ("end", 0, "ok") in browser.done and len(events.of("audio")) == 2)
    second = events.of("audio")[1]["id"]
    assert s.player.held()[0] == 1

    assert s.interrupt(1) == "ok"
    assert s.player.held() == (0, 0) and s.audio(second) is None   # 保持した音を消す
    assert s.speaker.turn == 2                           # 番号を進めた（古い合成を捨てる）
    done = interrupted_done(events, 1)
    assert len(done) == 1
    handle.gate.set()                                    # 生成は ctx.cancel を確かめて止まる
    assert handle.done.wait(5)
    time.sleep(0.2)
    assert handle.cancelled == [2]
    assert synth.said == ["一文目です。", "二文目です。"]
    assert [e for e in after(events, done[0]) if e.get("turn") == 1] == []   # 古い発言のイベントはその後に出ない
    assert len(events.of("turn_done")) == 1
    assert [(t.speaker, t.text) for t in s.ctx.history] == [(Who.OWNER, "こんにちは"), (Who.AI, "一文目です。" + MARK)]
    assert s.interrupt(1) == "stale"                     # 同じ番号の再送: もう処理していないので 409（何もしない）
    assert len(interrupted_done(events, 1)) == 1
    until(lambda: _records())
    assert _records()[-1]["ok"] is False and _records()[-1]["mode"] == "web"   # 割り込まれた発言

    # 次の発話は受け付け、番号は割り込まれた発言より大きい
    browser.play = None
    handle.gate_after = None
    assert s.submit(wav(), 48000) == 3
    events.wait("turn_done", 2)
    assert events.of("turn_done")[-1] == {**events.of("turn_done")[-1], "turn": 3}
    assert s.ctx.history[-1].text == "一文目です。二文目です。三文目です。"


def test_interrupt_while_thinking_keeps_owner_line_only_and_separates_context():
    handle = Gen(["一文目です。"], gate_after=0)
    s, events, _ = make_session(handle=handle)
    s.submit(wav(), 48000)
    events.wait("recognized")
    until(lambda: handle.calls == 1)
    old_ctx = s.ctx
    assert s.interrupt(1) == "ok"
    assert s.ctx is not old_ctx                          # 答えを作っている途中なら、文脈を分ける
    assert [(t.speaker, t.text) for t in s.ctx.history] == [(Who.OWNER, "こんにちは")]   # 聞いた文がなければ AI の行なし
    handle.gate.set()
    assert handle.done.wait(5)
    time.sleep(0.2)
    assert handle.cancelled == [0] and not events.of("answer_chunk", "audio", "answer_done")
    assert [e["type"] for e in events.of("turn_done")] == ["turn_done"]


def test_utterance_without_interrupt_is_barge_in():
    browser = Browser(play=1)
    handle = Gen(["一文目です。", "二文目です。"])
    s, events, _ = make_session(handle=handle, browser=browser)
    s.submit(wav(), 48000)
    until(lambda: ("end", 0, "ok") in browser.done and len(events.of("audio")) == 2)
    browser.play = None
    assert s.submit(wav(), 48000) == 3                   # /api/interrupt が先に届かなくても割り込み（409 にしない）
    done = interrupted_done(events, 1)
    assert len(done) == 1
    events.wait("turn_done", 2)
    assert [e for e in after(events, done[0]) if e.get("turn") == 1] == []
    hist = [(t.speaker, t.text) for t in s.ctx.history]
    assert hist == [(Who.OWNER, "こんにちは"), (Who.AI, "一文目です。" + MARK),
                    (Who.OWNER, "こんにちは"), (Who.AI, "一文目です。二文目です。")]


def test_stale_interrupt_is_rejected_and_does_nothing():
    browser = Browser(play=0)
    s, events, _ = make_session(handle=Gen(["一文目です。"]), browser=browser, clock=Clock())
    s.submit(wav(), 48000)
    until(lambda: events.of("audio"))
    assert s.interrupt(0) == "stale" and s.interrupt(2) == "stale"
    assert s.player.held()[0] == 1 and s.speaker.turn == 1 and not interrupted_done(events, 1)
    s.end()
    assert s.interrupt(1) == "stale"                     # 終えた会話


def test_interrupt_after_turn_finished_is_stale():
    s, events, _ = make_session(handle=Gen(["一文目です。"]))
    s.submit(wav(), 48000)
    until(lambda: events.of("turn_done"))
    assert s.interrupt(1) == "stale"                     # 処理を終えた発言（今の番号でも）には割り込まない
    assert not interrupted_done(events, 1)


def test_uncancellable_skill_result_is_not_used_and_context_is_separated():
    gate, recorded = threading.Event(), []
    calls = {"n": 0}

    def handle(text, ctx, ask=None):
        calls["n"] += 1
        if calls["n"] == 1:                              # 逐次表示のない遅いスキル（取り消しを見ない）
            gate.wait(5)
            ctx.items = [{"knowledge_id": "old"}]
            return outcome("遅い結果です", skill="knowledge-search")
        ctx.stream("新しい答えです。")
        ctx.shown = "新しい答えです。"
        return outcome("新しい答えです。")

    s, events, _ = make_session(handle=handle, record=lambda t, o: recorded.append(o.text))
    s.submit(wav(), 48000)
    until(lambda: calls["n"] == 1)
    old_ctx = s.ctx
    assert s.submit(wav(), 48000) == 3
    events.wait("turn_done", 2)
    assert s.ctx is not old_ctx and s.ctx.items == []
    gate.set()                                           # 前の処理が後から終わっても結果は使わない
    until(lambda: "遅い結果です" in recorded)             # 結果が出たら記録はする
    time.sleep(0.2)
    assert [e["text"] for e in events.of("answer_done")] == ["新しい答えです。"]
    assert all("遅い結果" not in t.text for t in s.ctx.history)
    assert s.ctx.items == [] and old_ctx.items == [{"knowledge_id": "old"}]
    assert [(t.speaker, t.text) for t in s.ctx.history] == [(Who.OWNER, "こんにちは"), (Who.OWNER, "こんにちは"),
                                                          (Who.AI, "新しい答えです。")]


def test_three_interrupts_in_a_row_answer_only_the_last():
    last = 7
    browser = Browser(play=lambda ev: ev["turn"] >= last)    # 最後の発言だけ再生する（それまでは再生中のまま）
    handle = Gen(["一文目です。", "二文目です。"])
    s, events, _ = make_session(handle=handle, browser=browser)
    turns, ids = [], []
    for i in range(4):
        turns.append(s.submit(wav(), 48000))
        if i < 3:
            until(lambda: len([e for e in events.of("audio") if e["turn"] == turns[-1]]) == 2)
            ids += [e["id"] for e in events.of("audio") if e["turn"] == turns[-1]]
    assert turns == [1, 3, 5, last]
    until(lambda: any(e["turn"] == last and not e.get("interrupted") for e in events.of("turn_done")))
    time.sleep(0.2)
    for t in turns[:3]:
        done = interrupted_done(events, t)
        assert len(done) == 1
        assert [e for e in after(events, done[0]) if e.get("turn") == t] == []
    assert all(s.audio(i) is None for i in ids)            # 古い音はもう取れない
    answers = [e for e in events.of("answer_done") if e["turn"] == last]
    assert len(answers) == 1                               # 二重の回答がない
    assert [e["turn"] for e in events.of("turn_done")] == [1, 3, 5, last]
    assert [(t.speaker, t.text) for t in s.ctx.history] == [(Who.OWNER, "こんにちは")] * 4 + [
        (Who.AI, "一文目です。二文目です。")]


def test_end_cancels_generation_and_advances_turn():
    handle = Gen(["一文目です。", "二文目です。"], gate_after=1)
    s, events, _ = make_session(handle=handle, browser=Browser(play=0))
    s.submit(wav(), 48000)
    until(lambda: events.of("audio"))
    s.end()
    assert s.speaker.turn == 2 and s.player.held() == (0, 0)
    handle.gate.set()
    assert handle.done.wait(5)
    assert handle.cancelled == [1]
    events.wait("ended")
    assert not events.of("turn_done")


# ---------------------------------------------------------------- サーバー

def test_interrupt_endpoint(served):
    port, fake, made = served
    fake.gate = threading.Event()                          # 認識の途中（考えている間）
    sid = json.loads(call(port, "POST", "/api/session")[1])["session"]
    assert _utter(port, sid, wav())[0].status == 202
    hdr = {"Content-Type": "application/json"}
    url = f"/api/interrupt?session={sid}"
    assert call(port, "POST", url, body=json.dumps({"turn": 1}), headers=hdr, origin=False)[0].status == 403
    assert call(port, "POST", url, body=json.dumps({"turn": 1}), headers=hdr, csrf=False)[0].status == 403
    assert call(port, "POST", url, body=b"{bad", headers=hdr)[0].status == 400
    assert call(port, "POST", url, body=json.dumps({"turn": "1"}), headers=hdr)[0].status == 400
    assert call(port, "POST", url, body=json.dumps({"turn": True}), headers=hdr)[0].status == 400
    assert call(port, "POST", url, body=json.dumps([1]), headers=hdr)[0].status == 400
    assert call(port, "POST", "/api/interrupt?session=other", body=json.dumps({"turn": 1}),
                headers=hdr)[0].status == 404
    r, body = call(port, "POST", url, body=json.dumps({"turn": 0}), headers=hdr)
    assert r.status == 409 and json.loads(body) == {"error": "stale_turn"}
    assert made[0]._active is not None                     # 409 では何もしない
    r, body = call(port, "POST", url, body=json.dumps({"turn": 1}), headers=hdr)
    assert r.status == 200 and json.loads(body) == {"ok": True}
    assert made[0]._active is None and made[0].speaker.turn == 2
    r, body = _utter(port, sid, wav())                     # 次の発話は 409 にしない
    assert r.status == 202 and json.loads(body) == {"turn": 3}
    fake.gate.set()


def test_sse_ping_every_5_seconds_by_default_and_named(served):
    assert web.PING_SECONDS == 5.0
    port, _, _ = served
    sid = json.loads(call(port, "POST", "/api/session")[1])["session"]
    conn, resp = _open_events(port, sid)
    _read_events(resp, "ready")
    lines = [resp.readline() for _ in range(4)]                # ready の区切りの空行の後に ping
    assert lines == [b"\n", b"event: ping\n", b"data: {}\n", b"\n"]   # 名前付き（ページの onmessage・seq に入らない）
    conn.close()


def test_page_js_has_barge_in_and_disconnect_handling():
    from pathlib import Path
    static = Path(__file__).resolve().parent.parent / "voice" / "web" / "static"
    js = (static / "app.js").read_text(encoding="utf-8")
    for needle in ("/api/interrupt", "/api/calibrate", "barge_margin", "setPlaying(playingNow())",
                   "seqFilter.accept(ev)", 'addEventListener("ping"', "onended", "AbortController",
                   "P.SILENCE_LIMIT_MS", "playThresholdFrom"):
        assert needle in js, needle
    assert "muted" not in js                               # 再生中も聞き続ける（T-1502 の「止める」をやめた）
    html = (static / "index.html").read_text(encoding="utf-8")
    assert html.index("/static/vad.js") < html.index("/static/page.js") < html.index("/static/app.js")
    for banned in ("localStorage", "sessionStorage", "indexedDB", "caches."):
        assert banned not in js, banned
