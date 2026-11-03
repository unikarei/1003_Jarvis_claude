"""ブラウザでの声の会話の土台（T-1501、../docs/testing.md §13）のテスト。

偽の認識・偽の orchestrator.handle・偽の合成・偽のブラウザ・一時の DB で動かし、本物の LLM・Whisper・音声の機器・
音声エンジンを使わない。読み上げ（T-1502）のテストは tests/test_voice_web_speech.py。
"""

import http.client
import io
import json
import queue
import shutil
import subprocess
import sys
import threading
import time
import wave
from pathlib import Path

import numpy as np
import pytest

import config
import orchestrator
from ckc.models import Speaker as Who
from voice.web import server as web
from voice.web.session import AudioError, Busy, WebSession, decode_wav

ROOT = Path(__file__).resolve().parent.parent
TOKEN = "test-token-123"


@pytest.fixture(autouse=True)
def _latency_log(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "LATENCY_LOG", tmp_path / "latency" / "latency.jsonl")


def wav(seconds=0.5, rate=48000, channels=1, value=1000) -> bytes:
    n = int(seconds * rate)
    samples = np.full(n * channels, value, dtype="<i2")
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(channels)
        w.setsampwidth(2)
        w.setframerate(rate)
        w.writeframes(samples.tobytes())
    return buf.getvalue()


def outcome(text="答えです", skill="general-chat", ok=True, error=None):
    return orchestrator.Outcome(skill, "rule", {}, text, ok, 0.1, [], error)


class Fake:
    """偽の認識と handle。呼ばれた値を覚える。"""

    def __init__(self, text="こんにちは", answer="答えです", chunks=("答え", "です")):
        self.text, self.answer, self.chunks = text, answer, chunks
        self.heard, self.asked, self.handled, self.recorded = [], [], [], []
        self.gate = None          # threading.Event を入れると認識をそこで止める

    def transcribe(self, audio):
        self.heard.append(audio)
        if self.gate is not None:
            self.gate.wait(5)
        if isinstance(self.text, Exception):
            raise self.text
        return self.text

    def handle(self, text, ctx, ask=None):
        self.handled.append(text)
        ctx.shown = ""
        for c in self.chunks:
            ctx.stream(c)
            ctx.shown += c                # orchestrator と同じく、逐次表示した部分を残す
        return outcome(self.answer)

    def record(self, text, out):
        self.recorded.append((text, out.text))


class Synth:
    """偽の合成（文, 取り消し）→ 16 kHz の float32。読んだ文を覚える。fail の文は合成できない（None）。"""

    def __init__(self, seconds=0.1, fail=(), log=None, silent=False):
        self.seconds, self.fail, self.log, self.silent = seconds, set(fail), log, silent
        self.said = []

    def __call__(self, sentence, cancel=None):
        self.said.append(sentence)
        if self.log is not None:
            self.log.append(("synth", sentence))
        if self.silent or sentence in self.fail:
            return None
        return np.full(int(self.seconds * 16000), 0.1, dtype=np.float32)


class Browser:
    """偽のブラウザ。audio のイベントを受けたら、別のスレッドで WAV を取り、start・end を知らせる。
    play は最初の何文まで再生するか（None なら全部。それより後は何も知らせない＝止まったブラウザ）、
    errors は再生できない文の順番。知らせる前にイベントの記録へ _played として残し（turn_done との順番を見る）、
    知らせた結果を done に残す。"""

    def __init__(self, play=None, errors=()):
        self.play, self.errors = play, set(errors)   # play は数か、イベントを受けて再生するかを返す関数
        self.session, self.events = None, None
        self.fetched, self.done = [], []
        self._queue: queue.Queue = queue.Queue()
        threading.Thread(target=self._run, daemon=True).start()

    def on_event(self, event):
        if event["type"] == "audio":
            self._queue.put(event)

    def _run(self):
        count = 0
        while True:
            ev = self._queue.get()
            data = self.session.audio(ev["id"])
            self.fetched.append((ev["id"], data))
            if callable(self.play):
                if not self.play(ev):
                    continue
            elif self.play is not None and count >= self.play:
                continue
            count += 1
            steps = ["error"] if ev["index"] in self.errors else ["start", "end"]
            for step in steps:
                self.events({"type": "_played", "event": step, "index": ev["index"]})
                self.done.append((step, ev["index"], self.session.played(ev["turn"], ev["id"], step)))


