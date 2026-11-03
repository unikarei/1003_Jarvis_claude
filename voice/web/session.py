"""ブラウザでの声の会話の 1 つ分の処理（T-1501・T-1502）。

ブラウザが区切った発話 1 回分の WAV を受け、16 kHz にして文字にし、orchestrator.handle で答える。
回答は voice.duplex.DuplexSpeaker で文ごとに合成し、voice.web.player.BrowserPlayer がブラウザに渡す（再生は
ブラウザ）。履歴には、ブラウザが再生し終えた文だけを残す（voice_chat.py と同じ規則）。割り込み（T-1503）は
まだないので、読み上げが終わるまで次の発話を受け付けない（voice_chat.py --no-barge-in と同じ動き）。
途中の経過はイベント（dict）として emit に渡す（送り方は server.py が決める）。音声はメモリ上だけで扱い、
ファイル・DB・ログに残さない。sounddevice は import しない（Mac の音声の機器を使わない）。
"""

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


class AudioError(Exception):
    """受け付けない音声。status は HTTP の状態コード（400 か 413）。"""

    def __init__(self, status: int, message: str):
        super().__init__(message)
        self.status = status


class Busy(Exception):
    """前の発言を処理している（読み上げの終わりまで）。"""


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


def default_launcher(settings: "tts.VoiceSettings | None" = None):
    """選んだ音声のエンジン（VOICEVOX・AivisSpeech）の起動と停止。管理しないエンジン（say）なら None。"""
    from voice import engine_process
    return engine_process.for_settings(settings or tts.load_settings())


class _FirstSynth:
    """最初の合成の前にだけエンジンを待ち、合成の関数を作る（会話の開始を遅らせない）。
    synth を渡さなければ voice_chat.build_speaker と同じ tts.make_synth（有料のエンジンには切り替えない）。"""

    def __init__(self, launcher, synth, notify, closed):
        self.launcher, self.synth, self.notify, self.closed = launcher, synth, notify, closed
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
        return self.synth(sentence, cancel)


