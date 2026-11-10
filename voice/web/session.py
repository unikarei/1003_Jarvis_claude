"""ブラウザでの声の会話の 1 つ分の処理（T-1501〜T-1504）。

ブラウザが区切った発話 1 回分の WAV を受け、16 kHz にして文字にし、orchestrator.handle で答える。
回答は voice.duplex.DuplexSpeaker で文ごとに合成し、voice.web.player.BrowserPlayer がブラウザに渡す（再生は
ブラウザ）。履歴には、ブラウザが再生し終えた文だけを残す（voice_chat.py と同じ規則）。
割り込み（T-1503、ADR-0028）: 考えている間・話している間に /api/interrupt か次の発話が届いたら、発言の番号を進めて
古い合成・保持した音を捨て、生成を取り消し（ctx.cancel）、その発言の履歴を voice_chat.run_barge_in と同じ形で
確定して、次の発話を受け付ける。割り込まれた発言のイベントはその後に送らない（turn_done interrupted の 1 回だけ）。
途中の経過はイベント（dict）として emit に渡す（送り方は server.py が決める）。音声はメモリ上だけで扱い、
ファイル・DB・ログに残さない。sounddevice は import しない（Mac の音声の機器を使わない）。
画面での承認（T-1504、BV-09）: 確認の要るスキルは、発言ごとの承認の要求（使い捨ての ID・発言の番号・期限 60 秒）を
イベント approval で送り、画面の「実行する」（/api/approve）が期限内に届いたときだけ実行する。声の発言は承認にならない
（話せば新しい発言として割り込みになり、待っている要求は取り消される）。端末の入力を待つスキルは実行しない
（Context.terminal=False）。
"""

import hmac
import io
import secrets
import sys
import threading
import time
import wave

import numpy as np

import latency
import orchestrator
import voice_chat
from ckc.models import Speaker as Who, Turn
from voice import tts
from voice.duplex import DuplexSpeaker
from voice.web.player import BrowserPlayer
from voice_chat import QUIT_WORDS, is_noise   # voice_chat の import では音声の機器を開かない（sounddevice は関数の中だけ）

MAX_AUDIO_SECONDS = 20.5      # 発話 1 回分の長さの上限（ページの最長 20 秒＋前の 0.3 秒＋余裕）
MIN_RATE, MAX_RATE = 8000, 96000
POLL_SECONDS = 0.02           # 読み上げの終わりを確かめる間隔
CALIBRATION_TEXT = "ジャービスです。どうぞ。"   # 回り込みの測定のあいさつ（voice_chat.run_barge_in と同じ）
CALIBRATION_TURN = 0          # あいさつの番号（発言は 1 から。履歴・遅延の記録に入れない）
CALIBRATE_TIMEOUT = 60.0      # あいさつの合成を待つ上限（最初の合成はエンジンの起動を待つ）
APPROVAL_SECONDS = 60         # 承認の要求の期限（T-1504）
APPROVAL_SPOKEN = "書き込みがあります。画面で確認してください。"
DECISIONS = {"run": "run", "decline": "declined", "later": "later"}   # /api/approve の decision → approval_done の result
MAX_TYPED = 16                # 2 段目の確認で入力する語の長さの上限（T-1702）
OBSIDIAN_SKILL = orchestrator.OBSIDIAN_SKILL


class AudioError(Exception):
    """受け付けない音声。status は HTTP の状態コード（400 か 413）。"""

    def __init__(self, status: int, message: str):
        super().__init__(message)
        self.status = status


class Busy(Exception):
    """受け付けられない（会話が終わっている・測定は済んでいる）。"""


class _TurnState:
    """発言 1 回分の状態（割り込みのときに履歴・遅延の記録を確定するために使う）。"""

    __slots__ = ("turn", "lat", "text", "outcome", "record", "done", "ctx")

    def __init__(self, turn: int, lat: "latency.Turn"):
        self.turn, self.lat = turn, lat
        self.text: str | None = None      # 認識した Owner の発言（雑音・認識の前なら None）
        self.outcome = None               # handle の結果（まだなら None）
        self.record = True                # 遅延を記録するか（雑音・「終了」は答えていないので記録しない）
        self.done = threading.Event()     # この発言の処理（_process）が終わった（T-1702）
        self.ctx = None                   # この発言が使った文脈（割り込みの判定に使う、T-1702）