class Events(list):
    def __init__(self, browser=None):
        super().__init__()
        self.cond = threading.Condition()
        self.browser = browser

    def __call__(self, event):
        with self.cond:
            self.append(event)
            self.cond.notify_all()
        if self.browser is not None:
            self.browser.on_event(event)

    def of(self, *types):
        with self.cond:
            return [e for e in self if e["type"] in types]

    def wait(self, type_, count=1, timeout=5.0):
        end = time.monotonic() + timeout
        with self.cond:
            while sum(e["type"] == type_ for e in self) < count:
                left = end - time.monotonic()
                if left <= 0:
                    raise AssertionError(f"{type_} が来ません: {[e['type'] for e in self]}")
                self.cond.wait(left)


def make_session(fake=None, browser=None, synth=None, **kw):
    """偽の認識・handle・合成・ブラウザ（既定はすべての文を再生する）でつくる会話。"""
    fake = fake or Fake()
    browser = browser or Browser()
    events = Events(browser)
    s = WebSession(transcribe=fake.transcribe, handle=kw.get("handle", fake.handle), emit=events,
                   record=kw.get("record", fake.record), synth=synth or Synth(), launcher=kw.get("launcher"),
                   clock=kw.get("clock", time.monotonic))
    browser.session, browser.events = s, events
    return s, events, fake


UI_EVENTS = ("ready", "recognized", "ignored", "answer_chunk", "answer_done", "error", "ended", "turn_done")


def ui(events):
    """読み上げ（audio・notice）と偽のブラウザの記録を除いた種類の並び。"""
    return [e["type"] for e in events.of(*UI_EVENTS)]


# ---------------------------------------------------------------- WAV

def test_decode_wav_checks_format_rate_and_length():
    assert decode_wav(wav(0.1, 16000), 16000).dtype == np.float32
    with pytest.raises(AudioError) as e:
        decode_wav(wav(0.1, 48000), 44100)                   # WAV の中のレートと違う
    assert e.value.status == 400
    with pytest.raises(AudioError) as e:
        decode_wav(wav(0.1, 16000, channels=2), 16000)       # ステレオ
    assert e.value.status == 400
    with pytest.raises(AudioError) as e:
        decode_wav(wav(21, 8000), 8000)                      # 20.5 秒を超える
    assert e.value.status == 413
    for rate in (7999, 96001):
        with pytest.raises(AudioError) as e:
            decode_wav(wav(0.1, 16000), rate)
        assert e.value.status == 400
    with pytest.raises(AudioError):
        decode_wav(b"not a wav", 16000)


# ---------------------------------------------------------------- WebSession

def test_48k_wav_is_resampled_to_16k_and_events_in_order():
    s, events, fake = make_session()
    assert s.submit(wav(0.5, 48000), 48000) == 1
    events.wait("turn_done")
    audio = fake.heard[0]
    assert audio.dtype == np.float32 and len(audio) == 8000      # 0.5 秒の 16 kHz
    assert abs(float(audio.mean()) - 1000 / 32768) < 1e-4
    assert ui(events) == ["ready", "recognized", "answer_chunk", "answer_chunk", "answer_done", "turn_done"]
    sent = [e for e in events if e["type"] != "_played"]
    assert [e["seq"] for e in sent] == list(range(1, len(sent) + 1))
    assert all(e["session"] == s.id for e in sent)
    assert all(e["turn"] == 1 for e in sent[1:])
    shown = events.of(*UI_EVENTS)
    assert shown[1]["text"] == "こんにちは"
    assert [e["text"] for e in shown[2:4]] == ["答え", "です"]
    assert (shown[4]["text"], shown[4]["skill"], shown[4]["ok"]) == ("答えです", "general-chat", True)
    assert fake.recorded == [("こんにちは", "答えです")]
    assert [(t.speaker, t.text) for t in s.ctx.history] == [(Who.OWNER, "こんにちは"), (Who.AI, "答えです")]
    assert s.ctx.voice is True and s.ctx.stream is None


@pytest.mark.parametrize("text,reason", [("ご視聴ありがとうございました", "noise"), ("", "empty"), ("。", "empty")])
def test_noise_is_ignored(text, reason):
    s, events, fake = make_session(Fake(text=text))
    s.submit(wav(), 48000)
    events.wait("turn_done")
    ignored = events.of("ignored")[-1]
    assert ignored["reason"] == reason and ignored["turn"] == 1
    assert ui(events)[-2:] == ["ignored", "turn_done"]
    assert fake.handled == []


