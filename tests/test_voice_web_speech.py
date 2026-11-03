"""ブラウザでの声の会話の読み上げ（T-1502、../docs/testing.md §13）のテスト。

偽の合成・偽のブラウザ・偽の handle・偽の認識・偽のエンジンの起動（launcher）・一時の DB と遅延の記録で動かし、
本物の LLM・Whisper・音声の機器・音声エンジンを使わない。
"""

import io
import json
import threading
import time
import wave

import numpy as np
import pytest

import config
import latency
from ckc.models import Speaker as Who
from tests.test_voice_web import (TOKEN, Browser, Fake, Synth, _open_events, _read_events, _utter, call, make_session,
                                  outcome, served, ui, wav)  # noqa: F401  served は fixture
from voice import tts
from voice.speak import speakable, speech_summary
from voice.web import player as wp
from voice.web.player import BrowserPlayer


@pytest.fixture(autouse=True)
def _isolated(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "LATENCY_LOG", tmp_path / "latency" / "latency.jsonl")
    monkeypatch.setattr(tts, "SETTINGS_PATH", tmp_path / "voice" / "settings.json")   # 本番の設定を読まない


class Clock:
    def __init__(self, t=1000.0):
        self.t = t

    def __call__(self):
        return self.t


def tone(seconds=1.0, value=0.25):
    return np.full(int(seconds * 16000), value, dtype=np.float32)


class Recorder(list):
    def __call__(self, type_, turn, **data):
        self.append({"type": type_, "turn": turn, **data})


def until(cond, timeout=5.0, step=None):
    """cond() が真になるまで待つ。step を渡すと待つ間に呼ぶ（偽の時計を進めるなど）。"""
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if cond():
            return
        if step is not None:
            step()
        time.sleep(0.02)
    raise AssertionError("待っている状態になりません")


# ---------------------------------------------------------------- BrowserPlayer

def test_player_accepts_only_current_turn_and_sends_audio_events_in_order():
    sent = Recorder()
    p = BrowserPlayer(emit=sent)
    p.set_turn(2)
    p.enqueue(1, "古い文です。", tone())                 # 古い番号の音は受け付けない
    assert sent == [] and p.held() == (0, 0)
    p.enqueue(2, "一文目です。", tone(1.0))
    p.enqueue(2, "二文目です。", tone(0.5))
    assert [(e["type"], e["turn"], e["index"], e["text"], e["seconds"]) for e in sent] == [
        ("audio", 2, 0, "一文目です。", 1.0), ("audio", 2, 1, "二文目です。", 0.5)]
    assert set(sent[0]) == {"type", "turn", "id", "index", "text", "seconds"}
    assert len(sent[0]["id"]) >= 20 and sent[0]["id"] != sent[1]["id"]
    with wave.open(io.BytesIO(p.audio(sent[0]["id"])), "rb") as w:     # PCM 16 bit・モノラル・16 kHz
        assert (w.getnchannels(), w.getsampwidth(), w.getframerate(), w.getnframes()) == (1, 2, 16000, 16000)
        samples = np.frombuffer(w.readframes(10), dtype="<i2")
    assert int(samples[0]) == round(0.25 * 32767) or int(samples[0]) == int(0.25 * 32767)
    p.set_turn(3)
    p.enqueue(3, "三の文です。", tone())
    assert sent[-1]["index"] == 0                        # 発言ごとに 0 から


def test_end_marks_heard_and_deletes_audio_error_does_not():
    sent = Recorder()
    p = BrowserPlayer(emit=sent)
    p.set_turn(1)
    for text in ("一文目です。", "二文目です。", "三文目です。"):
        p.enqueue(1, text, tone(0.2))
    a, b, c = (e["id"] for e in sent)
    assert p.played(2, a, "end") == ("stale", None)      # 別の番号は何もしない
    assert p.audio(a) is not None
    assert p.played(1, a, "start") == ("ok", 0)
    assert p.played(1, a, "end") == ("ok", 0)
    assert p.audio(a) is None                            # 再生を終えた音はすぐ消す
    assert p.played(1, a, "end") == ("unknown", None)
    assert p.played(1, b, "error") == ("ok", 1)          # 再生できなかった文は聞いていない
    assert p.audio(b) is None
    assert p.played(1, c, "end") == ("ok", 2)
    assert p.heard(1) == ["一文目です。", "三文目です。"]
    assert p.held() == (0, 0) and not p.speaking()
    with pytest.raises(ValueError):
        p.played(1, c, "pause")


