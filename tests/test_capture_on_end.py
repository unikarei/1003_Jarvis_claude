"""会話の終わりの知識化（T-1803、統合ワークスペースの testing §15）のテスト。

文字の会話・Mac の声の会話は終わりに y/N で聞き、ブラウザは候補を保存待ちにして次の会話の始めに知らせる。
偽の LLM（抽出を差し替え）・一時の Vault・一時の DB・偽の音声・偽のブラウザだけを使う。
"""

from types import SimpleNamespace
import queue

import numpy as np
import pytest

import chat_actions
import orchestrator as orc
from ckc.models import Speaker, Turn
from tests.test_voice_web import Fake, make_session, wav
from tests.test_voice_web_speech import until

OWNER3 = [Turn(Speaker.OWNER, "一つ目"), Turn(Speaker.AI, "はい"), Turn(Speaker.OWNER, "二つ目"),
          Turn(Speaker.OWNER, "三つ目")]


@pytest.fixture(autouse=True)
def enabled(monkeypatch):
    monkeypatch.setenv("JARVIS_CAPTURE_ON_END", "1")


@pytest.fixture
def ran(monkeypatch):
    calls = []

    def fake_run_ckc(history, ask=None):
        calls.append((list(history), ask))
        return True
    monkeypatch.setattr(chat_actions, "run_ckc", fake_run_ckc)
    return calls


# ---------------------------------------------------------------- 判定

def test_worth_capturing_needs_three_owner_turns_and_the_setting(monkeypatch):
    assert chat_actions.worth_capturing(OWNER3)
    assert not chat_actions.worth_capturing(OWNER3[:3])                 # Owner の発言 2 回
    monkeypatch.setenv("JARVIS_CAPTURE_ON_END", "0")
    assert not chat_actions.worth_capturing(OWNER3)


@pytest.mark.parametrize("reply, runs", [("y", True), ("Y ", True), ("n", False), ("", False), ("はい", False)])
def test_offer_runs_only_on_y(ran, reply, runs):
    asked = []
    assert chat_actions.offer_capture_at_end(OWNER3, lambda p: asked.append(p) or reply) is runs
    assert asked == [chat_actions.END_PROMPT] and bool(ran) is runs
    if runs:
        assert ran[0][0] == OWNER3 and ran[0][1] is not None       # 候補ごとの確認も同じ ask で聞く


def test_offer_is_not_asked_for_short_conversations_or_when_input_closes(ran):
    asked = []
    assert not chat_actions.offer_capture_at_end(OWNER3[:2], lambda p: asked.append(p) or "y")
    assert asked == []

    def closed(prompt):
        raise EOFError
    assert not chat_actions.offer_capture_at_end(OWNER3, closed) and ran == []


# ---------------------------------------------------------------- 文字の会話（chat.py）

def _chat(monkeypatch, inputs):
    import builtins
    import io
    import sys
    for name in ("stdin", "stdout"):
        monkeypatch.setattr(sys, name, io.TextIOWrapper(io.BytesIO(), encoding="utf-8"))
    import chat
    feed = iter(inputs)
    monkeypatch.setattr(builtins, "input", lambda prompt="": next(feed))
    monkeypatch.setattr(chat, "init_db", lambda: None)
    monkeypatch.setattr(chat, "record", lambda *a: None)
    monkeypatch.setattr(orc, "start_warm_up", lambda **kwargs: None)
    monkeypatch.setattr(orc, "handle", lambda text, ctx, ask=None, use_llm=None:
                        orc.Outcome("general-chat", "default", {}, "答え", True, 0.0))
    chat.main()


def test_chat_exit_asks_and_y_starts_the_capture(monkeypatch, ran):
    _chat(monkeypatch, ["一つ目", "二つ目", "三つ目", "exit", "y"])
    assert len(ran) == 1 and [t.text for t in ran[0][0] if t.speaker == Speaker.OWNER] == ["一つ目", "二つ目", "三つ目"]


def test_chat_exit_with_no_does_nothing_and_short_chats_are_not_asked(monkeypatch, ran):
    _chat(monkeypatch, ["一つ目", "二つ目", "三つ目", "exit", "n"])
    assert ran == []
    _chat(monkeypatch, ["一つ目", "exit"])                              # 聞かない（次の入力を読まない）
    assert ran == []


# ---------------------------------------------------------------- Mac の声の会話（voice_chat.py）

class SaidSpeaker:
    def __init__(self, keys=None, answers=()):
        self.said, self.keys, self.answers = [], keys, list(answers)

    def feed(self, chunk): pass
    def flush(self): pass
    def stop(self): pass

    def say(self, text):
        self.said.append(text)
        if self.keys is not None and text == chat_actions.END_SPOKEN and self.answers:
            self.keys.put(self.answers.pop(0))

    def speaking(self): return False
    def wait(self): pass
    def close(self): pass


