"""言いかけの文で答え始めない（T-3102、ワークスペースの testing §30）。偽の認識・handle・合成で動かす。"""
import time

import pytest

import config
from ckc.models import Speaker as Who
from tests.test_voice_web import Fake, make_session, wav
from tests.test_voice_web_speech import until
from voice import tts
from voice.web import turn_end


@pytest.fixture(autouse=True)
def _isolated(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "LATENCY_LOG", tmp_path / "latency" / "latency.jsonl")
    monkeypatch.setattr(tts, "SETTINGS_PATH", tmp_path / "voice" / "settings.json")


@pytest.mark.parametrize("text", ["要するに会話の中でナレッジを思い出して、それを使って引用しながら。",
                                  "一日に何回も承認しなくちゃ大変だから。", "ある程度承認をいちいち…",
                                  "えっとだね、それで、あの", "まずは大洗の物件を", "その件ですけど"])
def test_continues(text):
    assert turn_end.continues(text)


@pytest.mark.parametrize("text", ["教えて。", "こんにちは。", "どうも。", "今どれ？", "うん、こうやって言う。",
                                  "ナレッジ化して", "大洗に行くのはいつがいいかな？", "ありがとうございます。", ""])
def test_not_continues(text):
    assert not turn_end.continues(text)


def test_continue_seconds_env(monkeypatch):
    monkeypatch.setenv("JARVIS_VOICE_WEB_CONTINUE_SECONDS", "0")
    assert turn_end.continue_seconds() == 0
    monkeypatch.setenv("JARVIS_VOICE_WEB_CONTINUE_SECONDS", "9")
    assert turn_end.continue_seconds() == turn_end.DEFAULT_CONTINUE_SECONDS
    monkeypatch.setenv("JARVIS_VOICE_WEB_CONTINUE_SECONDS", "x")
    assert turn_end.continue_seconds() == turn_end.DEFAULT_CONTINUE_SECONDS


class Seq(Fake):
    """発話ごとに違う文を返す偽の認識。"""

    def __init__(self, texts):
        super().__init__()
        self.texts = list(texts)

    def transcribe(self, audio):
        self.heard.append(audio)
        return self.texts.pop(0)


def _history(s):
    return [(t.speaker, t.text) for t in s.ctx.history]


@pytest.mark.parametrize("via_interrupt", [False, True])
def test_continuation_is_joined_into_one_turn(via_interrupt):
    fake = Seq(["大変だから。", "ある程度は任せたい。"])
    s, events, _ = make_session(fake, continue_seconds=3.0)
    first = s.submit(wav(), 48000)
    until(lambda: s._active is not None and s._active.held is not None)   # 言いかけで待っている
    assert not events.of("recognized") and fake.handled == []
    if via_interrupt:
        assert s.interrupt(first) == "ok"
    s.submit(wav(), 48000)
    events.wait("turn_done", count=2)
    assert fake.handled == ["大変だから。ある程度は任せたい。"]          # 1 回だけ答える
    assert [e["text"] for e in events.of("recognized")] == ["大変だから。ある程度は任せたい。"]
    assert _history(s) == [(Who.OWNER, "大変だから。ある程度は任せたい。"), (Who.AI, "答えです")]


def test_noise_after_continuation_answers_first_sentence():
    fake = Seq(["大変だから。", ""])
    s, events, _ = make_session(fake, continue_seconds=3.0)
    s.submit(wav(), 48000)
    until(lambda: s._active is not None and s._active.held is not None)
    s.submit(wav(), 48000)
    events.wait("turn_done", count=2)
    assert fake.handled == ["大変だから。"]


def test_no_continuation_answers_after_wait():
    fake = Seq(["大変だから。"])
    s, events, _ = make_session(fake, continue_seconds=0.3)
    started = time.monotonic()
    s.submit(wav(), 48000)
    events.wait("turn_done")
    assert fake.handled == ["大変だから。"] and time.monotonic() - started >= 0.3


def test_speech_check_extends_wait(monkeypatch):
    monkeypatch.setattr(turn_end, "EXTEND_SECONDS", 0.8)
    fake = Seq(["大変だから。"])
    s, events, _ = make_session(fake, continue_seconds=0.2)
    turn = s.submit(wav(), 48000)
    until(lambda: s._active is not None and s._active.held is not None)
    s.speech_started(turn)
    time.sleep(0.4)
    assert fake.handled == []                     # 延ばした期限の中
    events.wait("turn_done")
    assert fake.handled == ["大変だから。"]


@pytest.mark.parametrize("seconds, text", [(3.0, "教えて。"), (0, "大変だから。")])
def test_complete_sentence_or_disabled_does_not_wait(seconds, text):
    fake = Seq([text])
    s, events, _ = make_session(fake, continue_seconds=seconds)
    started = time.monotonic()
    s.submit(wav(), 48000)
    events.wait("turn_done")
    assert fake.handled == [text] and time.monotonic() - started < 1.0