def test_quit_word_ends_session():
    s, events, fake = make_session(Fake(text="終了。"))
    s.submit(wav(), 48000)
    events.wait("ended")
    assert [e["type"] for e in events] == ["ready", "recognized", "ended"]
    assert events[-1]["turn"] == 1               # 発言による終了には発言の番号を付ける
    assert s.closed and fake.handled == []
    with pytest.raises(Busy):
        s.submit(wav(), 48000)


def test_skill_needing_confirmation_is_not_executed_when_declined():
    """確認は画面での承認（T-1504、詳しくは tests/test_voice_web_approval.py）。「実行しない」なら実行しない。"""
    fake = Fake()
    answers = []

    def handle(text, ctx, ask=None):
        answer = ask("「theme-register」を実行します。実行しますか？ [y/N] > ")
        answers.append(answer)
        return outcome("実行しませんでした。" if answer != "y" else "実行しました", error="declined")

    s, events, _ = make_session(fake, handle=handle)
    s.submit(wav(), 48000)
    events.wait("approval")
    req = events.of("approval")[0]
    assert req["text"] == "「theme-register」を実行します。実行しますか？" and req["expires_in"] == 60
    assert s.approve(req["turn"], req["id"], "decline") == "ok"
    events.wait("answer_done")
    assert answers == ["n"]
    assert events.of("answer_done")[0]["text"] == "実行しませんでした。"


def test_stt_error_is_reported_and_session_continues():
    fake = Fake(text=RuntimeError("whisper failed"))
    s, events, _ = make_session(fake)
    s.submit(wav(), 48000)
    events.wait("turn_done")
    error = events.of("error")[-1]
    assert error["kind"] == "stt" and error["turn"] == 1
    fake.text = "もう一度"
    assert s.submit(wav(), 48000) == 2
    events.wait("turn_done", 2)
    assert events.of("answer_done")[-1]["turn"] == 2 and not s.closed


def test_answer_error_and_record_error_do_not_stop_session():
    def broken(text, ctx, ask=None):
        raise RuntimeError("boom")

    s, events, fake = make_session(handle=broken)
    s.submit(wav(), 48000)
    events.wait("turn_done")
    assert events.of("error")[-1]["kind"] == "answer"

    def bad_record(text, out):
        raise RuntimeError("db is locked")

    s2, events2, _ = make_session(record=bad_record)
    s2.submit(wav(), 48000)
    events2.wait("turn_done")
    assert len(s2.ctx.history) == 2


def test_second_utterance_while_thinking_interrupts():
    """T-1503: 認識している間に次の発話が届いたら、前の発言に割り込んでから受け付ける（409 にしない）。"""
    fake = Fake()
    fake.gate = threading.Event()
    s, events, _ = make_session(fake)
    assert s.submit(wav(), 48000) == 1
    assert s.submit(wav(), 48000) == 3                   # 割り込みで 2、新しい発言で 3（割り込まれた発言より大きい）
    fake.gate.set()
    events.wait("turn_done", 2)
    done = events.of("turn_done")
    assert done[0] == {**done[0], "turn": 1, "interrupted": True}
    assert done[1]["turn"] == 3 and "interrupted" not in done[1]
    assert [e["turn"] for e in events.of("recognized", "answer_done")] == [3, 3]   # 前の発言には答えない
    assert fake.handled == ["こんにちは"]
    s.end()
    with pytest.raises(Busy):                            # 終えた会話は受け付けない
        s.submit(wav(), 48000)


def test_bad_audio_emits_error_event():
    s, events, _ = make_session()
    with pytest.raises(AudioError):
        s.submit(wav(0.1, 16000, channels=2), 16000)
    assert events[-1]["type"] == "error" and events[-1]["kind"] == "bad_audio"
    assert s.turn == 0


# ---------------------------------------------------------------- サーバー

