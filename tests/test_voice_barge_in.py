"""声の会話の割り込み（Barge-in、ADR-0028、T-1407）のテスト。マイク・再生・音声合成・LLM は偽物。"""

import queue
import threading
import time

import numpy as np
import pytest

import llm
import skills
from voice import duplex


def _frame(level: float) -> np.ndarray:
    return np.full(duplex.FRAME, level, dtype=np.int16)


# ---------------------------------------------------------------- 判定

def test_gate_ignores_noise_and_echo_but_fires_on_voice():
    gate = duplex.EchoGate(noise=270, coupling=0.3, confirm_seconds=0.16)   # 8 フレーム
    assert gate.threshold(0) == 540                      # 再生がなければ周囲の音 × 2
    for _ in range(50):                                  # 周囲の音だけ
        assert not gate.update(280)
    for _ in range(50):                                  # 自分の声の回り込み（再生 2000 × 0.3 + 周囲の音）
        assert not gate.update(270 + 0.3 * 2000, ref=2000)
    fired = [gate.update(270 + 0.3 * 2000 + 3000, ref=2000) for _ in range(10)]   # 回り込みの上に人の声
    assert fired.index(True) == 7 and fired.count(True) == 1    # 0.16 秒で 1 回だけ


def test_gate_needs_sustained_voice():
    gate = duplex.EchoGate(noise=270)
    for _ in range(5):                                   # 0.1 秒の物音では止めない
        assert not gate.update(5000)
    assert not gate.update(270)
    assert not any(gate.update(5000) for _ in range(7))
    assert gate.update(5000)


def test_short_backchannel_is_detected():
    """「うん」「はい」（約 0.2〜0.3 秒）は 0.16 秒の確認で拾える。"""
    gate = duplex.EchoGate(noise=270)
    assert any(gate.update(1500) for _ in range(12))     # 0.24 秒


def test_estimate_coupling_finds_delay_and_gain():
    rng = np.random.default_rng(0)
    ref = np.abs(rng.normal(2000, 800, 200))
    delay = 6
    mic = 270 + np.concatenate([np.zeros(delay), 0.2 * ref[:-delay]]) + rng.normal(0, 5, 200)
    coupling, found = duplex.estimate_coupling(list(mic), list(ref), noise=270)
    assert found == delay
    assert 0.2 * duplex.ECHO_SAFETY * 0.9 < coupling < 0.2 * duplex.ECHO_SAFETY * 1.1


def test_no_coupling_with_headset():
    rng = np.random.default_rng(1)
    ref = np.abs(rng.normal(2000, 800, 200))
    mic = 270 + rng.normal(0, 5, 200)
    coupling, _ = duplex.estimate_coupling(list(mic), list(ref), noise=270)
    assert coupling < 0.01


# ---------------------------------------------------------------- 再生

class FakeStream:
    def __init__(self, cb):
        self.cb = cb
    def start(self): pass
    def stop(self): pass
    def close(self): pass
    def pump(self, n=1):
        out = np.zeros((duplex.FRAME, 1), dtype=np.float32)
        for _ in range(n):
            self.cb(out, duplex.FRAME, None, None)
        return out


def _player():
    holder = {}
    p = duplex.Player(stream_factory=lambda cb: holder.setdefault("s", FakeStream(cb)))
    return p, holder["s"]


def test_player_plays_in_order_and_records_reference():
    p, s = _player()
    p.enqueue(1, "一文目。", np.full(400, 0.1, dtype=np.float32))
    p.enqueue(1, "二文目。", np.full(400, 0.1, dtype=np.float32))
    assert p.speaking()
    out = s.pump()
    assert np.allclose(out[:, 0], 0.1)
    s.pump(2)
    assert p.heard(1) == ["一文目。", "二文目。"]
    time.sleep(p.output_latency + 0.01)                   # 出力の遅れの間は読み上げ中
    assert not p.speaking()
    assert p.level(0, time.monotonic() + 1) == pytest.approx(0.1 * 32768, rel=0.01)


