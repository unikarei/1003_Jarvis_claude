"""JARVIS と声で会話する CLI（選んで使うモード。文字の対話は chat.py。FR-014、../docs/spec.md §3.4.9）。

既定はハンズフリーで割り込みあり: JARVIS が考えている間・話している間も聞き、Owner が話し始めたら読み上げを止めて
新しい質問に答える（ADR-0026・0028）。`--no-barge-in`（JARVIS_VOICE_BARGE_IN=0）で話している間は聞かない形、
`--push-to-talk` で Enter を押してから話す従来の形。音声は Mac の中で文字にし（faster-whisper）、録音は保存しない。
答えは画面に逐次表示しながら文ごとに読み上げる（macOS の say）。読み上げ中に Enter を押すと止める。
書き込みや課金のあるスキルの確認は、キーボードの y だけで受け付ける。終了: q と Enter、または「終了」と話す。
"""

import contextlib
import os
import queue
import sys
import threading
import time
from collections import deque

import numpy as np

import latency
import orchestrator
from ckc.models import Speaker as Who
from db import init_db
from run_task import record
from voice import duplex, tts
from voice import record as mic
from voice.speak import Speaker, speech_summary

VOICE = os.environ.get("JARVIS_VOICE", "Kyoko")
QUIT_WORDS = ("終了", "おしまい", "さようなら")
STREAMED_SKILLS = ("knowledge-answer",)   # 答えを逐次表示しながら読み上げるスキル


def output_device() -> str | None:
    """読み上げの出力先。JARVIS_VOICE_OUTPUT、なければ既定の入力機器と同じ名前の機器（ヘッドセット）。"""
    if os.environ.get("JARVIS_VOICE_OUTPUT"):
        return os.environ["JARVIS_VOICE_OUTPUT"]
    try:
        import sounddevice as sd
        device = sd.query_devices(kind="input")
        return device["name"] if device.get("max_output_channels", 0) > 0 else None
    except Exception:
        return None


# 無音や雑音を Whisper が文字にしてしまう決まり文句（話していないのに返事をしない）
NOISE_TEXTS = ("ご視聴ありがとうございました", "ご清聴ありがとうございました", "チャンネル登録", "おやすみなさい。",
               "Thank you for watching", "字幕")


def is_noise(text: str) -> bool:
    t = text.strip(" 　。、.!！?？")
    return not t or any(n.strip("。") in t for n in NOISE_TEXTS) and len(t) <= 20


def keyboard() -> "queue.Queue[str]":
    """キーボードの入力を別のスレッドで受ける（Enter で読み上げを止める・q で終了・書き込みの確認）。"""
    lines: queue.Queue[str] = queue.Queue()

    def read() -> None:
        for line in sys.stdin:
            lines.put(line.rstrip("\n"))
        lines.put("q")   # 入力が閉じたら終了

    threading.Thread(target=read, daemon=True).start()
    return lines


def _poll_keys(keys: "queue.Queue[str]", speaker: Speaker) -> bool:
    """届いているキーを処理する。q なら True（終了）。それ以外の Enter は読み上げを止める。"""
    while True:
        try:
            line = keys.get_nowait()
        except queue.Empty:
            return False
        if line.strip().lower() == "q":
            return True
        speaker.stop()