@pytest.mark.parametrize("answer, runs", [("y", True), ("n", False)])
def test_voice_quit_word_asks_on_the_keyboard(monkeypatch, ran, answer, runs):
    import voice_chat
    monkeypatch.setattr(orc, "handle", lambda text, ctx, ask=None, use_llm=None:
                        orc.Outcome("general-chat", "default", {}, "答え", True, 0.0))
    monkeypatch.setattr(voice_chat, "record", lambda *a: None)
    texts = iter(["一つ目", "二つ目", "三つ目", "終了"])
    keys = queue.Queue()
    sp = SaidSpeaker(keys, [answer])
    assert voice_chat.main(listen=lambda: np.zeros(10, dtype=np.float32), transcribe=lambda a: next(texts),
                           speaker=sp, keys=keys) == 0
    assert chat_actions.END_SPOKEN in sp.said and bool(ran) is runs


def test_voice_answer_timeout_is_no(monkeypatch, ran):
    import voice_chat
    monkeypatch.setattr(voice_chat, "END_ANSWER_SECONDS", 0.1)
    ctx = orc.Context(history=list(OWNER3))
    voice_chat.offer_capture(ctx, queue.Queue(), SaidSpeaker())
    assert ran == []


# ---------------------------------------------------------------- ブラウザ（voice/web）

def _session(capture=None, pending=lambda: 0, text="こんにちは"):
    captured = []
    fake = Fake(text=text)
    s, events, _ = make_session(fake, handle=fake.handle)
    s.capture_on_end = capture or captured.append
    return s, events, fake, captured


def test_browser_quit_word_puts_candidates_on_hold_once(monkeypatch):
    s, events, fake, captured = _session()
    s.ctx.history.extend(OWNER3)
    fake.text = "終了"
    s.submit(wav(), 48000)
    events.wait("ended")
    until(lambda: len(captured) == 1)
    assert [t.text for t in captured[0]] == [t.text for t in OWNER3]
    s.end(capture=True)                                                 # 2 回目の終わりでは何もしない
    assert len(captured) == 1


def test_browser_end_button_captures_but_disconnect_does_not():
    s, events, _, captured = _session()
    s.ctx.history.extend(OWNER3)
    s.end(capture=True)                                                 # /api/end（「会話を終える」）
    until(lambda: len(captured) == 1)
    s2, _, _, captured2 = _session()
    s2.ctx.history.extend(OWNER3)
    s2.end()                                                            # 切断・サーバーの停止
    import time
    time.sleep(0.1)
    assert captured2 == []


def test_browser_short_conversation_or_setting_off_is_not_captured(monkeypatch):
    s, _, _, captured = _session()
    s.ctx.history.extend(OWNER3[:2])
    s.end(capture=True)
    monkeypatch.setenv("JARVIS_CAPTURE_ON_END", "0")
    s2, _, _, captured2 = _session()
    s2.ctx.history.extend(OWNER3)
    s2.end(capture=True)
    import time
    time.sleep(0.1)
    assert captured == [] and captured2 == []


def test_browser_capture_on_end_only_holds_candidates(db_path, ckc_data_dir, make_vault, monkeypatch):
    """既定の保存待ち: 候補を作るだけで、Vault にも jarvis.db の知識にも書かない。"""
    import ckc.extract
    from ckc import store
    from ckc.models import Candidate, CandidateStatus, Confidence, Evidence, Kind, new_id
    from voice.web import session as web_session
    jk = make_vault("jarvis-knowledge")

    def fake_extract(conversation_id):
        c = Candidate(new_id("cand"), conversation_id, Kind.DECISION, "三つ目", "三つ目にする", ["jarvis"], None,
                      Confidence.CONFIRMED, [Evidence("三つ目", Speaker.OWNER)], resource_id="vault-jarvis-knowledge")
        store.add_candidates([c])
        return ckc.extract.ExtractResult([c], [], [])
    monkeypatch.setattr(ckc.extract, "extract", fake_extract)
    web_session._capture_pending(list(OWNER3))
    assert [c.status for c in store.list_candidates()] == [CandidateStatus.PENDING]
    assert not (jk / "_jarvis").exists()
    import db
    assert db._fetchall("SELECT id FROM nodes WHERE type = 'Knowledge'") == []


