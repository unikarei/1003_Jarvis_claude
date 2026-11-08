"""声の会話（voice_chat.py）とブラウザでの声の会話（voice/web）での `obsidian-note`（T-1702、testing §14）のテスト。

確認の読み上げ（spoken）、割り込みの後も文脈を作り直さず途中の依頼を使うこと、履歴に入れないこと、ブラウザでの
2 段目の確認（入力欄の語をサーバーで照合）を、一時の Vault・偽の LLM・偽の音声・偽のブラウザで確かめる。
"""

import json
import queue
import threading
import time
import warnings

import numpy as np
import pytest

import llm
import orchestrator as orc
from tests.test_obsidian_note import LLM, MEMO, env, read  # noqa: F401  （env は fixture）
from tests.test_voice_barge_in import FakeSession
from tests.test_voice_web import Fake, make_session, wav
from tests.test_voice_web_speech import Clock, until
from voice import duplex


def _keys(*lines):
    q = queue.Queue()
    for line in lines:
        q.put(line)
    return q


class SaidSpeaker:
    """voice_chat.main（割り込みなし）の読み上げの偽物。読んだ文を残す。確認の文（「キーボードで」）を読んだら、
    answers の次の答えをキーの列に入れる（読み上げを待つ間のキーの処理に先に取られないように）。"""

    def __init__(self, keys=None, answers=()):
        self.said, self.keys, self.answers = [], keys, list(answers)

    def feed(self, chunk): pass
    def flush(self): pass
    def stop(self): pass

    def say(self, text):
        self.said.append(text)
        if self.keys is not None and "キーボードで" in text and self.answers:
            self.keys.put(self.answers.pop(0))
    def speaking(self): return False
    def wait(self): pass
    def close(self): pass


# ---------------------------------------------------------------- voice_chat.py（割り込みなし）

def test_voice_without_barge_in_reads_the_confirmation_and_keeps_history_empty(env, monkeypatch):
    import voice_chat
    env["llm"].reply = {"note": "大洗メモ", "content": "声の追記"}
    contexts = []
    original = orc.Context
    monkeypatch.setattr(orc, "Context", lambda **kw: contexts.append(original(**kw)) or contexts[-1])
    texts = iter(["Obsidian の大洗メモに追加して", "終了"])
    keys = _keys()
    sp = SaidSpeaker(keys, ["y"])
    assert voice_chat.main(listen=lambda: np.zeros(10, dtype=np.float32), transcribe=lambda a: next(texts),
                           speaker=sp, keys=keys) == 0
    assert any("に追記します。キーボードで y を押してください" in s for s in sp.said)
    assert "声の追記" in read(env["oarai"], MEMO)
    assert contexts[0].history == []


def test_voice_delete_reads_both_steps_and_needs_the_typed_word(env):
    import voice_chat
    env["llm"].reply = {"note": "大洗メモ"}
    texts = iter(["Obsidian の大洗メモを削除して", "終了"])
    keys = _keys()
    sp = SaidSpeaker(keys, ["y", "削除"])
    voice_chat.main(listen=lambda: np.zeros(10, dtype=np.float32), transcribe=lambda a: next(texts), speaker=sp,
                    keys=keys)
    assert any("を削除します。Jarvis の知識からも消します。よろしいですか。キーボードで答えてください" in s for s in sp.said)
    assert "本当に消します。キーボードで『削除』と入力してください" in sp.said
    assert not (env["oarai"] / MEMO).exists()


# ---------------------------------------------------------------- voice_chat.py（割り込みあり）

def _run(session, keys):
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", UserWarning)
        import voice_chat
        return voice_chat.run_barge_in(session, transcribe=lambda audio: audio, keys=keys)