class SpeechEnd:
    """録音中に最後に声のあったブロックの時刻（話し終えた時刻）を残す。voice.record.segment に渡るブロックを
    包み、区切りの判定と同じしきい値（threshold × release_ratio）以上のブロックの時刻を記録する。
    無音の長さからの推定ではないので、最大の録音時間で区切られたときもずれない。
    時刻は、session（voice.duplex.Session）を渡せば各ブロックの最後のフレームの収録時刻（割り込みの後に、
    溜まった音を後から処理してもずれない）、渡さなければブロックを受け取った時刻（マイクから直接読む形）。"""

    def __init__(self, clock=time.monotonic):
        self.clock = clock
        self.last: float | None = None
        self._captured: float | None = None      # 今のブロックの収録時刻（session を見ているときだけ）

    def _timed_blocks(self, session):
        """Session._blocks と同じブロックを返し、各ブロックの最後のフレームの収録時刻を _captured に入れる。"""
        def blocks(start: int):
            index = start
            while True:
                with session._cond:
                    index = max(index, session.base)          # 捨てられた分は飛ばす
                    while session.base + len(session.frames) < index + 5:
                        session._cond.wait(timeout=1.0)
                        index = max(index, session.base)
                    rel = index - session.base
                    frames = session.frames[rel:rel + 5]
                index += 5
                self._captured = frames[-1][0]
                yield np.concatenate([f for _, f, _ in frames])
        return blocks

    @contextlib.contextmanager
    def watching(self, session=None):
        """この間の録音（mic.segment の呼び出し）を見る。listen はこのスレッドだけが呼ぶ。"""
        self.last, self._captured = None, None
        original = mic.segment
        timed_session = session is not None and all(hasattr(session, a) for a in ("_blocks", "_cond", "frames", "base"))

        def segment(blocks, threshold, *args, **kwargs):
            release = threshold * kwargs.get("release_ratio", mic.RELEASE_RATIO)

            def timed():
                for block in blocks:
                    if mic.rms(block) >= release:
                        self.last = self._captured if self._captured is not None else self.clock()
                    yield block
            return original(timed(), threshold, *args, **kwargs)
        mic.segment = segment
        own = vars(session).get("_blocks") if timed_session else None   # インスタンスに置かれていた元のもの
        if timed_session:
            session._blocks = self._timed_blocks(session)
        try:
            yield self
        finally:
            mic.segment = original
            if timed_session:
                if own is None:
                    del session._blocks                       # 差し替えを外す（クラスのものに戻る）
                else:
                    session._blocks = own


def start_turn(spoke: float | None, segmented: float, transcribed: float) -> "latency.Turn":
    """声の発言の遅延の記録を始める（話し終えた時点 spoke から。T-1405）。話し終えた時刻が分からない
    （録音を差し替えたなど）ときは区切りの時点から始め、segment は記録しない。"""
    turn = latency.Turn("voice", at=segmented if spoke is None else spoke)
    if spoke is not None:
        turn.mark("segment", at=segmented)
    turn.mark("stt", at=transcribed)
    return turn


class _StartWatch(deque):
    """Player の再生待ちの列。出力のコールバックが音を取り出した（最初のフレームを出力に渡した）時点を知らせる。"""

    def __init__(self, items, on_start):
        super().__init__(items)
        self.on_start = on_start

    def popleft(self):
        item = super().popleft()
        self.on_start(item[0])
        return item


def track_first_audio(speaker, current) -> None:
    """最初の声の再生開始（Player の出力のコールバックが文の音を出力に渡し始めた時点）を、current() が返す
    今の発言の記録に入れる。再生待ちの列に入れた時点（enqueue）ではない。待ちの列のない Player では記録しない。"""
    player = getattr(speaker, "player", None)
    pending = getattr(player, "_queue", None)
    if not isinstance(pending, deque):
        return

    def started(turn) -> None:
        lat = current()
        if lat is not None and getattr(player, "turn", None) in (None, turn):   # 捨てられた古い番号の音は数えない
            lat.mark("first_audio")
    player._queue = _StartWatch(pending, started)


class Cancelled(Exception):
    """割り込まれた発言の答えの生成を止める（逐次表示の途中で投げ、claude -p を止めさせる）。"""


INTERRUPTED_MARK = "（ここで Owner が割り込み）"
CONFIRM_SPOKEN = "書き込みがあります。キーボードで確認してください。"
OBSIDIAN_SKILL = orchestrator.OBSIDIAN_SKILL
END_ANSWER_SECONDS = 60      # 会話の終わりの「知識にしますか」を待つ上限（T-1803）


def obsidian_busy(ctx, outcome, text: str | None = None) -> bool:
    """割り込まれた発言が Obsidian のノートの編集の途中か（T-1702）: 途中の依頼がある・処理中・結果がこのスキル。
    振り分けの前（active_skill がまだない）でも、このスキルは発言の文だけで最初に決まる（orchestrator.route）ので、
    発言の文で判定する。"""
    import obsidian_note
    if ctx.pending is not None or ctx.active_skill == OBSIDIAN_SKILL:
        return True
    if outcome is not None:
        return outcome.skill == OBSIDIAN_SKILL
    return ctx.active_skill is None and text is not None and obsidian_note.matches(text)