def test_browser_start_tells_the_number_of_pending_candidates():
    from tests.test_voice_web import Browser, Events, Synth
    from voice.web.session import WebSession
    events = Events(Browser())
    WebSession(transcribe=lambda a: "", handle=lambda *a, **k: None, emit=events, record=lambda *a: None,
               synth=Synth(), pending_count=lambda: 2)
    notices = [e["message"] for e in events.of("notice")]
    assert any("保存待ちの知識の候補が 2 件あります" in m for m in notices)
    events2 = Events(Browser())
    WebSession(transcribe=lambda a: "", handle=lambda *a, **k: None, emit=events2, record=lambda *a: None,
               synth=Synth(), pending_count=lambda: 0)
    assert events2.of("notice") == []


def test_capture_with_no_history_reviews_pending_candidates(monkeypatch):
    seen = {}

    def fake_review(decide, gate=None):
        seen["called"] = True
        return "1 件を保存しました。"
    monkeypatch.setattr(chat_actions, "review_pending_with_cards", fake_review)
    ctx = orc.Context(terminal=False)
    ctx.review = lambda text, spoken=None, edit=None: "save"
    assert orc._capture({}, ctx, "ナレッジ化して") == ("1 件を保存しました。", []) and seen["called"]
    monkeypatch.setattr(chat_actions, "review_pending_with_cards", lambda decide, gate=None: None)
    assert orc._capture({}, ctx, "ナレッジ化して")[0] == "ナレッジ化する対話がまだありません。"


# ---------------------------------------------------------------- 会話の途中のナレッジ化で候補がないとき（T-2504、上位 spec §3.3.13 B）

def _with_history():
    ctx = orc.Context(terminal=False)
    ctx.history = [Turn(Speaker.OWNER, "あれ、地下して")]
    spoken = []
    ctx.review = lambda text, spoken_text=None, edit=None: spoken.append(spoken_text) or "later"
    return ctx, spoken


@pytest.mark.parametrize("found", ["none", "extract_error"])
def test_no_new_candidate_reviews_pending_with_notice(monkeypatch, found):
    from ckc.extract import ExtractError

    if found == "none":
        monkeypatch.setattr(chat_actions, "capture_candidates", lambda history: [])
    else:
        def fail(history):
            raise ExtractError("保存できる候補がありませんでした（候補なし）")
        monkeypatch.setattr(chat_actions, "capture_candidates", fail)
    pending = [SimpleNamespace(title="t1", body="b1"), SimpleNamespace(title="t2", body="b2")]
    monkeypatch.setattr("ckc.store.list_candidates", lambda *a, **k: pending)
    monkeypatch.setattr("ckc.review.card_text", lambda c, i, n: f"card {i}/{n}")
    monkeypatch.setattr("ckc.review.decide_each",
                        lambda cands, decide, gate=None: ([decide(c, i, len(cands)) for i, c in enumerate(cands, 1)]
                                                          and {"saved": 0, "rejected": 0, "later": 2, "failed": 0}))
    ctx, spoken = _with_history()
    message, _ = orc._capture({}, ctx, "ナレッジ化して")
    assert message.startswith("この会話からは新しい候補がありませんでした。保存待ちの候補: ")
    assert "2 件は保存待ちにしました" in message
    assert spoken == [orc.PENDING_SPOKEN.format(n=2), None]   # 最初のカードだけ、新しい候補がなかったことと件数を読む



def test_no_new_candidate_and_no_pending_keeps_the_old_message(monkeypatch):
    monkeypatch.setattr(chat_actions, "capture_candidates", lambda history: [])
    monkeypatch.setattr("ckc.store.list_candidates", lambda *a, **k: [])
    ctx, spoken = _with_history()
    assert orc._capture({}, ctx, "ナレッジ化して")[0] == "知識の候補はありませんでした。" and spoken == []


def test_capture_failure_or_new_candidates_do_not_show_pending(monkeypatch):
    from ckc.capture import CaptureError
    called = []
    monkeypatch.setattr(chat_actions, "review_pending_with_cards", lambda *a, **k: called.append(1) or "x")

    def fail(history):
        raise CaptureError("秘密情報を検出しました")
    monkeypatch.setattr(chat_actions, "capture_candidates", fail)
    ctx, _ = _with_history()
    assert orc._capture({}, ctx, "ナレッジ化して")[0] == "[CKC] 秘密情報を検出しました"
    monkeypatch.setattr(chat_actions, "capture_candidates", lambda history: ["new"])
    monkeypatch.setattr("ckc.review.decide_each", lambda c, d, g=None: {"saved": 1, "rejected": 0, "later": 0, "failed": 0})
    ctx, _ = _with_history()
    assert orc._capture({}, ctx, "ナレッジ化して")[0] == "1 件を保存しました。" and called == []