@pytest.fixture
def served():
    fake = Fake()
    fake.synth = Synth(silent=True)      # 既定は音を作らない（SSE の並びを T-1501 と同じに保つ）。読み上げのテストで差し替える
    made = []

    def factory(emit):
        s = WebSession(transcribe=fake.transcribe, handle=fake.handle, emit=emit, record=fake.record,
                       synth=fake.synth)
        made.append(s)
        return s

    srv, token = web.make_server(0, factory, token=TOKEN, ping_seconds=0.2)
    thread = threading.Thread(target=srv.serve_forever, daemon=True)
    thread.start()
    try:
        yield srv.server_address[1], fake, made
    finally:
        srv.shutdown()
        srv.server_close()


def call(port, method, path, body=None, headers=None, cookie=True, host=None, origin=True, csrf=True):
    h = {"Host": host or f"localhost:{port}"}
    if cookie:
        h["Cookie"] = f"jv={TOKEN}"
    if method == "POST":
        if origin:
            h["Origin"] = origin if isinstance(origin, str) else f"http://localhost:{port}"
        if csrf:
            h["X-Jarvis-Voice"] = "1"
    h.update(headers or {})
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
    conn.request(method, path, body=body, headers=h)
    r = conn.getresponse()
    data = r.read()
    conn.close()
    return r, data


def test_token_sets_cookie_and_redirects(served):
    port, _, _ = served
    r, _ = call(port, "GET", f"/?token={TOKEN}", cookie=False)
    assert r.status == 303 and r.getheader("Location") == "/"
    assert r.getheader("Set-Cookie") == f"jv={TOKEN}; HttpOnly; SameSite=Strict; Path=/"
    assert call(port, "GET", "/?token=wrong", cookie=False)[0].status == 403
    assert call(port, "GET", "/", cookie=False)[0].status == 403
    assert call(port, "GET", "/api/config", headers={"Cookie": "jv=wrong"}, cookie=False)[0].status == 403
    r, body = call(port, "GET", "/")
    assert r.status == 200 and b"vad.js" in body and "default-src 'self'" in r.getheader("Content-Security-Policy")


def test_host_is_checked_but_port_is_free(served):
    port, _, _ = served
    assert call(port, "GET", "/", host="127.0.0.1:65000")[0].status == 200     # VS Code が別の番号に転送する
    assert call(port, "GET", "/", host="localhost")[0].status == 200
    for host in ("evil.example", "localhost.evil.example", "127.0.0.2:8771"):
        assert call(port, "GET", "/", host=host)[0].status == 400
    assert call(port, "GET", f"/?token={TOKEN}", host="evil.example", cookie=False)[0].status == 400


def test_post_requires_origin_and_header(served):
    port, _, _ = served
    assert call(port, "POST", "/api/session", origin=False)[0].status == 403
    assert call(port, "POST", "/api/session", origin="http://evil.example")[0].status == 403
    assert call(port, "POST", "/api/session", origin="https://localhost:8771")[0].status == 403
    assert call(port, "POST", "/api/session", csrf=False)[0].status == 403
    assert call(port, "POST", "/api/session", cookie=False)[0].status == 403
    r, body = call(port, "POST", "/api/session", origin="http://127.0.0.1:12345")
    assert r.status == 200 and json.loads(body)["session"]


def test_static_files_only_inside_static(served):
    port, _, _ = served
    for name in ("app.js", "vad.js", "page.js", "vad-worklet.js", "index.html"):
        r, _ = call(port, "GET", f"/static/{name}")
        assert r.status == 200
    assert call(port, "GET", "/static/app.js")[0].getheader("Content-Type").startswith("text/javascript")
    for path in ("/static/../server.py", "/static/..%2fserver.py", "/static/%2e%2e/server.py",
                 "/static/nope.js", "/static/", "/static/sub/app.js", "/static/.hidden.js", "/server.py"):
        assert call(port, "GET", path)[0].status == 404, path


def test_config(served, monkeypatch):
    port, _, _ = served
    monkeypatch.delenv("JARVIS_VOICE_SILENCE_SECONDS", raising=False)
    monkeypatch.delenv("JARVIS_VOICE_BARGE_MARGIN", raising=False)
    assert json.loads(call(port, "GET", "/api/config")[1]) == {
        "silence_seconds": 0.8, "max_seconds": 20, "preroll_seconds": 0.3, "threshold_factor": 4.0,
        "min_threshold": 0.002, "barge_margin": 5.0}
    monkeypatch.setenv("JARVIS_VOICE_SILENCE_SECONDS", "1.2")
    assert json.loads(call(port, "GET", "/api/config")[1])["silence_seconds"] == 1.2
    monkeypatch.setenv("JARVIS_VOICE_BARGE_MARGIN", "3.5")
    assert json.loads(call(port, "GET", "/api/config")[1])["barge_margin"] == 3.5
    for bad in ("0", "-1", "abc", "inf", "nan"):
        monkeypatch.setenv("JARVIS_VOICE_BARGE_MARGIN", bad)
        assert json.loads(call(port, "GET", "/api/config")[1])["barge_margin"] == 5.0, bad