def test_player_stop_discards_queue_and_partial_sentence():
    p, s = _player()
    p.enqueue(1, "一文目。", np.full(800, 0.1, dtype=np.float32))
    p.enqueue(1, "二文目。", np.full(800, 0.1, dtype=np.float32))
    s.pump()                                             # 一文目の途中
    p.stop()
    out = s.pump(5)
    assert not out.any() and not p.speaking()
    assert p.heard(1) == []                              # 途中で止めた文は聞いた文にしない


# ---------------------------------------------------------------- 読み上げ（発言の番号）

class FakePlayer:
    """enqueue したら、すぐ最後まで再生したことにする。"""
    def __init__(self):
        self.played, self.stops = [], 0
    def enqueue(self, turn, sentence, audio): self.played.append((turn, sentence))
    def stop(self): self.stops += 1
    def speaking(self): return False
    def heard(self, turn): return [s for t, s in self.played if t == turn]
    def level(self, a, b): return 0.0
    def close(self): pass


def test_speaker_drops_sentences_of_old_turns():
    started, release = threading.Event(), threading.Event()

    def synth(sentence, cancel):
        if sentence == "遅い文。":
            started.set()
            release.wait(2)
        return np.zeros(10, dtype=np.float32)
    player = FakePlayer()
    sp = duplex.DuplexSpeaker(player, synth=synth)
    t1 = sp.new_turn()
    sp.feed("遅い文。次の文。")
    assert started.wait(2)
    t2 = sp.new_turn()                                    # 合成の途中で割り込み
    release.set()
    sp.say("新しい答え。")
    sp.wait()
    assert player.heard(t1) == [] and player.heard(t2) == ["新しい答え。"]
    sp.close()


def test_speaker_stop_cancels_running_synthesis():
    seen = {}

    def synth(sentence, cancel):
        seen["cancel"] = cancel
        cancel.wait(2)
        return None
    sp = duplex.DuplexSpeaker(FakePlayer(), synth=synth)
    sp.say("長い文。")
    time.sleep(0.1)
    sp.stop()
    assert seen["cancel"].is_set()
    sp.close()


# ---------------------------------------------------------------- マイク（割り込みの判定と録音）

class StubSpeaker:
    def __init__(self):
        self.stops = 0
    def new_turn(self): self.stops += 1


class LevelPlayer(FakePlayer):
    def __init__(self, ref=0.0):
        super().__init__(); self.ref = ref
    def level(self, a, b): return self.ref


def _session(ref=0.0):
    speaker = StubSpeaker()
    s = duplex.Session(LevelPlayer(ref), speaker, stream_factory=lambda cb: FakeStream(cb))
    s.gate = duplex.EchoGate(noise=270, coupling=0.3)
    return s, speaker


def test_session_barges_in_only_when_armed():
    s, speaker = _session(ref=2000)
    for _ in range(20):
        s.push(_frame(5000))
    assert not s.barged.is_set() and speaker.stops == 0   # arm していなければ止めない
    s.arm()
    for _ in range(20):
        s.push(_frame(270 + 0.3 * 2000))                  # 自分の声の回り込みだけ
    assert not s.barged.is_set()
    for _ in range(8):
        s.push(_frame(5000))
    assert s.barged.is_set() and speaker.stops == 1 and not s.armed
    assert s.barge_index == 47 - 8 - duplex.Session.PREROLL_FRAMES
    assert s.stop_latency and s.stop_latency[0] < 0.2


def test_session_listen_after_barge_in_keeps_the_start():
    s, _ = _session()
    for _ in range(20):
        s.push(_frame(270))
    s.arm()
    for _ in range(40):                                   # 0.8 秒の発話
        s.push(_frame(3000))
    for _ in range(60):                                   # 1.2 秒の無音
        s.push(_frame(270))
    audio = s.listen(from_barge_in=True)
    assert audio is not None
    loud = int((np.abs(audio * 32768) > 2000).sum())
    assert loud >= 40 * duplex.FRAME                       # 検出より前の話し始めも含む


# ---------------------------------------------------------------- 会話（受け入れ A・B・E）