def test_set_turn_and_stop_discard_unplayed_audio():
    sent = Recorder()
    p = BrowserPlayer(emit=sent)
    p.set_turn(1)
    p.enqueue(1, "一文目です。", tone())
    p.enqueue(1, "二文目です。", tone())
    p.played(1, sent[0]["id"], "end")
    assert p.held()[0] == 1 and p.speaking()
    p.stop()
    assert p.held() == (0, 0) and p.audio(sent[1]["id"]) is None and not p.speaking()
    assert p.heard(1) == ["一文目です。"]               # 聞いた文は残す
    p.enqueue(1, "三文目です。", tone())
    p.set_turn(2)
    assert p.held() == (0, 0) and p.audio(sent[2]["id"]) is None
    assert p.played(1, sent[2]["id"], "end") == ("stale", None)
    p.close()
    p.enqueue(2, "閉じた後です。", tone())
    assert p.held() == (0, 0)


def test_speaking_gives_up_after_length_plus_5_seconds_without_notice():
    clock = Clock()
    sent = Recorder()
    p = BrowserPlayer(emit=sent, clock=clock)
    p.set_turn(1)
    p.enqueue(1, "一文目です。", tone(1.0))
    p.enqueue(1, "二文目です。", tone(2.0))
    assert p.speaking()
    clock.t += 5.9
    assert p.speaking()                                   # 1 秒 ＋ 5 秒の手前
    clock.t += 0.2                                        # 1 文目の期限（6 秒）を過ぎた
    assert p.speaking()                                   # 2 文目は 1 文目の期限から 2 ＋ 5 秒
    assert p.audio(sent[0]["id"]) is None
    clock.t += 6.8                                        # 1000 + 12.9
    assert p.speaking()
    clock.t += 0.2                                        # 1000 + 13.1 > 1000 + 6 + 7
    assert not p.speaking()
    assert p.heard(1) == [] and p.held() == (0, 0)

    # start を知らせたら、期限の起点はその時刻
    p.set_turn(2)
    p.enqueue(2, "三文目です。", tone(1.0))
    clock.t += 5.0
    p.played(2, sent[2]["id"], "start")
    clock.t += 5.5                                        # 起点（start）から 5.5 秒 < 6 秒
    assert p.speaking()
    p.played(2, sent[2]["id"], "end")
    assert not p.speaking() and p.heard(2) == ["三文目です。"]


def test_player_limits_items_and_bytes():
    assert (wp.MAX_ITEMS, wp.MAX_BYTES) == (64, 20 * 1024 * 1024)
    sent = Recorder()
    p = BrowserPlayer(emit=sent, max_items=3)
    p.set_turn(1)
    for i in range(5):
        p.enqueue(1, f"{i}番目です。", tone(0.1))
    assert p.held()[0] == 3
    assert p.audio(sent[0]["id"]) is None and p.audio(sent[1]["id"]) is None   # 古いものから捨てる
    assert all(p.audio(e["id"]) is not None for e in sent[2:])

    one = len(wp.to_wav(tone(1.0)))
    sent2 = Recorder()
    p2 = BrowserPlayer(emit=sent2, max_bytes=one * 2 + 10)
    p2.set_turn(1)
    for i in range(4):
        p2.enqueue(1, f"{i}番目です。", tone(1.0))
    count, total = p2.held()
    assert count == 2 and total <= one * 2 + 10
    assert [p2.audio(e["id"]) is not None for e in sent2] == [False, False, True, True]


# ---------------------------------------------------------------- WebSession