def run_barge_in(session, transcribe, keys: "queue.Queue[str]") -> int:
    """割り込みのある会話（ADR-0028）。状態: 聞き取り → 考え中 → 読み上げ（考え中・読み上げ中に Owner が話すと
    割り込み → 聞き取り）。発言ごとに番号（speaker.turn）を付け、古い番号の生成・合成・再生は捨てる。
    session は voice.duplex.Session と同じ形（テストで差し替える）。"""
    speaker = session.speaker
    init_db()
    state = {"turn": 0, "started": False, "asking": False, "status": "IDLE", "latency": None}
    track_first_audio(speaker, lambda: state["latency"])
    spoken = SpeechEnd()

    def stream_for(turn: int):
        """発言 turn 専用の逐次表示。番号は呼び出しの間ずっと固定し、読み上げにも同じ番号で渡す
        （割り込みの後に次の発言が始まっても、古い生成の文が新しい発言として読まれない）。"""
        def stream(chunk: str) -> None:
            with speaker._lock:               # 番号の確認・表示・読み上げへの投入を、割り込み（new_turn）と同期
                if turn != speaker.turn:
                    raise Cancelled()         # 割り込まれた。claude -p は llm 側で止まる
                if not state["started"]:
                    state["started"] = True
                    state["status"] = "SPEAKING"
                    print("\nJARVIS > ", end="", flush=True)
                print(chunk, end="", flush=True)
                speaker.feed(chunk, turn=turn)
        return stream

    def ask_for(turn: int):
        """発言 turn 専用の確認。割り込まれた（番号が進んだ）発言の確認は、何もせず「実行しない」にする。"""
        def ask(prompt: str, spoken: str | None = None, expect: str | None = None) -> str:
            # spoken は読み上げる確認の要点（なければ決まった文）。expect（2 段目の語）はキーボードでは使わない
            if turn != speaker.turn:          # 割り込まれた発言の確認は、何も触らずに「実行しない」
                return "n"
            # ロックの順番は「割り込みの判定 → 読み上げ」（マイクの側と同じ）。読み上げのロックを持ったまま
            # 割り込みの判定のロックを取らない（取り合いで止まらないように）
            session.disarm(turn=turn)         # 確認を待つ間は割り込みを受けない（この発言に arm しているときだけ）
            with speaker._lock:
                if turn != speaker.turn:
                    return "n"
                state["asking"] = True
                speaker.stop()
                speaker.say(spoken or CONFIRM_SPOKEN, turn=turn)
                print(prompt, end="", flush=True)
            try:
                return keys.get()
            finally:
                state["asking"] = False
                if turn == speaker.turn:
                    session.arm()             # 確認の後の生成・読み上げでも割り込めるように
        return ask

    ctx = orchestrator.Context(voice=True, channel="voice")
    orchestrator.prewarm_session(ctx)
    print("JARVIS と声で会話します。そのまま話しかけてください。JARVIS が話している途中でも、話し始めれば止まります。"
          "（「終了」と話すか q と Enter で終了）")
    info = session.calibrate("ジャービスです。どうぞ。")
    print(f"（スピーカーからマイクへの回り込みを測りました: {info}）")
    from_barge, prompt_shown = False, False
    while True:
        if _poll_keys(keys, speaker):
            break
        if not from_barge and not prompt_shown:
            print("\n（聞いています…）", flush=True)
            prompt_shown = True
        state["status"] = "LISTENING"
        try:
            with spoken.watching(session):             # 話し終えた時刻はフレームの収録時刻から
                audio = session.listen(from_barge_in=from_barge)
            segmented = time.monotonic()
            from_barge = False
            if audio is None:
                continue
            text = transcribe(audio).strip()
            transcribed = time.monotonic()
        except KeyboardInterrupt:
            break
        if is_noise(text):
            continue
        prompt_shown = False
        print(f"あなた（声）> {text}")
        if any(w in text for w in QUIT_WORDS) and len(text) <= 10:
            break

        turn = speaker.new_turn()
        lat = start_turn(spoken.last, segmented, transcribed)            # 段階ごとの時間（本文は記録しない、T-1405）
        state.update(turn=turn, started=False, status="THINKING", latency=lat)
        ctx.stream = stream_for(turn)                       # この発言の生成だけが使う（呼び出しの時に固定される）
        ctx.cancel = lambda t=turn: t != speaker.turn
        result: dict = {}

        def work(a=ask_for(turn), c=ctx, r=lat, said=text):
            latency.use(r)                                  # handle を呼ぶスレッドで今の記録にする
            result.update(outcome=orchestrator.handle(said, c, ask=a))
        worker = threading.Thread(target=work, daemon=True)
        session.arm()
        worker.start()
        finished, interrupted = False, False
        while True:
            if session.barged.is_set():
                interrupted = True
                break
            if not finished and not worker.is_alive():
                finished = True
                _finish_answer(result["outcome"], state, ctx, speaker, turn)
            if finished and not speaker.speaking():
                break
            if not state["asking"] and _poll_keys(keys, speaker):
                session.disarm()
                speaker.stop()
                worker.join(timeout=5)
                state["latency"] = None
                latency.finish(lat, ok=False)                 # 答えの途中で終了した
                return _end(speaker, session, ctx, keys)
            time.sleep(0.02)
        session.disarm()
        if not interrupted and session.barged.is_set():
            from_barge = True                 # 答えを読み終えた直後の割り込み: 履歴は普通に確定し、録音は話し始めから

        if interrupted:
            state["status"] = "INTERRUPTED"
            heard = speaker.heard(turn)
            speaker.new_turn()                # 残りの合成・再生を捨てる（生成は stream で止まる）
            ev = session.events[-1] if getattr(session, "events", None) else None
            timing = (f"、話し始めから停止まで約 {ev['detect_ms'] + ev['stop_call_ms'] + ev['output_ms']:.0f} ms"
                      f"（検出 {ev['detect_ms']}・停止の指示 {ev['stop_call_ms']}・出力 {ev['output_ms']}）") if ev else ""
            print(f"\n（割り込み: 読み上げを止めました{timing}）")
            if obsidian_busy(ctx, result.get("outcome"), text):
                # Obsidian のノートの編集の途中: 処理を待ってから次の発言を扱う（文脈を作り直さず、途中の依頼を
                # 次の発言に使う）。確認の後の書き込みは取り消さず、結果の文を出して読み上げる。履歴には入れない
                worker.join()
                state["latency"] = None
                latency.finish(lat, ok=False)
                out = result.get("outcome")
                if out is not None:
                    _record(text, out)
                    if not finished:
                        print(f"\nJARVIS > {out.text}\n")
                        speaker.say(speech_summary(out.skill, out.text))
                        while speaker.speaking():
                            time.sleep(0.02)
                from_barge = True
                continue
            worker.join(timeout=10)            # 生成は取り消しの確認（0.1 秒ごと）で止まる
            state["latency"] = None
            latency.finish(lat, ok=False)      # 割り込まれた発言
            if worker.is_alive():              # 止まらない処理（逐次表示のないスキル）は、文脈を分けて切り離す
                print("[前の処理は裏で終わるまで続きます。結果は使いません]")
                ctx = orchestrator.renew_context(ctx, voice=True)
            if "outcome" in result:
                _record(text, result["outcome"])
                if getattr(result["outcome"], "private", False):
                    from_barge = True
                    continue
            orchestrator.add_turn(ctx, Who.OWNER, text)
            if heard:                         # 聞いた文だけを履歴に残す
                orchestrator.add_turn(ctx, Who.AI, "".join(heard) + INTERRUPTED_MARK)
            from_barge = True
            continue

        outcome = result["outcome"]
        state["latency"] = None
        latency.finish(lat)                             # 読み上げの後（最初の声の時刻が入ってから）保存する
        _record(text, outcome)
        if not getattr(outcome, "private", False) and not (outcome.skill == "knowledge-capture" and outcome.method == "rule"):
            orchestrator.add_turn(ctx, Who.OWNER, text)
            heard = "".join(speaker.heard(turn))         # 読み上げた文だけ（表示だけの出典・一覧の残りは入れない）
            if outcome.ok and outcome.error is None and heard:
                orchestrator.add_turn(ctx, Who.AI, heard)
        state["status"] = "IDLE"
    return _end(speaker, session, ctx, keys)