def test_barge_in_during_processing_waits_and_keeps_the_pending_request(env, monkeypatch):
    """取り出しの途中で割り込まれても、処理を待って文脈を作り直さず、次の発言を途中の依頼（内容）に渡す。
    確認の要点を読み、書き込みの結果を伝え、履歴には何も入れない。"""
    gate = threading.Event()
    reply = LLM({"note": "大洗メモ"})

    def slow(prompt, provider=None, timeout=None):
        gate.wait(5)                                   # 割り込まれるまで取り出しを終えない
        return reply(prompt, provider, timeout)
    monkeypatch.setattr(llm, "complete", slow)
    contexts = []
    original = orc.Context
    monkeypatch.setattr(orc, "Context", lambda **kw: contexts.append(original(**kw)) or contexts[-1])
    session = FakeSession(["Obsidian の大洗メモに追加して", "牛乳を買う"])
    keys = queue.Queue()
    said = []

    def synth(sentence, cancel):
        said.append(sentence)
        if "キーボードで y を押してください" in sentence:
            keys.put("y")                               # 確認を待っている間に y を打つ
        return np.zeros(10, dtype=np.float32)
    session.speaker = duplex.DuplexSpeaker(session.player, synth=synth)
    original_arm = session.arm

    def arm_then_barge():
        original_arm()
        if len(session.listened) == 1:
            def barge():
                session.speaker.new_turn(); session.barged.set()
                time.sleep(0.3); gate.set()            # 割り込んでから取り出しを終える
            threading.Timer(0.05, barge).start()
    session.arm = arm_then_barge
    assert _run(session, keys) == 0
    assert len(contexts) == 1                           # 文脈を作り直していない
    assert "牛乳を買う" in read(env["oarai"], MEMO)       # 割り込みの後の発言が途中の依頼（内容）に渡った
    assert any("何を追加しますか" in s for s in said)    # 割り込まれた質問の文を出して読み上げた
    assert contexts[0].history == [] and contexts[0].pending is None


# ---------------------------------------------------------------- ブラウザ（voice/web）

def real_handle(text, ctx, ask=None):
    return orc.handle(text, ctx, ask=ask, use_llm=False)


def _approvals(events):
    return events.of("approval")


def start(text, clock=None):
    clock = clock or Clock()
    fake = Fake(text=text)
    s, events, _ = make_session(fake, handle=real_handle, clock=clock)
    s.submit(wav(), 48000)
    return s, events, fake, clock


def test_browser_one_step_writes_only_with_run(env):
    env["llm"].reply = {"note": "大洗メモ", "content": "画面の追記"}
    s, events, _, _ = start("Obsidian の大洗メモに追加して")
    events.wait("approval")
    req = _approvals(events)[0]
    assert "画面の追記" in req["text"] and "input" not in req and "[y/N" not in req["text"]
    until(lambda: any("画面で答えてください" in x for x in s.speaker.heard(1)))
    assert s.approve(1, req["id"], "run") == "ok"
    events.wait("turn_done")
    assert "画面の追記" in read(env["oarai"], MEMO)
    assert [e["result"] for e in events.of("approval_done")] == ["run"]
    assert s.ctx.history == []


@pytest.mark.parametrize("how", ["decline", "expire", "speak"])
def test_browser_one_step_is_not_written_otherwise(env, how):
    env["llm"].reply = {"note": "大洗メモ", "content": "画面の追記"}
    before = read(env["oarai"], MEMO)
    s, events, fake, clock = start("Obsidian の大洗メモに追加して")
    events.wait("approval")
    req = _approvals(events)[0]
    if how == "decline":
        s.approve(1, req["id"], "decline")
    elif how == "expire":
        clock.t += 61
    else:
        fake.text = "はい"
        s.submit(wav(), 48000)                          # 声の発言は確認にならない（割り込みで取り消す）
    events.wait("turn_done")
    time.sleep(0.2)
    assert read(env["oarai"], MEMO) == before


def _delete_second_step(env, op="delete"):
    env["llm"].reply = {"note": "大洗メモ", "content": "新しい全文"}
    text = "Obsidian の大洗メモを削除して" if op == "delete" else "Obsidian の大洗メモの全体を書き直して"
    s, events, fake, clock = start(text)
    events.wait("approval")
    first = _approvals(events)[0]
    assert "input" not in first
    until(lambda: any("画面で答えてください" in x for x in s.speaker.heard(1)))
    assert s.approve(1, first["id"], "run") == "ok"
    events.wait("approval", 2)                          # 1 段目の承認が残って 2 段目が出ない、ということがない
    second = _approvals(events)[1]
    assert second["input"] is True and second["word"] == ("削除" if op == "delete" else "置き換え")
    assert second["id"] != first["id"]
    until(lambda: any("画面の入力欄に" in x for x in s.speaker.heard(1)))
    return s, events, fake, clock, second