class Streamer:
    """偽の handle。文を 1 つずつ逐次表示し、gate で回答の途中を止められる。"""

    def __init__(self, sentences, skill="knowledge-answer", gate_after=None):
        self.sentences, self.skill, self.gate_after = sentences, skill, gate_after
        self.gate = threading.Event()

    def __call__(self, text, ctx, ask=None):
        ctx.shown = ""
        for i, s in enumerate(self.sentences):
            if self.gate_after is not None and i == self.gate_after:
                assert self.gate.wait(5)
            ctx.stream(s)
            ctx.shown += s
        answer = "".join(self.sentences)
        return outcome(answer + "\n\n出典: [1] https://example.com", skill=self.skill) if self.skill == \
            "knowledge-answer" else outcome(answer, skill=self.skill)


def test_streamed_sentences_become_audio_before_answer_ends_and_history_is_heard():
    handle = Streamer(["一文目です。", "二文目です。"], gate_after=1)
    synth = Synth()
    s, events, fake = make_session(handle=handle, synth=synth)
    assert s.submit(wav(), 48000) == 1
    until(lambda: events.of("audio"))                     # 最初の文は回答の終わりを待たずに音になる
    assert events.of("audio")[0]["text"] == "一文目です。" and not events.of("answer_done")
    handle.gate.set()
    events.wait("turn_done")
    audio = events.of("audio")
    assert [(e["index"], e["text"]) for e in audio] == [(0, "一文目です。"), (1, "二文目です。")]
    assert synth.said == ["一文目です。", "二文目です。"]        # 出典は読まない・要約も読まない
    # すべての文の end の後に turn_done
    kinds = [(e["type"], e.get("event")) for e in events if e["type"] in ("_played", "turn_done")]
    assert kinds == [("_played", "start"), ("_played", "end"), ("_played", "start"), ("_played", "end"),
                     ("turn_done", None)]
    assert events.of("answer_done")[0]["text"].startswith("一文目です。二文目です。")   # 表示は全文
    assert [(t.speaker, t.text) for t in s.ctx.history] == [(Who.OWNER, "こんにちは"),
                                                          (Who.AI, "一文目です。二文目です。")]
    assert s.player.held() == (0, 0)
    assert s.submit(wav(), 48000) == 2                    # 次の発話を受け付ける


def test_not_streamed_answer_reads_summary():
    text = "1. 一つ目の知識（piano）\n2. 二つ目の知識\n"

    def handle(said, ctx, ask=None):
        return outcome(text, skill="knowledge-search")

    synth = Synth()
    s, events, _ = make_session(handle=handle, synth=synth)
    s.submit(wav(), 48000)
    events.wait("turn_done")
    summary = speakable(speech_summary("knowledge-search", text))
    assert synth.said == [summary] and summary.startswith("2 件見つかりました")
    assert events.of("answer_done")[0]["text"] == text
    assert s.ctx.history[-1].text == summary


def test_busy_until_browser_finishes_playing():
    browser = Browser(play=0)                             # 音を受けても再生を知らせない
    handle = Streamer(["一文目です。"], skill="general-chat")
    clock = Clock()
    s, events, _ = make_session(handle=handle, browser=browser, clock=clock)
    s.submit(wav(), 48000)
    events.wait("answer_done")
    until(lambda: events.of("audio"))
    time.sleep(0.1)
    assert not events.of("turn_done")
    from voice.web.session import Busy
    with pytest.raises(Busy):                             # 読み終えるまで次の発話は 409
        s.submit(wav(), 48000)
    aid = events.of("audio")[0]["id"]
    assert s.played(1, aid, "start") == "ok"
    assert s.played(1, aid, "end") == "ok"
    events.wait("turn_done")
    assert s.played(1, aid, "end") == "unknown"
    assert s.played(5, aid, "end") == "stale"


def test_history_keeps_only_sentences_played_to_the_end():
    browser = Browser(play=1)                             # 1 文目だけ再生して止まったブラウザ
    clock = Clock()
    handle = Streamer(["一文目です。", "二文目です。", "三文目です。"])
    s, events, _ = make_session(handle=handle, browser=browser, clock=clock)
    s.submit(wav(), 48000)
    events.wait("answer_done")
    until(lambda: ("end", 0, "ok") in browser.done)       # 1 文目は再生し終えた

    def advance():
        clock.t += 10                                     # 知らせのない文は「長さ＋5 秒」で待つのをやめる
    until(lambda: events.of("turn_done"), step=advance)
    assert len(events.of("audio")) == 3
    assert s.ctx.history[-1].speaker == Who.AI and s.ctx.history[-1].text == "一文目です。"
    assert s.player.held() == (0, 0)