def _finish_answer(outcome, state: dict, ctx, speaker, turn: int | None = None) -> None:
    """答えの残り（出典など）を表示し、逐次表示しなかった答えは短い要約を読む。turn（その発言の番号）を渡すと、
    割り込みで番号が進んでいたら読まない。"""
    extra = {"turn": turn} if turn is not None else {}
    with speaker._lock if hasattr(speaker, "_lock") else _nullcontext():   # 割り込みと同期して表示・投入する
        if turn is not None and turn != getattr(speaker, "turn", turn):
            return                            # 割り込まれた発言の残りは表示も読み上げもしない
        _finish_answer_locked(outcome, state, ctx, speaker, extra)


def _nullcontext():
    import contextlib
    return contextlib.nullcontext()


def _finish_answer_locked(outcome, state: dict, ctx, speaker, extra: dict) -> None:
    if state["started"] and ctx.shown and outcome.text.startswith(ctx.shown):
        speak_rest(outcome, True, ctx, speaker, extra)
        rest = outcome.text[len(ctx.shown):].strip()
        print(("\n\n" + rest if rest else "") + "\n")
        return
    if state["started"]:
        print()
    print(f"\nJARVIS > {outcome.text}\n")
    if speak_rest(outcome, state["started"], ctx, speaker, extra):
        state["status"] = "SPEAKING"