class _Approval:
    """承認の要求 1 つ（T-1504）。answer は Owner の答え（run / declined）、result は確定した結果（approval_done の
    result）。どちらも 1 回だけ決まる。"""

    __slots__ = ("id", "turn", "deadline", "answer", "result", "event", "expect", "review")

    def __init__(self, turn: int, deadline: float, expect: str | None = None, review: bool = False):
        self.id = secrets.token_urlsafe(16)
        self.turn, self.deadline = turn, deadline
        self.expect = expect              # 2 段目の確認で求める語（サーバーが決めて照合する、T-1702）
        self.review = review              # 知識の候補の確認（保存する・保存しない・あとで、T-1802）
        self.answer: str | None = None
        self.result: str | None = None
        self.event = threading.Event()


def confirm_prompt(prompt: str) -> str:
    """orchestrator.confirm_text の文から、末尾のキーボード向けの「[y/N] >」を除く。"""
    text = prompt.rstrip()
    if text.endswith("[y/N] >"):
        text = text[: -len("[y/N] >")].rstrip()
    return text


def decode_wav(data: bytes, rate: int) -> np.ndarray:
    """PCM 16 bit・モノラルの WAV を float32（-1〜1）の配列にする。レートは rate と一致すること。"""
    if not MIN_RATE <= rate <= MAX_RATE:
        raise AudioError(400, "サンプルレートが範囲の外です")
    try:
        with wave.open(io.BytesIO(data), "rb") as w:
            if w.getnchannels() != 1 or w.getsampwidth() != 2 or w.getcomptype() != "NONE":
                raise AudioError(400, "PCM 16 bit・モノラルの WAV ではありません")
            if w.getframerate() != rate:
                raise AudioError(400, "WAV のサンプルレートが rate と違います")
            if w.getnframes() > MAX_AUDIO_SECONDS * rate:
                raise AudioError(413, "発話が長すぎます")
            frames = w.readframes(w.getnframes())
    except (wave.Error, EOFError) as e:
        raise AudioError(400, f"WAV を読めません: {e}") from None
    samples = np.frombuffer(frames[: len(frames) // 2 * 2], dtype="<i2")
    if not len(samples):
        raise AudioError(400, "音声が空です")
    if len(samples) > MAX_AUDIO_SECONDS * rate:
        raise AudioError(413, "発話が長すぎます")
    return samples.astype(np.float32) / 32768.0


def _transcribe(audio: np.ndarray) -> str:
    import speech_to_text   # faster-whisper の読み込みは使うときだけ
    return speech_to_text.transcribe_array(audio)


def _record(text: str, outcome) -> None:
    from run_task import record
    record(text, outcome)


def _capture_pending(history: list) -> None:
    """会話の終わりに、知識の候補を作って保存待ちにする（T-1803。保存はしない）。"""
    import chat_actions
    chat_actions.capture_candidates(history)


def _pending_count() -> int:
    import chat_actions
    return chat_actions.pending_count()


def default_launcher(settings: "tts.VoiceSettings | None" = None):
    """選んだ音声のエンジン（VOICEVOX・AivisSpeech）の起動と停止。管理しないエンジン（say）なら None。"""
    from voice import engine_process
    return engine_process.for_settings(settings or tts.load_settings())


class _FirstSynth:
    """最初の合成の前にだけエンジンを待ち、合成の関数を作る（会話の開始を遅らせない）。
    synth を渡さなければ voice_chat.build_speaker と同じ tts.make_synth（有料のエンジンには切り替えない）。
    取り消し・終了ではないのに 1 文を合成できなかったら failed() を呼ぶ（その文は読まない）。"""

    def __init__(self, launcher, synth, notify, closed, failed=None):
        self.launcher, self.synth, self.notify, self.closed, self.failed = launcher, synth, notify, closed, failed
        self._lock = threading.Lock()
        self._ready = False

    def __call__(self, sentence: str, cancel: threading.Event):
        with self._lock:
            if not self._ready:
                self._ready = True
                if not self.closed():
                    voice_chat.wait_engine(self.launcher)
                if self.synth is None:
                    settings = tts.load_settings()
                    self.synth = tts.make_synth(settings, notify=self.notify,
                                                recover=voice_chat._recover_for(self.launcher))
        if self.closed() or (cancel is not None and cancel.is_set()):
            return None
        try:
            audio = self.synth(sentence, cancel)
        except Exception:  # noqa: BLE001  合成の失敗で読み上げのスレッドを止めない（DuplexSpeaker と同じ）
            audio = None
        if audio is None and self.failed is not None and not self.closed() \
                and not (cancel is not None and cancel.is_set()):
            self.failed()
        return audio


class WebSession:
    """会話 1 つ分。認識（transcribe）・回答（handle）・イベントの送り先（emit）・記録（record）・合成（synth:
    (文, 取り消し) → 16 kHz の float32）・エンジンの起動と停止（launcher: voice.engine_process.EngineProcess と同じ形）・
    再生の待つ期限・承認の期限の時計（clock）を差し替えられる。launcher を渡さなければエンジンを起動・停止しない。"""

    def __init__(self, transcribe=None, handle=None, emit=None, record=None, synth=None, launcher=None,
                 clock=time.monotonic, capture_on_end=None, pending_count=None):
        self.id = secrets.token_urlsafe(12)
        self.transcribe = transcribe or _transcribe
        self.handle = handle or orchestrator.handle
        self.emit = emit if emit is not None else (lambda event: None)
        self.record = record or _record
        self.launcher = launcher
        self.ctx = orchestrator.Context(voice=True, terminal=False)
        self.clock = clock
        self.closed = False
        self.turn = 0
        self._seq = 0
        self._lock = threading.Lock()
        self._active: _TurnState | None = None     # 処理している発言（考えている間・話している間）
        self._cut: set[int] = set()                # 割り込まれた発言の番号（その後のイベントは送らない）
        self._ran: set[int] = set()                # 「実行する」で実行を始めた発言の番号（割り込まれても結果を画面に出す）
        self._calibrated = False
        self._calibration_audio = threading.Event()
        self._tts_failed: int | None = None        # 合成の失敗を知らせた発言の番号（発言ごとに 1 回だけ知らせる）
        self._latency: tuple[int, latency.Turn] | None = None   # （発言の番号, その発言の遅延の記録）
        self._approval: _Approval | None = None    # 答えを待っている承認の要求（T-1504、同時に 1 つだけ）
        # 割り込まれた、Obsidian のノートの編集の途中の発言（T-1702）。次の発言は、この処理の終わりを待つ
        self._after: _TurnState | None = None
        self.player = BrowserPlayer(emit=self._player_event, clock=clock)
        self.speaker = DuplexSpeaker(self.player, voice=voice_chat.VOICE,
                                     synth=_FirstSynth(launcher, synth, self._notice, lambda: self.closed,
                                                       self._synth_failed))
        # 会話の終わりの知識化（T-1803）: 終わりに候補を保存待ちにする関数と、始めに保存待ちの件数を数える関数
        self.capture_on_end = capture_on_end if capture_on_end is not None else _capture_pending
        self._send("ready")
        self._notice_pending(pending_count if pending_count is not None else _pending_count)
        try:
            voice_chat.start_engine(launcher)      # 起動し始めるだけ（待たない）
        except Exception as e:  # noqa: BLE001  起動できなくても say で読む
            self._notice(f"[音声] エンジンを起動できません（{type(e).__name__}）。macOS の say（Kyoko）で読み上げます。")

    # ------------------------------------------------------------ イベント

    def _send(self, type_: str, turn: int | None = None, **data) -> None:
        with self._lock:
            self._send_locked(type_, turn, **data)

    def _send_locked(self, type_: str, turn: int | None = None, **data) -> None:
        """self._lock を持った状態で送る。割り込まれた発言のイベントは送らない。"""
        if turn is not None and turn in self._cut:
            return
        self._emit_locked(type_, turn, **data)

    def _emit_locked(self, type_: str, turn: int | None = None, **data) -> None:
        if self.closed and type_ != "ended":
            return                            # 終えた後の古い発言の結果は送らない
        self._seq += 1
        event = {"session": self.id, "seq": self._seq, "type": type_}
        if turn is not None:
            event["turn"] = turn
        event.update(data)
        self.emit(event)

    def _notice(self, message: str) -> None:
        self._send("notice", message=message)

    def _synth_failed(self) -> None:
        """1 文を合成できなかった（say も使えない）。文字の回答は続け、発言ごとに 1 回だけ知らせる。"""
        turn = self.speaker.turn
        with self._lock:
            if self._tts_failed == turn:
                return
            self._tts_failed = turn
            self._send_locked("error", turn, kind="tts", message="読み上げの音を作れませんでした（文字の回答は続けます）")

    def _latency_for(self, turn: int) -> "latency.Turn | None":
        current = self._latency
        return current[1] if current is not None and current[0] == turn else None

    def _player_event(self, type_: str, turn: int, **data) -> None:
        if type_ == "audio" and turn == CALIBRATION_TURN:
            self._send(type_, turn, calibration=True, **data)    # 回り込みの測定のあいさつ
            self._calibration_audio.set()
            return
        if type_ == "audio":
            lat = self._latency_for(turn)
            if lat is not None:
                lat.mark("audio_ready")       # 最初の文の音を保持した時刻（最初の 1 回だけ記録される）
        self._send(type_, turn, **data)

    # ------------------------------------------------------------ 操作

    def calibrate(self, timeout: float = CALIBRATE_TIMEOUT) -> bool:
        """回り込みの測定のあいさつを 1 回だけ合成し、イベント audio（calibration: true）で送る。履歴には入れない。
        送れたら True、合成できなかったら False（ページは回り込み 0 で続ける）。最初の発言の後・2 回目は Busy。"""
        with self._lock:
            if self.closed:
                raise Busy("会話は終わっています")
            if self._calibrated or self.turn != CALIBRATION_TURN:
                raise Busy("回り込みは測り終えています")
            self._calibrated = True
        with self.speaker._lock:              # 再生の知らせ（/api/played）をあいさつの番号で受けられるように
            if self.speaker.turn == CALIBRATION_TURN:
                self.player.set_turn(CALIBRATION_TURN)
        self.speaker.say(CALIBRATION_TEXT, turn=CALIBRATION_TURN)
        deadline = time.monotonic() + timeout
        while not self.closed and time.monotonic() < deadline:
            if self._calibration_audio.wait(POLL_SECONDS):
                return True
            if not self.speaker.synthesizing():
                break
        return self._calibration_audio.is_set()

    def submit(self, data: bytes, rate: int) -> int:
        """発話 1 回分の WAV を受け付け、発言の番号を返す。認識・回答・読み上げは別のスレッドで行う。
        前の発言を考えている間・話している間なら、その発言に割り込んでから受け付ける（409 にしない）。"""
        accepted = time.monotonic()
        try:
            audio = decode_wav(data, rate)
        except AudioError as e:
            self._send("error", kind="bad_audio", message=str(e))
            raise
        with self._lock:
            if self.closed:
                raise Busy("会話は終わっています")
            cut = self._interrupt_locked(self._active) if self._active is not None else None
            self.turn = self.speaker.new_turn()     # 読み上げの側と同じ番号（古い発言の音を捨てる）
            st = _TurnState(self.turn, latency.Turn("web", at=accepted))   # 発話を受け付けた時刻から
            self._active = st
            self._latency = (st.turn, st.lat)
        if cut is not None:
            self._finish_interrupted(cut)
        audio16 = tts.resample(audio, rate)
        threading.Thread(target=self._process, args=(st, audio16), name=f"voice-web-turn-{st.turn}",
                         daemon=True).start()
        return st.turn

    def interrupt(self, turn: int) -> str:
        """ブラウザが割り込んだ（/api/interrupt）。返り値は "ok" / "stale"（今処理している発言の番号でない・
        すでに割り込んだ・処理を終えた・終えた会話）。stale は何もしない（server.py が 409 にする）。"""
        with self._lock:
            if self.closed or turn != self.turn:
                return "stale"
            st = self._active
            if st is None or st.turn != turn:
                return "stale"
            self._interrupt_locked(st)
        self._finish_interrupted(st)
        return "ok"

    def audio(self, audio_id: str) -> bytes | None:
        """保持している文の WAV。終えた会話・保持していない ID なら None。"""
        if self.closed:
            return None
        return self.player.audio(audio_id)

    def played(self, turn: int, audio_id: str, event: str) -> str:
        """ブラウザの再生の知らせ（start / end / error）。返り値は "ok" / "stale" / "unknown"（server.py が状態コードにする）。"""
        if self.closed:
            return "stale"
        status, _ = self.player.played(turn, audio_id, event)
        if status != "ok":
            return status
        if event == "start":
            lat = self._latency_for(turn)
            if lat is not None:
                lat.mark("first_audio")       # ブラウザが最初に再生を始めた時刻（最初の 1 回だけ記録される）
        elif event == "error":
            self._send("error", turn, kind="playback", message="ブラウザで読み上げの音を再生できませんでした")
        return "ok"

    def _notice_pending(self, count) -> None:
        """保存待ちの知識の候補があれば、会話の始めに知らせる（T-1803）。数えられなくても会話は始める。"""
        try:
            n = count()
        except Exception:  # noqa: BLE001
            return
        if n:
            self._notice(f"保存待ちの知識の候補が {n} 件あります。「ナレッジ化して」と言うと、1 件ずつ確認できます。")

    def end(self, turn: int | None = None, capture: bool = False) -> None:
        """会話を終える。turn は「終了」と話した発言の番号（/api/end・切断では None）。
        生成を取り消し（番号を進める）、読み上げを止め、保持した音を消し、この会話が起動したエンジンを止める。
        capture=True（「終了」と話した・「会話を終える」を押した）で、知識にする価値のある会話なら、裏で候補を作って
        保存待ちにする（T-1803。Vault・jarvis.db の知識には書かない。切断・サーバーの停止では何もしない）。"""
        with self._lock:
            if self.closed:
                return
            self._cancel_approval_locked()    # 待っている承認の要求を取り消す（実行しない）
            self.closed = True
            history = list(self.ctx.history) if capture else []
        if history:
            self._capture_in_background(history)
        try:
            self.speaker.new_turn()           # 生成・合成を取り消す（ctx.cancel が True になる）
            self.speaker.close()              # 未再生の音を捨て、合成のスレッドを終える（BrowserPlayer の音も消える）
        finally:
            self._send("ended", turn)
            if self.launcher is not None:
                try:
                    self.launcher.stop()      # この会話が起動したものだけを止める（EngineProcess の規則）
                except Exception as e:  # noqa: BLE001
                    print(f"[音声] エンジンを止められませんでした（{type(e).__name__}）", file=sys.stderr)

    def approve(self, turn: int, approval_id: str, decision: str, text: str | None = None) -> str:
        """画面の「実行する」（decision="run"）・「実行しない」（"decline"）。返り値は "ok" / "stale"。
        待っている要求と ID・発言の番号が一致し、期限内で、まだ答えていないときだけ受け付ける。stale は何もしない
        （server.py が 409 にする）。実行するかは要求を待っている側（_ask）が、割り込み・終了と合わせて決める。
        求める語のある要求（2 段目の確認、T-1702）の "run" は、text の前後の空白を除いたものが語に一致するときだけ
        run。違う・ない・長すぎるときは declined で確定する（やり直しは受け付けない）。入力した語は残さない。"""
        answer = DECISIONS.get(decision)
        with self._lock:
            req = self._approval
            if req is not None and answer == "later" and not req.review:
                answer = None                 # 「あとで」は知識の候補の確認でだけ受け付ける
            if (answer is None or self.closed or req is None or req.answer is not None or req.result is not None
                    or turn != req.turn or turn in self._cut
                    or not hmac.compare_digest(req.id.encode(), str(approval_id).encode())
                    or self.clock() >= req.deadline):
                return "stale"
            if req.expect is not None and answer == "run":
                typed = text.strip() if isinstance(text, str) and len(text) <= MAX_TYPED else None
                if typed is None or not hmac.compare_digest(typed.encode(), req.expect.encode()):
                    answer = "declined"
            req.answer = answer
            req.event.set()
        return "ok"

    def _capture_in_background(self, history: list) -> None:
        import chat_actions
        if not chat_actions.worth_capturing(history):
            return

        def run():
            try:
                self.capture_on_end(history)
            except Exception as e:  # noqa: BLE001  抽出の失敗は記録だけ（本文は出さない）
                print(f"[音声] 会話の終わりの知識の候補を作れませんでした（{type(e).__name__}）", file=sys.stderr)
        threading.Thread(target=run, name="voice-web-capture-on-end", daemon=True).start()

    # ------------------------------------------------------------ 承認（T-1504）

    def _review(self, turn: int, text: str, spoken: str | None = None) -> str:
        """知識の候補 1 件の確認（T-1802）。画面の「保存する」→ "save"、「保存しない」→ "reject"、「あとで」・期限切れ →
        "later"。割り込み・終了・切断・会話が終わっている → "stop"（残りの候補も保存待ちにする）。声の発言は答えにならない。"""
        answer = self._ask(turn, text, spoken=spoken, review=True)
        if answer == "y":
            return "save"
        if answer == "reject":
            return "reject"
        with self._lock:
            if self.closed or turn in self._cut:
                return "stop"
        return "later"

    def _ask(self, turn: int, prompt: str, spoken: str | None = None, expect: str | None = None,
             review: bool = False) -> str:
        """orchestrator.execute の確認。発言 turn 専用の承認の要求を作って画面に出し、答え・期限・割り込み・終了の
        どれかまで待つ。「実行する」が、その ID・その発言・期限内・会話が続いている・割り込まれていない、のすべてを
        満たしたときだけ "y"。それ以外はすべて "n"。
        expect（2 段目の確認の語、T-1702）があれば入力欄つきの要求にし、語が一致したときだけ expect を返す。
        spoken は読み上げる確認の要点（なければ決まった文）。"""
        with self._lock:
            if self.closed or turn in self._cut or self._approval is not None:
                return "n"                    # 割り込まれた・終えた発言には要求を出さない。要求は同時に 1 つだけ
            req = _Approval(turn, self.clock() + APPROVAL_SECONDS, expect, review)
            self._approval = req
            extra = {"input": True, "word": expect} if expect is not None else {}
            if review:
                extra = {"choices": "review"}   # 画面のボタンを「保存する・保存しない・あとで」にする
            self._send_locked("approval", turn, id=req.id, text=confirm_prompt(prompt), expires_in=APPROVAL_SECONDS,
                              **extra)
        self.speaker.say(spoken or APPROVAL_SPOKEN, turn=turn)   # 割り込まれていれば番号が違うので読まない
        while True:
            with self._lock:
                if req.result is None:        # 割り込み・終了の側で取り消されていなければ、ここで確定する
                    if req.answer is not None:
                        req.result = req.answer
                    elif self.clock() >= req.deadline:
                        req.result = "expired"
                    if req.result is not None and req.result != "run":
                        if self._approval is req:
                            self._approval = None
                        self._send_locked("approval_done", turn, id=req.id, result=req.result)
                    # run は実行の直前（_before_run）まで要求を残し、それまでの割り込み・終了で取り消せるようにする
                if req.result is not None:
                    if req.review and req.result == "declined":
                        return "reject"
                    if req.result != "run":
                        return "n"
                    return req.expect if req.expect is not None else "y"
            req.event.wait(POLL_SECONDS)      # 時計を差し替えられるように、短い間隔で期限を確かめる

    def _before_run(self, turn: int) -> bool:
        """orchestrator.execute が承認の後、スキルを動かすスレッドで handler を呼ぶ直前に呼ぶ（Context.before_run）。「実行する」で確定した
        発言 turn の要求がまだ取り消されておらず、会話が続いていて割り込まれていなければ、要求を使い切って True。
        割り込み・終了と同じロックで判定するので、実行を始めた後の取り消しと、取り消した後の実行は起きない。"""
        with self._lock:
            req = self._approval
            if req is None or req.turn != turn or req.result != "run" or self.closed or turn in self._cut:
                return False
            self._approval = None
            self._ran.add(turn)
            self._send_locked("approval_done", turn, id=req.id, result="run")
            return True

    def _cancel_approval_locked(self, turn: int | None = None) -> None:
        """待っている承認の要求を取り消す（self._lock の中で呼ぶ）。turn を渡せば、その発言の要求だけ。
        「実行する」で確定しても実行を始める前（_before_run の前）なら取り消す。
        approval_done（cancelled）は、割り込み・終了のイベントより先に送る（その後は送れないため）。"""
        req = self._approval
        if req is None or (turn is not None and req.turn != turn):
            return
        self._approval = None
        if req.result in (None, "run"):
            req.result = "cancelled"
            self._emit_locked("approval_done", req.turn, id=req.id, result="cancelled")
        req.event.set()

    # ------------------------------------------------------------ 割り込み

    def _interrupt_locked(self, st: _TurnState) -> _TurnState:
        """発言 st に割り込む（self._lock の中で呼ぶ）。voice_chat.run_barge_in と同じく、番号を進めて古い合成・保持した
        音を捨て（生成は ctx.cancel・stream で止まる）、履歴に Owner の発言と、ブラウザが再生し終えた文に割り込みの印を
        付けたもの（聞いた文がなければ AI の行を入れない）を入れる。まだ答えを作っている（取り消せない処理かもしれない）
        なら、文脈を分けて切り離す（結果は使わない）。最後に turn_done（interrupted）を 1 回だけ送る。"""
        self._cancel_approval_locked(st.turn)   # 待っている承認の要求を取り消す（声の「はい」も、ここで取り消しになる）
        self._cut.add(st.turn)                # これより後、この発言のイベントは送らない
        self._active = None                   # 次の発話を受け付ける
        if self._latency is not None and self._latency[0] == st.turn:
            self._latency = None
        heard = self.speaker.heard(st.turn)
        self.speaker.new_turn()               # 残りの合成・保持した音を捨てる
        if st.text is not None:
            lines = [Turn(Who.OWNER, st.text)]
            if heard:                         # 聞いた文だけを履歴に残す
                lines.append(Turn(Who.AI, "".join(heard) + voice_chat.INTERRUPTED_MARK))
            ctx = st.ctx or self.ctx
            if voice_chat.obsidian_busy(ctx, st.outcome, st.text):
                # Obsidian のノートの編集の途中（T-1702）: 文脈を作り直さず、履歴に入れない。次の発言は処理の終わりを待つ
                self._after = st
            else:
                if st.outcome is None:
                    self.ctx = orchestrator.Context(voice=True, terminal=False, history=list(self.ctx.history))
                if not getattr(st.outcome, "private", False):
                    self.ctx.history.extend(lines)
        self._emit_locked("turn_done", st.turn, interrupted=True)
        return st

    def _settle_after(self) -> None:
        """割り込まれた発言が Obsidian のノートの編集の途中なら、その処理が終わるまで待つ（文脈を作り直さず、
        途中の依頼を次の発言に使う、T-1702）。"""
        with self._lock:
            st, self._after = self._after, None
        if st is not None:
            st.done.wait()

    def _finish_interrupted(self, st: _TurnState) -> None:
        if st.record:
            latency.finish(st.lat, ok=False)  # 割り込まれた発言（ロックの外でファイルに書く）

    # ------------------------------------------------------------ 1 回の発言

    def _process(self, st: _TurnState, audio: np.ndarray) -> None:
        latency.use(st.lat)                   # handle を呼ぶこのスレッドで今の記録にする
        result = {"last": None}
        try:
            try:
                self._answer(st, audio, result)
            except Exception as e:  # noqa: BLE001  例外で会話を止めない
                result["last"] = ("error", {"kind": "internal", "message": f"{type(e).__name__}: {e}"})
            try:
                self._wait_spoken(st.turn)    # ブラウザが最後の文を再生し終えるまで（割り込まれたら待たない）
            finally:
                self._finish_turn(st, result)
        finally:
            st.done.set()

    def _finish_turn(self, st: _TurnState, result: dict) -> None:
        # 割り込みの判定・履歴・受付の再開・最後の知らせを同じロックの中で行う: submit・interrupt も同じロックを取るので、
        # 割り込まれた発言の履歴を二重に入れず、次の発言のイベントは必ず turn_done の後に出る
        with self._lock:
            if st.turn in self._cut:          # 割り込まれた（履歴・遅延の記録は割り込みの側で確定済み）
                if latency.current() is st.lat:
                    latency.use(None)
                return
            try:
                if st.text is not None and st.record and not self.closed:
                    self._remember(st)
            finally:
                self._finish_latency(st)
                if self._active is st:
                    self._active = None
                if result["last"] is not None:
                    self._send_locked(result["last"][0], st.turn, **result["last"][1])
                self._send_locked("turn_done", st.turn)

    def _finish_latency(self, st: _TurnState) -> None:
        if self._latency is not None and self._latency[0] == st.turn:
            self._latency = None
        if not st.record:                     # 雑音・「終了」は答えていないので記録しない
            if latency.current() is st.lat:
                latency.use(None)
            return
        failed = self.closed or st.outcome is None or not st.outcome.ok
        latency.finish(st.lat, **({"ok": False} if failed else {}))

    def _wait_spoken(self, turn: int) -> None:
        while not self.closed and self._active is not None and self._active.turn == turn and self.speaker.speaking():
            time.sleep(POLL_SECONDS)

    def _remember(self, st: _TurnState) -> None:
        """履歴は voice_chat.py と同じ規則: Owner の発言と、ブラウザが再生し終えた文だけ（空なら AI の行を入れない）。
        回答が例外で失敗した（outcome が None）ときも、Owner の発言は残す（T-1210: ナレッジ化の材料）。"""
        outcome = st.outcome
        if outcome is not None and outcome.skill == "knowledge-capture" and outcome.method == "rule":
            return
        if getattr(outcome, "private", False):
            return                            # Obsidian のノートの編集などは履歴に入れない（T-1702）
        self.ctx.history.append(Turn(Who.OWNER, st.text))
        if outcome is None:
            return
        heard = "".join(self.speaker.heard(st.turn))
        if outcome.ok and outcome.error is None and heard:
            self.ctx.history.append(Turn(Who.AI, heard))

    def _answer(self, st: _TurnState, audio: np.ndarray, result: dict) -> None:
        """認識して答え、読み上げを始める。result に最後に送るイベントを入れ、st に発言・結果を入れる。"""
        turn = st.turn
        try:
            text = (self.transcribe(audio) or "").strip()
        except Exception as e:  # noqa: BLE001
            result["last"] = ("error", {"kind": "stt", "message": f"認識できませんでした（{type(e).__name__}）"})
            return
        st.lat.mark("stt")
        if is_noise(text):
            st.record = False
            result["last"] = ("ignored", {"reason": "noise" if text.strip(" 　。、.!！?？") else "empty"})
            return
        self._settle_after()                  # 前の発言が Obsidian のノートの編集の途中なら、その終わりを待つ（T-1702）
        with self._lock:
            if self.closed or turn in self._cut:
                return                        # 認識している間に割り込まれた・終えた（答えない）
            st.text = text                    # 回答が失敗しても Owner の発言は履歴に残す
            ctx = self.ctx                    # 割り込みで文脈が切り離されても、この発言はこの文脈だけを使う
            st.ctx = ctx
            self._send_locked("recognized", turn, text=text)
        if any(w in text for w in QUIT_WORDS) and len(text) <= 10:
            st.record = False
            self.end(turn, capture=True)      # 「終了」と話した: 会話の終わりの知識化（T-1803）
            return
        started = {"value": False}

        def stream(chunk: str) -> None:
            if self.closed or turn != self.speaker.turn:
                raise voice_chat.Cancelled()  # 割り込まれた: 何も送らずに生成を止める（llm 側で claude -p も止まる）
            if not started["value"]:
                started["value"] = True
                st.lat.mark("first_text")
            self._send("answer_chunk", turn, text=chunk)   # 割り込まれた直後なら送らない（_send_locked）
            self.speaker.feed(chunk, turn=turn)   # 文がそろったら合成し始める（回答の終わりを待たない）

        ctx.stream = stream
        ctx.cancel = lambda t=turn: self.closed or t != self.speaker.turn   # voice_chat.run_barge_in と同じ
        ctx.before_run = lambda t=turn: self._before_run(t)   # 承認の後、実行の直前にもう一度確かめる（T-1504）
        ctx.review = lambda text, spoken=None, t=turn: self._review(t, text, spoken)   # 知識の候補の確認（T-1802）
        try:
            outcome = self.handle(text, ctx, ask=lambda prompt, spoken=None, expect=None:   # 確認は画面で（T-1504）
                                  self._ask(turn, prompt, spoken=spoken, expect=expect))
        except Exception as e:  # noqa: BLE001
            result["last"] = ("error", {"kind": "answer", "message": f"答えを作れませんでした（{type(e).__name__}: {e}）"})
            return
        finally:
            ctx.stream = None
            ctx.before_run = None
            ctx.review = None
            with self._lock:
                self._cancel_approval_locked(turn)   # 実行の直前の確認を通らなかった要求を残さない
        st.outcome = outcome
        with self.speaker._lock:              # 番号の確認と残りの投入を、読み上げの側と同じロックで
            if turn == self.speaker.turn and not self.closed:
                voice_chat.speak_rest(outcome, started["value"], ctx, self.speaker, {"turn": turn})
        self._send("answer_done", turn, text=outcome.text, skill=outcome.skill, ok=outcome.ok)
        with self._lock:
            if turn in self._ran and turn in self._cut:   # 承認して実行した後に割り込まれた: 結果は文脈に使わないが、
                self._emit_locked("run_result", turn, text=outcome.text, skill=outcome.skill, ok=outcome.ok)   # 画面には出す
            self._ran.discard(turn)
        try:
            self.record(text, outcome)        # 割り込まれた後でも、結果が出ていれば記録する
        except Exception as e:  # noqa: BLE001  記録の失敗で会話を止めない（本文は出さない）
            print(f"[記録できませんでした] {type(e).__name__}", file=sys.stderr)