@pytest.mark.parametrize("typed", ["削除", " 削除 "])
def test_browser_delete_with_the_typed_word(env, typed):
    s, events, _, _, second = _delete_second_step(env)
    assert s.approve(1, second["id"], "run", typed) == "ok"
    events.wait("turn_done")
    assert not (env["oarai"] / MEMO).exists()
    assert typed.strip() not in json.dumps(events.of("approval_done"), ensure_ascii=False)


@pytest.mark.parametrize("typed", [None, "", "y", "はい", "置き換え", "削除" * 9])
def test_browser_delete_is_not_run_without_the_right_word(env, typed):
    s, events, _, _, second = _delete_second_step(env)
    assert s.approve(1, second["id"], "run", typed) == "ok"            # 受け付けるが declined で確定
    assert s.approve(1, second["id"], "run", "削除") == "stale"         # 1 回違えば、送り直しても実行しない
    events.wait("turn_done")
    assert (env["oarai"] / MEMO).exists()


def test_browser_rewrite_needs_the_word_replace(env):
    s, events, _, _, second = _delete_second_step(env, op="rewrite")
    assert s.approve(1, second["id"], "run", "置き換え") == "ok"
    events.wait("turn_done")
    assert read(env["oarai"], MEMO) == "新しい全文"


@pytest.mark.parametrize("how", ["expire", "end", "speak", "other_turn"])
def test_browser_second_step_is_not_run_on_expiry_end_speech_or_other_ids(env, how):
    s, events, fake, clock, second = _delete_second_step(env)
    if how == "expire":
        clock.t += 61
        assert s.approve(1, second["id"], "run", "削除") == "stale"
    elif how == "end":
        s.end()
        assert s.approve(1, second["id"], "run", "削除") == "stale"
    elif how == "speak":
        fake.text = "削除"
        s.submit(wav(), 48000)
        assert s.approve(1, second["id"], "run", "削除") == "stale"
    else:
        assert s.approve(2, second["id"], "run", "削除") == "stale"
        assert s.approve(1, second["id"][:-1] + "x", "run", "削除") == "stale"
        clock.t += 61
    until(lambda: not any(t.is_alive() for t in threading.enumerate() if t.name.startswith("voice-web-turn-1")))
    assert (env["oarai"] / MEMO).exists()


def test_browser_server_checks_the_text_field(env):
    """/api/approve の text: 文字列以外は 400（使い切らない）、正しい語は 200 で実行する。"""
    from tests.test_voice_web import TOKEN, _open_events, call
    from voice.web import server as web
    from voice.web.session import WebSession
    env["llm"].reply = {"note": "大洗メモ"}
    events, made = [], []
    fake = Fake(text="Obsidian の大洗メモを削除して")

    def factory(emit):
        def both(event):
            events.append(event)
            emit(event)
        s = WebSession(transcribe=fake.transcribe, handle=real_handle, emit=both, record=fake.record,
                       synth=lambda sentence, cancel=None: None)
        made.append(s)
        return s
    srv, _ = web.make_server(0, factory, token=TOKEN, ping_seconds=0.2)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        port = srv.server_address[1]
        sid = json.loads(call(port, "POST", "/api/session")[1])["session"]
        conn, _ = _open_events(port, sid)
        call(port, "POST", f"/api/utterance?session={sid}&rate=48000", body=wav(),
             headers={"Content-Type": "audio/wav"})
        until(lambda: any(e["type"] == "approval" for e in events))
        first = [e for e in events if e["type"] == "approval"][0]
        assert made[0].approve(1, first["id"], "run") == "ok"
        until(lambda: sum(e["type"] == "approval" for e in events) == 2)
        second = [e for e in events if e["type"] == "approval"][1]

        def post(data):
            return call(port, "POST", f"/api/approve?session={sid}", body=json.dumps(data),
                        headers={"Content-Type": "application/json"})[0].status
        base = {"turn": 1, "id": second["id"], "decision": "run"}
        assert post({**base, "text": 5}) == 400 and post({**base, "text": ["削除"]}) == 400
        assert post({**base, "text": "削除"}) == 200
        until(lambda: not (env["oarai"] / MEMO).exists())
        conn.close()
    finally:
        srv.shutdown()
        srv.server_close()