def test_nothing_heard_adds_no_ai_line():
    browser = Browser(play=0)
    clock = Clock()
    s, events, _ = make_session(handle=Streamer(["一文目です。"]), browser=browser, clock=clock)
    s.submit(wav(), 48000)
    events.wait("answer_done")
    until(lambda: events.of("turn_done"), step=lambda: setattr(clock, "t", clock.t + 10))
    assert [(t.speaker, t.text) for t in s.ctx.history] == [(Who.OWNER, "こんにちは")]


def test_answer_exception_keeps_owner_line_without_ai_line():
    def broken(text, ctx, ask=None):
        raise RuntimeError("LLM が止まった")

    s, events, _ = make_session(handle=broken)
    s.submit(wav(), 48000)
    events.wait("turn_done")
    errors = events.of("error")
    assert len(errors) == 1 and errors[0]["kind"] == "answer"
    assert [(t.speaker, t.text) for t in s.ctx.history] == [(Who.OWNER, "こんにちは")]


def test_playback_error_sentence_is_not_in_history():
    browser = Browser(errors={1})
    s, events, _ = make_session(handle=Streamer(["一文目です。", "二文目です。", "三文目です。"]), browser=browser)
    s.submit(wav(), 48000)
    events.wait("turn_done")
    errors = events.of("error")
    assert len(errors) == 1 and errors[0]["kind"] == "playback" and errors[0]["turn"] == 1
    assert s.ctx.history[-1].text == "一文目です。三文目です。"


def test_end_stops_speech_and_deletes_held_audio():
    browser = Browser(play=0)
    clock = Clock()
    s, events, _ = make_session(handle=Streamer(["一文目です。", "二文目です。"]), browser=browser, clock=clock)
    s.submit(wav(), 48000)
    until(lambda: len(events.of("audio")) == 2)
    ids = [e["id"] for e in events.of("audio")]
    assert s.player.held()[0] == 2
    s.end()
    assert s.player.held() == (0, 0)
    assert all(s.audio(i) is None for i in ids)
    assert s.played(1, ids[0], "end") == "stale"
    events.wait("ended")
    time.sleep(0.1)
    assert not events.of("turn_done")                     # 終えた後は送らない


class FakeEngine:
    def available(self):
        return False, "テストでは使わない"

    def preload(self, style):
        raise AssertionError("使えないエンジンに読み込ませない")


class FakeLauncher:
    """voice.engine_process.EngineProcess と同じ形の偽物。呼ばれた順を log に残す。"""

    def __init__(self, log):
        self.log, self.owned, self.status, self.engine = log, False, "", FakeEngine()

    def start(self):
        self.log.append(("start",))
        self.owned, self.status = True, "起動しています"

    def wait_ready(self, timeout=None):
        self.log.append(("wait_ready",))
        return True

    def recover(self, timeout=20, speaker=None):
        self.log.append(("recover",))
        return True

    def stop(self):
        self.log.append(("stop",))
        self.owned = False


def test_engine_starts_with_session_waits_before_first_synth_and_stops_at_end():
    log = []
    launcher = FakeLauncher(log)
    synth = Synth(log=log)
    s, events, _ = make_session(handle=Streamer(["一文目です。"]), synth=synth, launcher=launcher)
    assert log == [("start",)]                            # 会話の開始で起動し始める（待たない）
    s.submit(wav(), 48000)
    events.wait("turn_done")
    assert log == [("start",), ("wait_ready",), ("synth", "一文目です。")]   # 待つのは最初の合成の前だけ
    s.submit(wav(), 48000)
    events.wait("turn_done", 2)
    assert log.count(("wait_ready",)) == 1
    s.end()
    assert log[-1] == ("stop",)
    s.end()
    assert log.count(("stop",)) == 1


def test_quit_word_stops_engine():
    log = []
    s, events, _ = make_session(Fake(text="終了"), launcher=FakeLauncher(log))
    s.submit(wav(), 48000)
    events.wait("ended")
    until(lambda: ("stop",) in log)