class FakeSession:
    """listen は台本の音を順に返す。音声合成のときに台本の文が来たら割り込む（読み上げ中の割り込み）。"""

    def __init__(self, utterances, barge_on=()):
        self.player = FakePlayer()
        self.barged = threading.Event()
        self.barge_on = set(barge_on)
        self.utterances = list(utterances)
        self.armed = False
        self.listened = []
        self.speaker = duplex.DuplexSpeaker(self.player, synth=self._synth)

    def _synth(self, sentence, cancel):
        if sentence in self.barge_on and self.armed:
            self.barge_on.discard(sentence)
            self.armed = False
            self.speaker.new_turn()                       # 本物の Session と同じく、番号を進めて止める
            self.barged.set()
            return None
        return np.zeros(10, dtype=np.float32)

    def calibrate(self, greeting):
        self.speaker.say(greeting)
        self.speaker.wait()
        return {"coupling": 0.0}

    def arm(self):
        self.barged.clear(); self.armed = True
    def disarm(self):
        self.armed = False
    def listen(self, from_barge_in=False):
        self.listened.append(from_barge_in)
        if not self.utterances:
            return "終了"
        return self.utterances.pop(0)
    def close(self): pass


@pytest.fixture
def kb(db_path, monkeypatch):
    import db
    db.init_db()
    db.insert_node("Knowledge", category="minpaku", title="大洗の補助金", content="民泊の補助金の条件",
                   url="https://example.com/a")
    skills.reset_cache()
    yield
    skills.reset_cache()


def _llm(monkeypatch, answers, delays=None, cancelled=None):
    """質問ごとの答え（文の並び）を、逐次表示で返す偽の LLM。Cancelled を受けたら記録する。"""
    import voice_chat
    calls = []

    def fake_stream(prompt, on_text, provider=None, timeout=None, cancel=None):
        parts = answers[min(len(calls), len(answers) - 1)]
        calls.append(prompt)
        try:
            for part in parts:
                end = time.monotonic() + (delays or {}).get(part, 0.01)
                while time.monotonic() < end:          # 本物と同じく 0.1 秒以内ごとに取り消しを確かめる
                    if cancel is not None and cancel():
                        raise llm.LLMCancelled("取り消されました")
                    time.sleep(0.01)
                on_text(part)
        except (voice_chat.Cancelled, llm.LLMCancelled):
            if cancelled is not None:
                cancelled.append(parts)
            raise
        return "".join(parts)
    monkeypatch.setattr(llm, "complete_stream", fake_stream)
    return calls


def _run(session, keys=()):
    import voice_chat
    q = queue.Queue()
    for k in keys:
        q.put(k)
    import warnings
    with warnings.catch_warnings():                       # テストの Registry がないことの警告は関係しない
        warnings.simplefilter("ignore", UserWarning)
        return voice_chat.run_barge_in(session, transcribe=lambda audio: audio, keys=q)


def test_A_barge_in_while_speaking_answers_the_new_question(kb, monkeypatch, capsys):
    """長い答えの途中で話すと止まり、新しい質問に答える。聞いた文だけが履歴に残る。"""
    import orchestrator
    contexts = []
    original = orchestrator.Context
    monkeypatch.setattr(orchestrator, "Context", lambda **kw: contexts.append(original(**kw)) or contexts[-1])
    _llm(monkeypatch, [["一文目です。", "二文目です。", "三文目です。"], ["新しい答えです。"]])
    session = FakeSession(["民泊の補助金の条件は？", "ちょっと待って、別の質問"], barge_on=["二文目です。"])
    assert _run(session) == 0
    out = capsys.readouterr().out
    assert "割り込み" in out and "あなた（声）> ちょっと待って、別の質問" in out
    spoken = [s for _, s in session.player.played]
    assert "一文目です。" in spoken and "新しい答えです。" in spoken
    assert "二文目です。" not in spoken and "三文目です。" not in spoken     # 古い音が戻らない
    assert session.listened[:2] == [False, True]                            # 割り込みの直後は話し始めから録る
    history = [t.text for t in contexts[0].history]
    assert history[0] == "民泊の補助金の条件は？"
    assert history[1] == "一文目です。" + __import__("voice_chat").INTERRUPTED_MARK   # 聞いた文だけ
    assert "三文目" not in "".join(history)