def test_second_session_is_busy(served):
    port, _, made = served
    r, body = call(port, "POST", "/api/session")
    sid = json.loads(body)["session"]
    r, body = call(port, "POST", "/api/session")
    assert r.status == 409 and json.loads(body) == {"error": "busy"}
    assert not made[0].closed                                 # 既存の会話を奪わない
    assert call(port, "POST", f"/api/end?session={sid}")[0].status == 200
    assert call(port, "POST", "/api/session")[0].status == 200


def _utter(port, sid, body, rate=48000, ctype="audio/wav"):
    return call(port, "POST", f"/api/utterance?session={sid}&rate={rate}", body=body,
                headers={"Content-Type": ctype})


def test_utterance_limits(served):
    port, _, _ = served
    sid = json.loads(call(port, "POST", "/api/session")[1])["session"]
    assert _utter(port, sid, wav(0.1, 48000), rate=44100)[0].status == 400            # レートの不一致
    assert _utter(port, sid, wav(0.1, 48000), rate="abc")[0].status == 400
    assert _utter(port, sid, wav(0.1, 7000), rate=7000)[0].status == 400              # 範囲の外
    assert _utter(port, sid, wav(0.1, 48000), ctype="application/octet-stream")[0].status == 400
    assert _utter(port, sid, wav(21, 8000), rate=8000)[0].status == 413               # 長すぎる
    assert _utter(port, "other", wav())[0].status == 404

    # 4 MB を超える本文は読まずに 413
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
    conn.putrequest("POST", f"/api/utterance?session={sid}&rate=48000", skip_host=True)
    for k, v in {"Host": f"localhost:{port}", "Cookie": f"jv={TOKEN}", "Origin": f"http://localhost:{port}",
                 "X-Jarvis-Voice": "1", "Content-Type": "audio/wav",
                 "Content-Length": str(4 * 1024 * 1024 + 1)}.items():
        conn.putheader(k, v)
    conn.endheaders()
    assert conn.getresponse().status == 413
    conn.close()


def test_utterance_while_thinking_is_interrupt_not_409(served):
    port, fake, _ = served
    fake.gate = threading.Event()
    sid = json.loads(call(port, "POST", "/api/session")[1])["session"]
    r, body = _utter(port, sid, wav())
    assert r.status == 202 and json.loads(body) == {"turn": 1}
    r, body = _utter(port, sid, wav())
    assert r.status == 202 and json.loads(body) == {"turn": 3}
    fake.gate.set()


def _read_events(resp, until, timeout=5.0):
    """SSE を until の種類まで読む。ping（event: ping）は数だけ返す。"""
    out, pings, named = [], 0, None
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        line = resp.readline()
        if not line:
            break
        line = line.decode("utf-8").rstrip("\n")
        if line.startswith("event: "):
            named = line[7:]
            if named == "ping":
                pings += 1
        elif line == "":
            named = None
        elif line.startswith("data: ") and named is None:
            out.append(json.loads(line[6:]))
            if out[-1]["type"] == until:
                break
    return out, pings


def no_tts(events):
    """合成の失敗（偽の合成が音を作らない served では、文ごとに error tts が出る）を除く。"""
    return [e for e in events if not (e["type"] == "error" and e.get("kind") == "tts")]


def _open_events(port, sid):
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
    conn.request("GET", f"/api/events?session={sid}", headers={"Host": f"localhost:{port}", "Cookie": f"jv={TOKEN}"})
    return conn, conn.getresponse()