def test_real_synth_is_made_lazily_with_notice_and_recover(monkeypatch):
    log = []
    made = {}

    def make_synth(settings, notify=print, recover=None, fallback=None):
        made.update(settings=settings, recover=recover)
        notify("[音声] VOICEVOX は使えません（テスト）。macOS の say（Kyoko）で読み上げます。")
        return lambda sentence, cancel: tone(0.1)

    monkeypatch.setattr(tts, "make_synth", make_synth)
    fake = Fake()
    events_browser = Browser()
    from tests.test_voice_web import Events
    from voice.web.session import WebSession
    events = Events(events_browser)
    s = WebSession(transcribe=fake.transcribe, handle=Streamer(["一文目です。"]), emit=events, record=fake.record,
                   launcher=FakeLauncher(log))
    events_browser.session, events_browser.events = s, events
    assert made == {}                                     # 会話の開始では作らない（エンジンを待たない）
    s.submit(wav(), 48000)
    events.wait("turn_done")
    assert made["settings"] == tts.VoiceSettings() and made["recover"] is not None
    notices = events.of("notice")
    assert len(notices) == 1 and "say" in notices[0]["message"] and set(notices[0]) == {"session", "seq", "type",
                                                                                        "message"}
    made["recover"]()
    assert ("recover",) in log
    s.end()


# ---------------------------------------------------------------- 遅延の記録

def _latency_records():
    return latency.load(config.LATENCY_LOG)


def test_latency_records_web_turn_with_audio_ready_and_first_audio():
    s, events, _ = make_session(handle=Streamer(["一文目です。", "二文目です。"], skill="general-chat"))
    s.submit(wav(), 48000)
    events.wait("turn_done")
    until(lambda: _latency_records())
    rec = _latency_records()[-1]
    assert rec["mode"] == "web" and rec.get("ok") is not False
    st = rec["stages"]
    assert {"stt", "first_text", "audio_ready", "first_audio", "done"} <= set(st)
    assert st["stt"] <= st["first_text"] <= st["audio_ready"] <= st["first_audio"]
    text = json.dumps(rec, ensure_ascii=False)
    assert "一文目" not in text and "こんにちは" not in text   # 本文は記録しない


def test_latency_failed_turn_is_not_ok_and_noise_is_not_recorded():
    s, events, fake = make_session(Fake(text="ご視聴ありがとうございました"))
    s.submit(wav(), 48000)
    events.wait("turn_done")
    assert _latency_records() == []
    fake.text = RuntimeError("whisper failed")
    s.submit(wav(), 48000)
    events.wait("turn_done", 2)
    until(lambda: _latency_records())
    assert _latency_records()[-1]["ok"] is False and _latency_records()[-1]["mode"] == "web"


def test_latency_report_shows_web_and_audio_ready(capsys):
    assert latency.STAGES.index("first_text") < latency.STAGES.index("audio_ready") < latency.STAGES.index(
        "first_audio")
    assert "web" in latency.MODES
    turn = latency.Turn("web", clock=Clock(0.0))
    turn.clock = Clock(0.4)
    turn.mark("first_text")
    turn.clock = Clock(0.9)
    turn.mark("audio_ready")
    turn.clock = Clock(1.2)
    turn.mark("first_audio")
    latency.finish(turn, path=config.LATENCY_LOG)
    assert latency.main(["report", "--mode", "web"], path=config.LATENCY_LOG) == 0
    out = capsys.readouterr().out
    lines = {line.split()[0]: line.split() for line in out.splitlines() if line.strip()}
    assert lines["audio_ready"] == ["audio_ready", "1", "900", "900"]
    assert lines["first_audio"] == ["first_audio", "1", "1200", "1200"]
    assert "記録 1 件" in out


# ---------------------------------------------------------------- サーバー

def _session_with_audio(port, fake, made):
    fake.synth = Synth()                                  # 音を作る
    sid = json.loads(call(port, "POST", "/api/session")[1])["session"]
    conn, resp = _open_events(port, sid)
    assert _utter(port, sid, wav())[0].status == 202
    events, _ = _read_events(resp, "audio")
    audio = [e for e in events if e["type"] == "audio"]
    assert audio, events
    return sid, conn, resp, audio[0]