def speak_rest(outcome, started: bool, ctx, speaker, extra: dict) -> bool:
    """答えの残りを読む（表示はしない。声の会話とブラウザでの声の会話で同じ規則、T-1502）。逐次表示した答えは
    区切りの途中の残りを読み、逐次表示しなかった答え（一覧など）は短い要約を読む。要約を読んだら True。"""
    if started:
        speaker.flush(**extra)
        if ctx.shown and outcome.text.startswith(ctx.shown):
            return False                      # 出典は表示だけ
    if outcome.skill not in STREAMED_SKILLS or not started:
        speaker.say(speech_summary(outcome.skill, outcome.text), **extra)
        return True
    return False


def _record(text: str, outcome) -> None:
    try:
        record(text, outcome)
    except Exception as e:  # 記録の失敗で会話を止めない
        print(f"[記録できませんでした] {type(e).__name__}: {e}")


def offer_capture(ctx, keys, speaker, turn=None) -> None:
    """声の会話の終わりに、知識にするかをキーボードで聞く（T-1803）。答えと候補ごとの確認はキーボードの列から読む
    （キーボードのスレッドが標準入力を読んでいるため、input を使わない）。"""
    import chat_actions
    if ctx is None or keys is None or not chat_actions.worth_capturing(ctx.history):
        return

    def ask(prompt: str) -> str:
        print(prompt, end="", flush=True)
        try:
            return keys.get(timeout=END_ANSWER_SECONDS)   # 答えがなければ「いいえ」で終える
        except queue.Empty:
            print()
            return ""
    speaker.say(chat_actions.END_SPOKEN, **({"turn": turn} if turn is not None else {}))
    chat_actions.offer_capture_at_end(ctx.history, ask)


def _end(speaker, session, ctx=None, keys=None) -> int:
    session.disarm()
    speaker.new_turn()
    offer_capture(ctx, keys, speaker)
    if ctx is not None:
        orchestrator.end_session(ctx)
    speaker.say("終了します。")
    speaker.wait()
    speaker.close()
    session.close()
    print("終了します。")
    return 0


def build_speaker(settings: "tts.VoiceSettings | None" = None, player=None, recover=None) -> "duplex.DuplexSpeaker":
    """保存した音声の設定（python -m acc.voice_settings で選ぶ）で読み上げる。どの起動の形でも使う（ADR-0029）。
    recover を渡すと、会話の途中でエンジンが止まったときに起動し直して選んだ音声に戻す。"""
    settings = settings or tts.load_settings()
    print(f"（音声: {tts.engine(settings.engine).label}・{settings.speaker}・話速 {settings.rate}）")
    player = player or duplex.Player(device=output_device())
    return duplex.DuplexSpeaker(player, voice=VOICE, synth=tts.make_synth(settings, recover=recover))


def _recover_for(launcher):
    """会話の途中でエンジンが止まったときに起動し直す関数（選んだ話者を先に読み込む）。管理しないなら None。"""
    if launcher is None:
        return None
    speaker = tts.load_settings().speaker
    return lambda: launcher.recover(speaker=speaker)