def test_B_consecutive_barge_ins_keep_only_the_latest(kb, monkeypatch, capsys):
    _llm(monkeypatch, [["一の一。", "一の二。"], ["二の一。", "二の二。"], ["三の答え。"]])
    session = FakeSession(["質問一", "質問二", "質問三"], barge_on=["一の二。", "二の二。"])
    assert _run(session) == 0
    spoken = [s for _, s in session.player.played]
    assert "三の答え。" in spoken
    assert "一の二。" not in spoken and "二の二。" not in spoken
    assert capsys.readouterr().out.count("割り込み: 読み上げを止めました") == 2


def test_E_barge_in_while_thinking_cancels_generation(kb, monkeypatch, capsys):
    """考え中（最初の文が出る前）の割り込みでも、生成を止め、古い答えを読まない。止まったままにならない。"""
    cancelled = []
    session = FakeSession(["質問一", "質問二"])
    original_arm = session.arm

    def arm_then_barge():
        original_arm()
        if not cancelled and len(session.listened) == 1:
            threading.Timer(0.05, lambda: (session.speaker.new_turn(), session.barged.set())).start()
    session.arm = arm_then_barge
    _llm(monkeypatch, [["遅い答え。", "続き。"], ["二の答え。"]], delays={"遅い答え。": 0.3}, cancelled=cancelled)
    started = time.monotonic()
    assert _run(session) == 0
    assert time.monotonic() - started < 10
    spoken = [s for _, s in session.player.played]
    assert "遅い答え。" not in spoken and "二の答え。" in spoken
    assert cancelled == [["遅い答え。", "続き。"]]        # 生成は Cancelled で止まった


def test_quit_with_q_key(kb, monkeypatch, capsys):
    session = FakeSession(["質問一"])
    assert _run(session, keys=["q"]) == 0
    assert "終了します" in capsys.readouterr().out


def test_barge_in_can_be_disabled(monkeypatch):
    monkeypatch.setenv("JARVIS_VOICE_BARGE_IN", "0")
    assert duplex.barge_in_enabled() is False
    monkeypatch.delenv("JARVIS_VOICE_BARGE_IN")
    assert duplex.barge_in_enabled() is True


# ---------------------------------------------------------------- Codex の指摘への確認

def test_listen_keeps_working_after_old_audio_is_dropped():
    """2 分を超えて古い音を捨てても、読み取り位置がずれて待ち続けない（指摘 1）。"""
    s, _ = _session()
    for _ in range(50 * 120 + 10):
        s.push(_frame(270))
    assert s.base > 0
    result = {}

    def run():
        result["audio"] = s.listen()
    t = threading.Thread(target=run, daemon=True)
    t.start()
    time.sleep(0.05)
    for _ in range(30):
        s.push(_frame(3000))
    for _ in range(60):
        s.push(_frame(270))
    t.join(3)
    assert not t.is_alive() and result["audio"] is not None


def test_heard_excludes_sentences_still_in_the_output_delay():
    """出力の遅れの間に渡し終えた文は、割り込みで止めたら聞いた文にしない（指摘 3）。"""
    p, s = _player()
    p.output_latency = 0.5
    p.enqueue(1, "一文目。", np.full(300, 0.1, dtype=np.float32))
    s.pump()
    assert p.heard(1) == ["一文目。"]
    p.stop()                                              # まだスピーカーから出きっていない
    assert p.heard(1) == []
    p.output_latency = 0.0
    p.enqueue(2, "二文目。", np.full(300, 0.1, dtype=np.float32))
    s.pump()
    time.sleep(0.01)
    p.stop()
    assert p.heard(2) == ["二文目。"]