class WebSession:
    """会話 1 つ分。認識（transcribe）・回答（handle）・イベントの送り先（emit）・記録（record）・合成（synth:
    (文, 取り消し) → 16 kHz の float32）・エンジンの起動と停止（launcher: voice.engine_process.EngineProcess と同じ形）・
    再生の待つ期限の時計（clock）を差し替えられる。launcher を渡さなければエンジンを起動・停止しない。"""

    def __init__(self, transcribe=None, handle=None, emit=None, record=None, synth=None, launcher=None,
                 clock=time.monotonic):
        self.id = secrets.token_urlsafe(12)
        self.transcribe = transcribe or _transcribe
        self.handle = handle or orchestrator.handle
        self.emit = emit if emit is not None else (lambda event: None)
        self.record = record or _record
        self.launcher = launcher
        self.ctx = orchestrator.Context(voice=True)
        self.closed = False
        self.turn = 0
        self._seq = 0
        self._busy = False
        self._lock = threading.Lock()
        self._latency: tuple[int, latency.Turn] | None = None   # （発言の番号, その発言の遅延の記録）
        self.player = BrowserPlayer(emit=self._player_event, clock=clock)
        self.speaker = DuplexSpeaker(self.player, voice=voice_chat.VOICE,
                                     synth=_FirstSynth(launcher, synth, self._notice, lambda: self.closed))
        self._send("ready")
        try:
            voice_chat.start_engine(launcher)      # 起動し始めるだけ（待たない）
        except Exception as e:  # noqa: BLE001  起動できなくても say で読む
            self._notice(f"[音声] エンジンを起動できません（{type(e).__name__}）。macOS の say（Kyoko）で読み上げます。")

    # ------------------------------------------------------------ イベント

    def _send(self, type_: str, turn: int | None = None, **data) -> None:
        with self._lock:
            self._send_locked(type_, turn, **data)

    def _send_locked(self, type_: str, turn: int | None = None, **data) -> None:
        """self._lock を持った状態で送る。"""
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

    def _latency_for(self, turn: int) -> "latency.Turn | None":
        current = self._latency
        return current[1] if current is not None and current[0] == turn else None

    def _player_event(self, type_: str, turn: int, **data) -> None:
        if type_ == "audio":
            lat = self._latency_for(turn)
            if lat is not None:
                lat.mark("audio_ready")       # 最初の文の音を保持した時刻（最初の 1 回だけ記録される）
        self._send(type_, turn, **data)

    # ------------------------------------------------------------ 操作

    def submit(self, data: bytes, rate: int) -> int:
        """発話 1 回分の WAV を受け付け、発言の番号を返す。認識・回答・読み上げは別のスレッドで行う。"""
        accepted = time.monotonic()
        try:
            audio = decode_wav(data, rate)
        except AudioError as e:
            self._send("error", kind="bad_audio", message=str(e))
            raise
        with self._lock:
            if self.closed:
                raise Busy("会話は終わっています")
            if self._busy:
                raise Busy("前の発言を処理しています")
            self._busy = True
            self.turn = self.speaker.new_turn()     # 読み上げの側と同じ番号（古い発言の音を捨てる）
            turn = self.turn
        audio16 = tts.resample(audio, rate)
        threading.Thread(target=self._process, args=(turn, audio16, accepted), name=f"voice-web-turn-{turn}",
                         daemon=True).start()
        return turn

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

    def end(self, turn: int | None = None) -> None:
        """会話を終える。turn は「終了」と話した発言の番号（/api/end・切断では None）。
        読み上げを止め、保持した音を消し、この会話が起動したエンジンを止める。"""
        with self._lock:
            if self.closed:
                return
            self.closed = True
        try:
            self.speaker.close()              # 未再生の音を捨て、合成のスレッドを終える（BrowserPlayer の音も消える）
        finally:
            self._send("ended", turn)
            if self.launcher is not None:
                try:
                    self.launcher.stop()      # この会話が起動したものだけを止める（EngineProcess の規則）
                except Exception as e:  # noqa: BLE001
                    print(f"[音声] エンジンを止められませんでした（{type(e).__name__}）", file=sys.stderr)

    # ------------------------------------------------------------ 1 回の発言

    def _process(self, turn: int, audio: np.ndarray, accepted: float) -> None:
        lat = latency.start("web", at=accepted)   # 発話を受け付けた時刻から（本文・音声は記録しない）
        self._latency = (turn, lat)
        result = {"last": None, "text": None, "outcome": None, "record": True}
        try:
            self._answer(turn, audio, lat, result)
        except Exception as e:  # noqa: BLE001  例外で会話を止めない
            result["last"] = ("error", {"kind": "internal", "message": f"{type(e).__name__}: {e}"})
        try:
            self._wait_spoken()               # ブラウザが最後の文を再生し終えるまで（その間は受け付けない）
            if result["text"] is not None and not self.closed:
                self._remember(turn, result["text"], result["outcome"])
        finally:
            self._finish_latency(turn, lat, result)
            # 受付の再開と最後の知らせを同じロックの中で行う: submit も同じロックを取るので、次の発言のイベントは
            # 必ず turn_done の後に出る。知らせた時点では受け付けているので、すぐ次の発言を送っても 409 にしない
            with self._lock:
                self._busy = False
                if result["last"] is not None:
                    self._send_locked(result["last"][0], turn, **result["last"][1])
                self._send_locked("turn_done", turn)

    def _finish_latency(self, turn: int, lat: "latency.Turn", result: dict) -> None:
        if self._latency is not None and self._latency[0] == turn:
            self._latency = None
        if not result["record"]:              # 雑音・「終了」は答えていないので記録しない
            if latency.current() is lat:
                latency.use(None)
            return
        failed = self.closed or result["outcome"] is None or not result["outcome"].ok
        latency.finish(lat, **({"ok": False} if failed else {}))

    def _wait_spoken(self) -> None:
        while not self.closed and self.speaker.speaking():
            time.sleep(POLL_SECONDS)

    def _remember(self, turn: int, text: str, outcome) -> None:
        """履歴は voice_chat.py と同じ規則: Owner の発言と、ブラウザが再生し終えた文だけ（空なら AI の行を入れない）。
        回答が例外で失敗した（outcome が None）ときも、Owner の発言は残す（T-1210: ナレッジ化の材料）。"""
        if outcome is not None and outcome.skill == "knowledge-capture" and outcome.method == "rule":
            return
        self.ctx.history.append(Turn(Who.OWNER, text))
        if outcome is None:
            return
        heard = "".join(self.speaker.heard(turn))
        if outcome.ok and outcome.error is None and heard:
            self.ctx.history.append(Turn(Who.AI, heard))

    def _answer(self, turn: int, audio: np.ndarray, lat: "latency.Turn", result: dict) -> None:
        """認識して答え、読み上げを始める。result に最後に送るイベント・発言・結果を入れる。"""
        try:
            text = (self.transcribe(audio) or "").strip()
        except Exception as e:  # noqa: BLE001
            result["last"] = ("error", {"kind": "stt", "message": f"認識できませんでした（{type(e).__name__}）"})
            return
        lat.mark("stt")
        if is_noise(text):
            result["record"] = False
            result["last"] = ("ignored", {"reason": "noise" if text.strip(" 　。、.!！?？") else "empty"})
            return
        self._send("recognized", turn, text=text)
        if any(w in text for w in QUIT_WORDS) and len(text) <= 10:
            result["record"] = False
            self.end(turn)
            return
        started = {"value": False}

        def stream(chunk: str) -> None:
            if self.closed or turn != self.turn:
                return
            if not started["value"]:
                started["value"] = True
                lat.mark("first_text")
            self._send("answer_chunk", turn, text=chunk)
            self.speaker.feed(chunk, turn=turn)   # 文がそろったら合成し始める（回答の終わりを待たない）

        self.ctx.stream = stream
        result["text"] = text                 # 回答が失敗しても Owner の発言は履歴に残す
        try:
            # 確認の要るスキルは、画面での承認（T-1504）までは実行しない
            outcome = self.handle(text, self.ctx, ask=lambda prompt: "n")
        except Exception as e:  # noqa: BLE001
            result["last"] = ("error", {"kind": "answer", "message": f"答えを作れませんでした（{type(e).__name__}: {e}）"})
            return
        finally:
            self.ctx.stream = None
        with self.speaker._lock:              # 番号の確認と残りの投入を、読み上げの側と同じロックで
            if turn == self.speaker.turn and not self.closed:
                voice_chat.speak_rest(outcome, started["value"], self.ctx, self.speaker, {"turn": turn})
        self._send("answer_done", turn, text=outcome.text, skill=outcome.skill, ok=outcome.ok)
        result["text"], result["outcome"] = text, outcome
        try:
            self.record(text, outcome)
        except Exception as e:  # noqa: BLE001  記録の失敗で会話を止めない（本文は出さない）
            print(f"[記録できませんでした] {type(e).__name__}", file=sys.stderr)