def start_engine(launcher) -> None:
    """選んだ音声のエンジンを起動し始める（待たない。音声認識のモデルの読み込みと並べる）。"""
    if launcher is not None:
        launcher.start()
        if launcher.status:
            print(f"（音声: {launcher.status}）")


def wait_engine(launcher) -> None:
    """エンジンが使えるようになるまで待つ。使えなければ build_speaker が say（Kyoko）にする。"""
    if launcher is not None and launcher.owned:
        launcher.wait_ready()
        print(f"（音声: {launcher.status}）")
    if launcher is not None:
        from voice import engine_process
        engine_process.preload(launcher, tts.load_settings())   # 最初の文の合成を速くする


def main(listen=None, transcribe=None, speaker: Speaker | None = None, push_to_talk: bool = False,
         keys: "queue.Queue[str] | None" = None, barge_in: bool | None = None, launcher=None) -> int:
    """既定はハンズフリー（JARVIS が話し終えたら自動で聞く、ADR-0026）。push_to_talk で Enter を押してから話す。
    listen・transcribe・speaker・keys・launcher はテストで差し替える。
    選んだ音声のエンジン（VOICEVOX・AivisSpeech）が止まっていれば、この会話の間だけ起動し、終わりに止める
    （この会話が起動したものだけ。voice/engine_process.py）。"""
    if listen is None and not mic.has_input_device():
        print("マイクが見つかりません。ヘッドセットなどをつないでから起動してください（文字の対話は chat.py）。")
        return 1
    if launcher is None and listen is None:
        from voice import engine_process
        launcher = engine_process.for_settings(tts.load_settings())
    restore = _stop_on_signals(launcher)
    try:
        start_engine(launcher)
        return _main(listen, transcribe, speaker, push_to_talk, keys, barge_in, launcher)
    finally:
        try:
            if launcher is not None:
                launcher.stop()              # この会話が起動したものだけを止める
        finally:
            restore()


def use_voice_model() -> None:
    """声の会話のプロセスでは、claude -p を速いモデル（JARVIS_VOICE_CLAUDE_MODEL、既定 Sonnet）で起動する
    （2026-11-03 Owner 指示。ナレッジの回答の最初の文字を速くする）。待機させる claude を起動する前に呼ぶ。"""
    import llm
    from config import VOICE_CLAUDE_MODEL
    llm.use_claude_model(VOICE_CLAUDE_MODEL)


def _stop_on_signals(launcher):
    from voice import engine_process
    return engine_process.stop_on_signals(launcher)