def test_browser_interrupt_keeps_the_context_and_the_pending_request(env, monkeypatch):
    """取り出しの途中に割り込まれても文脈を作り直さず、次の発言は処理の終わりを待って途中の依頼に渡る。"""
    gate = threading.Event()
    reply = LLM({"note": "大洗メモ"})

    def slow(prompt, provider=None, timeout=None):
        gate.wait(5)
        return reply(prompt, provider, timeout)
    monkeypatch.setattr(llm, "complete", slow)
    s, events, fake, _ = start("Obsidian の大洗メモに追加して")
    events.wait("recognized")
    ctx = s.ctx
    fake.text = "牛乳を買う"
    s.submit(wav(), 48000)                              # 処理中の割り込み
    assert s.ctx is ctx
    time.sleep(0.1)
    gate.set()
    events.wait("approval")                            # 2 回目の発言が内容になり、確認が出る
    req = _approvals(events)[0]
    assert "牛乳を買う" in req["text"] and req["turn"] > 1          # 割り込んだ後の発言の確認
    s.approve(req["turn"], req["id"], "run")
    events.wait("turn_done", 2)
    assert "牛乳を買う" in read(env["oarai"], MEMO)
    assert s.ctx is ctx and s.ctx.history == [] and s.ctx.pending is None


def test_browser_interrupt_before_the_write_does_not_write(env, monkeypatch):
    """2 段目の答えの後、書き込みの直前（承認を使い切る前）に割り込むと書かない。"""
    import obsidian_note as on
    s, events, fake, _, second = _delete_second_step(env)
    original = on._gate
    entered, release = threading.Event(), threading.Event()
    calls = []

    def slow_gate(ctx):
        calls.append(1)
        if len(calls) == 1:                             # 書き込みの直前の使い切り
            entered.set()
            release.wait(5)
        return original(ctx)
    monkeypatch.setattr(on, "_gate", slow_gate)
    assert s.approve(1, second["id"], "run", "削除") == "ok"
    assert entered.wait(5)
    assert s.interrupt(1) == "ok"
    release.set()
    until(lambda: not any(t.name.startswith("voice-web-turn-1") for t in threading.enumerate()))
    assert (env["oarai"] / MEMO).exists()


def test_obsidian_busy_covers_pending_running_finished_and_unrouted():
    """割り込みの判定: 途中の依頼・処理中・書き込みが終わった直後（結果だけがある）・振り分けの前（発言の文）。"""
    import voice_chat
    ctx = orc.Context()
    done = orc.Outcome("obsidian-note", "rule", {}, "「大洗メモ」に追記しました。", True, 0.1, private=True)
    other = orc.Outcome("general-chat", "default", {}, "こんにちは", True, 0.1)
    assert voice_chat.obsidian_busy(ctx, done)                                 # pending も active_skill もない
    assert not voice_chat.obsidian_busy(ctx, other, "Obsidian に追加して")      # 結果があれば結果で決める
    assert voice_chat.obsidian_busy(ctx, None, "Obsidian に追加して")           # 振り分けの前は発言の文で
    assert not voice_chat.obsidian_busy(ctx, None, "こんにちは")
    ctx.active_skill = "obsidian-note"
    assert voice_chat.obsidian_busy(ctx, None, "こんにちは")
    ctx.active_skill, ctx.pending = None, orc.Pending("obsidian-note", {}, 0.0)
    assert voice_chat.obsidian_busy(ctx, None, "牛乳")