def test_short_backchannel_after_barge_in_is_recorded():
    """「うん」（約 0.24 秒）で割り込んだら、その相づちも録って質問として扱う（指摘 4、受け入れ D）。"""
    s, speaker = _session(ref=0.0)
    for _ in range(20):
        s.push(_frame(270))
    s.arm()
    for _ in range(12):
        s.push(_frame(1500))
    assert s.barged.is_set()
    for _ in range(60):
        s.push(_frame(270))
    audio = s.listen(from_barge_in=True)
    assert audio is not None and int((np.abs(audio * 32768) > 1000).sum()) >= 12 * duplex.FRAME


def test_E2_barge_in_before_the_first_character_stops_generation(kb, monkeypatch):
    """最初の文字が出る前（考え中）の割り込みでも、取り消しの確認で生成を止める（指摘 2）。"""
    cancelled = []
    session = FakeSession(["質問一", "質問二"])
    original_arm = session.arm

    def arm_then_barge():
        original_arm()
        if len(session.listened) == 1:
            threading.Timer(0.05, lambda: (session.speaker.new_turn(), session.barged.set())).start()
    session.arm = arm_then_barge
    _llm(monkeypatch, [["とても遅い答え。"], ["二の答え。"]], delays={"とても遅い答え。": 5.0}, cancelled=cancelled)
    started = time.monotonic()
    assert _run(session) == 0
    assert time.monotonic() - started < 3                  # 5 秒の生成を待たずに次へ
    assert cancelled == [["とても遅い答え。"]]
    assert "二の答え。" in [s for _, s in session.player.played]


def test_player_rejects_audio_of_interrupted_turns():
    """合成が終わる前に割り込まれた発言の音は、後から再生の列に入らない（2 回目の指摘 1）。"""
    p, s = _player()
    p.set_turn(1)
    p.set_turn(2)                                         # 割り込みで番号が進んだ
    p.enqueue(1, "古い文。", np.full(300, 0.1, dtype=np.float32))
    assert not p.speaking()
    p.enqueue(2, "新しい文。", np.full(300, 0.1, dtype=np.float32))
    assert p.speaking()


def test_player_is_speaking_until_the_output_delay_passes():
    """最後の音を出力に渡しても、出力の遅れの間は読み上げ中（2 回目の指摘 3）。"""
    p, s = _player()
    p.output_latency = 0.2
    p.enqueue(1, "一文目。", np.full(300, 0.1, dtype=np.float32))
    s.pump()
    assert p.speaking()
    time.sleep(0.25)
    assert not p.speaking()


def test_old_generation_cannot_feed_the_new_turn():
    """割り込みの後に次の発言が始まっても、古い生成の文は新しい発言として読まれない（3 回目の指摘 1・2）。"""
    player = FakePlayer()
    sp = duplex.DuplexSpeaker(player, synth=lambda s, c: np.zeros(10, dtype=np.float32))
    old = sp.new_turn()
    new = sp.new_turn()
    sp.feed("古い答え。", turn=old)
    sp.say("古い文。", turn=old)
    sp.feed("新しい答え。", turn=new)
    sp.wait()
    assert [s for _, s in player.played] == ["新しい答え。"]
    sp.close()


def test_history_keeps_only_what_was_spoken(kb, monkeypatch):
    """読み上げなかった内容（一覧の残り・出典）は、普通に終わった発言でも履歴に入れない（3 回目の指摘 3）。"""
    import orchestrator
    contexts = []
    original = orchestrator.Context
    monkeypatch.setattr(orchestrator, "Context", lambda **kw: contexts.append(original(**kw)) or contexts[-1])
    long_list = "1. 大洗_空調（oarai-minpaku、2026-10-07）\n   https://x\n2. 熊本県の補助（oarai-minpaku、2026-10-11）\n   https://y"
    monkeypatch.setattr(orchestrator, "handle", lambda text, ctx, ask=None: orchestrator.Outcome(
        "knowledge-search", "rule", {}, long_list, True, 0.1, [], None))
    session = FakeSession(["空調について知ってること"])
    assert _run(session) == 0
    history = [t.text for t in contexts[0].history]
    assert history[0] == "空調について知ってること"
    assert history[1] == "2 件見つかりました。1 件目は、大洗 空調。"          # 読んだ要約だけ
    assert "https://" not in "".join(history) and "熊本" not in "".join(history)