def _main(listen, transcribe, speaker, push_to_talk, keys, barge_in, launcher) -> int:
    use_voice_model()
    # 使う LLM を裏で読み込み、前の会話を要約しておく（最初の答えを速くする、T-1405。音声認識のモデルより先に始める）。
    # 始めたばかりの会話はまだ記録がないので、要約の対象から除く ID は要らない（T-2101）
    orchestrator.start_warm_up()
    if listen is None:
        print("音声認識のモデルを読み込んでいます（初回はダウンロードに数分かかります）…")
        from speech_to_text import load_model, transcribe_array
        load_model()
    wait_engine(launcher)                     # 読み込みの間に起動したエンジンを待つ（選んだ音声を使えるように）
    if listen is None:
        if barge_in is None:
            barge_in = duplex.barge_in_enabled()
        if barge_in and not push_to_talk:     # 割り込みのある会話（ADR-0028）
            speaker = build_speaker(recover=_recover_for(launcher))
            session = duplex.Session(speaker.player, speaker)
            return run_barge_in(session, transcribe_array, keys if keys is not None else keyboard())
        listen, transcribe = mic.record_utterance, transcribe_array
    speaker = speaker or build_speaker(recover=_recover_for(launcher))   # どの形でも選んだ音声で読む
    keys = keys if keys is not None else keyboard()
    init_db()

    def stream(chunk: str) -> None:
        if not state["started"]:
            state["started"] = True
            print("\nJARVIS > ", end="", flush=True)
        print(chunk, end="", flush=True)
        speaker.feed(chunk)

    def ask(prompt: str, spoken: str | None = None, expect: str | None = None) -> str:
        speaker.stop()
        speaker.say(spoken or CONFIRM_SPOKEN)
        print(prompt, end="", flush=True)
        return keys.get()

    def finish_speaking() -> bool:
        """読み上げが終わるまで待つ（その間は聞かない）。q なら True。"""
        while speaker.speaking():
            if _poll_keys(keys, speaker):
                return True
            time.sleep(0.05)
        return _poll_keys(keys, speaker)

    def finish_latency() -> None:
        """前の発言の遅延の記録を保存する（読み上げが終わり、最初の声の時刻が入ってから）。"""
        if state["latency"] is not None:
            lat, state["latency"] = state["latency"], None
            latency.finish(lat)

    state = {"started": False, "latency": None}
    track_first_audio(speaker, lambda: state["latency"])
    spoken = SpeechEnd()
    ctx = orchestrator.Context(stream=stream, voice=True, channel="voice")
    orchestrator.prewarm_session(ctx)
    if push_to_talk:
        print("JARVIS と声で会話します。Enter を押してから話してください（q と Enter で終了）。")
    else:
        print("JARVIS と声で会話します。そのまま話しかけてください。"
              "（読み上げ中に Enter で止める／「終了」と話すか q と Enter で終了）")
    speaker.say("ジャービスです。どうぞ。")
    prompt_shown = False
    while True:
        try:
            if push_to_talk:             # 従来の形: Enter で読み上げを止めて話す
                print("\n[Enter で話す / q で終了] ", end="", flush=True)
                if keys.get().strip().lower() == "q":
                    break
                finish_latency()
                speaker.stop()
                print("（聞いています…）", flush=True)
            elif finish_speaking():
                break
            else:
                finish_latency()
                if not prompt_shown:
                    print("\n（聞いています…）", flush=True)
                    prompt_shown = True
            with spoken.watching():
                audio = listen()
            segmented = time.monotonic()
            if audio is None:
                if push_to_talk:
                    print("聞き取れませんでした。")
                continue            # ハンズフリーでは話し始めるまで聞き続ける
            text = transcribe(audio).strip()
            transcribed = time.monotonic()
        except KeyboardInterrupt:
            break
        if is_noise(text):
            if push_to_talk:
                print("聞き取れませんでした。")
            continue
        prompt_shown = False
        print(f"あなた（声）> {text}")
        if any(w in text for w in QUIT_WORDS) and len(text) <= 10:
            break

        state["started"] = False
        state["latency"] = start_turn(spoken.last, segmented, transcribed)   # 段階ごとの時間（本文は記録しない、T-1405）
        latency.use(state["latency"])
        outcome = orchestrator.handle(text, ctx, ask=ask)
        latency.use(None)
        if state["started"] and ctx.shown and outcome.text.startswith(ctx.shown):
            speaker.flush()
            rest = outcome.text[len(ctx.shown):].strip()      # 出典は表示だけ
            print(("\n\n" + rest if rest else "") + "\n")
        else:
            if state["started"]:
                speaker.flush()
                print()
            print(f"\nJARVIS > {outcome.text}\n")
            if outcome.skill not in STREAMED_SKILLS or not state["started"]:
                speaker.say(speech_summary(outcome.skill, outcome.text))   # 一覧やレポートは短い要約だけ読む
        try:
            record(text, outcome)
        except Exception as e:  # 記録の失敗で会話を止めない
            print(f"[記録できませんでした] {type(e).__name__}: {e}")
        if not getattr(outcome, "private", False) and not (outcome.skill == "knowledge-capture" and outcome.method == "rule"):
            orchestrator.add_turn(ctx, Who.OWNER, text)
            if outcome.ok and outcome.error is None:
                orchestrator.add_turn(ctx, Who.AI, outcome.text)

    finish_latency()
    speaker.stop()
    orchestrator.end_session(ctx)
    offer_capture(ctx, keys, speaker)          # 会話の終わりに知識にするか聞く（T-1803。y のときだけ）
    speaker.say("終了します。")
    speaker.wait()
    speaker.close()
    print("終了します。")
    return 0


if __name__ == "__main__":
    sys.stdin.reconfigure(encoding="utf-8")
    sys.stdout.reconfigure(encoding="utf-8")
    sys.exit(main(push_to_talk="--push-to-talk" in sys.argv[1:],
                  barge_in=False if "--no-barge-in" in sys.argv[1:] else None))