def test_audio_endpoint_requires_cookie_and_current_session(served):
    port, fake, made = served
    sid, conn, resp, ev = _session_with_audio(port, fake, made)
    path = f"/api/audio/{ev['id']}?session={sid}"
    assert call(port, "GET", path, cookie=False)[0].status == 403
    assert call(port, "GET", path, host="evil.example")[0].status == 400
    r, body = call(port, "GET", path)
    assert r.status == 200 and r.getheader("Content-Type") == "audio/wav" and r.getheader("Cache-Control") == "no-store"
    with wave.open(io.BytesIO(body), "rb") as w:
        assert (w.getnchannels(), w.getframerate()) == (1, 16000)
    assert call(port, "GET", f"/api/audio/{ev['id']}?session=other")[0].status == 404     # 別の会話の ID
    assert call(port, "GET", f"/api/audio/unknown-id?session={sid}")[0].status == 404
    assert call(port, "GET", f"/api/audio/../x?session={sid}")[0].status == 404
    # end を知らせた音は消える
    hdr = {"Content-Type": "application/json"}
    body = json.dumps({"turn": ev["turn"], "id": ev["id"], "event": "end"})
    assert call(port, "POST", f"/api/played?session={sid}", body=body, headers=hdr)[0].status == 200
    assert call(port, "GET", path)[0].status == 404
    conn.close()


def test_played_endpoint_checks_origin_header_and_turn(served):
    port, fake, made = served
    sid, conn, resp, ev = _session_with_audio(port, fake, made)
    hdr = {"Content-Type": "application/json"}
    url = f"/api/played?session={sid}"

    def body(**kw):
        return json.dumps({"turn": ev["turn"], "id": ev["id"], "event": "start", **kw})
    assert call(port, "POST", url, body=body(), headers=hdr, origin=False)[0].status == 403
    assert call(port, "POST", url, body=body(), headers=hdr, origin="http://evil.example")[0].status == 403
    assert call(port, "POST", url, body=body(), headers=hdr, csrf=False)[0].status == 403
    assert call(port, "POST", url, body=body(), headers=hdr, cookie=False)[0].status == 403
    r, data = call(port, "POST", url, body=body(turn=ev["turn"] + 1, event="end"), headers=hdr)
    assert r.status == 409 and json.loads(data) == {"error": "stale_turn"}
    assert made[0].audio(ev["id"]) is not None            # 409 では何もしない
    assert call(port, "POST", url, body=body(event="pause"), headers=hdr)[0].status == 400
    assert call(port, "POST", url, body=body(turn="1"), headers=hdr)[0].status == 400
    assert call(port, "POST", url, body=b"{not json", headers=hdr)[0].status == 400
    assert call(port, "POST", url, body=body(id="nope"), headers=hdr)[0].status == 404
    assert call(port, "POST", "/api/played?session=other", body=body(), headers=hdr)[0].status == 404
    assert call(port, "POST", url, body=body(), headers=hdr)[0].status == 200
    assert call(port, "POST", url, body=body(event="end"), headers=hdr)[0].status == 200
    events, _ = _read_events(resp, "turn_done")
    assert events[-1]["type"] == "turn_done"
    assert made[0].ctx.history[-1].speaker == Who.AI
    conn.close()


def test_page_plays_audio_and_reports_played():
    from pathlib import Path
    js = (Path(__file__).resolve().parent.parent / "voice" / "web" / "static" / "app.js").read_text(encoding="utf-8")
    for needle in ('case "audio"', 'case "turn_done"', 'case "notice"', "decodeAudioData", "/api/played",
                   "/api/audio/", "話しています", "AFTER_SPEECH_MS = 300"):
        assert needle in js, needle
    # turn_done の後（サーバーが再生の知らせを待つ期限を過ぎた後）に届いた・取得中の音は再生しない
    assert "turnDone = true;\n    discardPlayback();" in js
    assert "!source && audio && !turnDone" in js
    for banned in ("localStorage", "sessionStorage", "indexedDB", "caches."):
        assert banned not in js, banned