def test_sse_events_and_end(served):
    port, fake, made = served
    sid = json.loads(call(port, "POST", "/api/session")[1])["session"]
    assert call(port, "GET", "/api/events?session=wrong")[0].status == 404
    conn, resp = _open_events(port, sid)
    assert resp.status == 200 and resp.getheader("Content-Type").startswith("text/event-stream")
    conn2, resp2 = _open_events(port, sid)                    # 2 本目の SSE はつながない
    assert resp2.status == 404
    conn2.close()

    assert _utter(port, sid, wav())[0].status == 202
    first, _ = _read_events(resp, "answer_done")
    assert [e["type"] for e in no_tts(first)] == ["ready", "recognized", "answer_chunk", "answer_chunk", "answer_done"]
    assert {e["session"] for e in first} == {sid}

    time.sleep(0.5)
    assert call(port, "POST", f"/api/end?session={sid}")[0].status == 200
    events, pings = _read_events(resp, "ended")
    assert pings >= 1                                         # 待っている間は ping（event: ping）を送る
    assert [e["type"] for e in no_tts(events)] == ["turn_done", "ended"]
    seqs = [e["seq"] for e in first + events]
    assert seqs == list(range(1, len(seqs) + 1))              # 連番は 1 から抜けなく
    tts_errors = [e for e in first + events if e["type"] == "error"]
    assert len(tts_errors) <= 1 and all(e["kind"] == "tts" and e["turn"] == 1 for e in tts_errors)   # 発言ごとに 1 回
    assert resp.readline() == b"\n" and resp.readline() == b""   # 終えたら閉じる
    conn.close()
    assert made[0].closed
    assert _utter(port, sid, wav())[0].status == 404


def test_sse_disconnect_ends_session(served):
    port, _, made = served
    sid = json.loads(call(port, "POST", "/api/session")[1])["session"]
    conn, resp = _open_events(port, sid)
    _read_events(resp, "ready")
    resp.close()
    conn.close()
    deadline = time.monotonic() + 5
    while not made[0].closed and time.monotonic() < deadline:
        time.sleep(0.05)
    assert made[0].closed
    assert call(port, "POST", "/api/session")[0].status == 200


def test_session_without_sse_is_replaced_after_grace(served, monkeypatch):
    port, _, made = served
    call(port, "POST", "/api/session")
    assert call(port, "POST", "/api/session")[0].status == 409
    monkeypatch.setattr(web, "ATTACH_SECONDS", 0)
    assert call(port, "POST", "/api/session")[0].status == 200
    assert made[0].closed and not made[1].closed


def test_log_message_is_silent(capsys, served):
    port, _, _ = served
    call(port, "GET", f"/?token={TOKEN}", cookie=False)
    call(port, "GET", "/?token=wrong", cookie=False)
    captured = capsys.readouterr()
    assert TOKEN not in captured.err and TOKEN not in captured.out


# ---------------------------------------------------------------- 起動の経路・ページの判定

def test_startup_path_does_not_import_sounddevice():
    code = """
import sys, http.server
import speech_to_text, orchestrator
speech_to_text.load_model = lambda: None
orchestrator.start_warm_up = lambda: None
def stop(self, *a, **k):
    raise KeyboardInterrupt
http.server.ThreadingHTTPServer.serve_forever = stop
from voice.web.__main__ import main
assert main(["--port", "0"]) == 0
assert "sounddevice" not in sys.modules, "sounddevice was imported"
print("OK")
"""
    r = subprocess.run([sys.executable, "-c", code], cwd=ROOT, capture_output=True, text=True, timeout=120)
    assert r.returncode == 0, r.stderr
    assert "http://localhost:" in r.stdout and "/?token=" in r.stdout and r.stdout.rstrip().endswith("OK")


@pytest.mark.skipif(shutil.which("node") is None, reason="Node がない")
@pytest.mark.parametrize("name", ["vad.test.js", "page.test.js"])
def test_page_js_with_node(name):
    r = subprocess.run(["node", "--test", str(ROOT / "tests" / "js" / name)], cwd=ROOT,
                       capture_output=True, text=True, timeout=60)
    assert r.returncode == 0, r.stdout + r.stderr


@pytest.mark.skipif(shutil.which("node") is None, reason="Node がない")
@pytest.mark.parametrize("name", ["app.js", "vad-worklet.js"])
def test_page_js_syntax(name):
    """DOM に触れるため Node で動かせないページの JS も、構文だけは確かめる。"""
    r = subprocess.run(["node", "--check", str(ROOT / "voice" / "web" / "static" / name)], cwd=ROOT,
                       capture_output=True, text=True, timeout=60)
    assert r.returncode == 0, r.stdout + r.stderr
