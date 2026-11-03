"""ブラウザでの声の会話の 1 つ分の処理（T-1501）。

ブラウザが区切った発話 1 回分の WAV を受け、16 kHz にして文字にし、orchestrator.handle で答える。
途中の経過はイベント（dict）として emit に渡す（送り方は server.py が決める）。音声はメモリ上だけで扱い、
ファイル・DB・ログに残さない。sounddevice は import しない（Mac の音声の機器を使わない）。
"""

import io
import secrets
import sys
import threading
import wave

import numpy as np

import orchestrator
from ckc.models import Speaker as Who, Turn
from voice import tts
from voice_chat import QUIT_WORDS, is_noise   # voice_chat の import では音声の機器を開かない（sounddevice は関数の中だけ）

MAX_AUDIO_SECONDS = 20.5      # 発話 1 回分の長さの上限（ページの最長 20 秒＋前の 0.3 秒＋余裕）
MIN_RATE, MAX_RATE = 8000, 96000


class AudioError(Exception):
    """受け付けない音声。status は HTTP の状態コード（400 か 413）。"""

    def __init__(self, status: int, message: str):
        super().__init__(message)
        self.status = status


class Busy(Exception):
    """前の発言を処理している。"""


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


class WebSession:
    """会話 1 つ分。認識（transcribe）・回答（handle）・イベントの送り先（emit）・記録（record）を差し替えられる。"""

    def __init__(self, transcribe=None, handle=None, emit=None, record=None):
        self.id = secrets.token_urlsafe(12)
        self.transcribe = transcribe or _transcribe
        self.handle = handle or orchestrator.handle
        self.emit = emit if emit is not None else (lambda event: None)
        self.record = record or _record
        self.ctx = orchestrator.Context(voice=True)
        self.closed = False
        self.turn = 0
        self._seq = 0
        self._busy = False
        self._lock = threading.Lock()
        self._send("ready")

    # ------------------------------------------------------------ イベント

    def _send(self, type_: str, turn: int | None = None, **data) -> None:
        with self._lock:
            if self.closed and type_ != "ended":
                return                        # 終えた後の古い発言の結果は送らない
            self._seq += 1
            event = {"session": self.id, "seq": self._seq, "type": type_}
            if turn is not None:
                event["turn"] = turn
            event.update(data)
            self.emit(event)

    # ------------------------------------------------------------ 操作

    def submit(self, data: bytes, rate: int) -> int:
        """発話 1 回分の WAV を受け付け、発言の番号を返す。認識と回答は別のスレッドで行う。"""
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
            self.turn += 1
            turn = self.turn
        audio16 = tts.resample(audio, rate)
        threading.Thread(target=self._process, args=(turn, audio16), name=f"voice-web-turn-{turn}",
                         daemon=True).start()
        return turn

    def end(self, turn: int | None = None) -> None:
        """会話を終える。turn は「終了」と話した発言の番号（/api/end・切断では None）。"""
        with self._lock:
            if self.closed:
                return
            self.closed = True
        self._send("ended", turn)

    # ------------------------------------------------------------ 1 回の発言

    def _process(self, turn: int, audio: np.ndarray) -> None:
        last = None
        try:
            last = self._answer(turn, audio)
        except Exception as e:  # noqa: BLE001  例外で会話を止めない
            last = ("error", {"kind": "internal", "message": f"{type(e).__name__}: {e}"})
        finally:
            with self._lock:
                self._busy = False
        if last is not None:          # 記録・履歴・受付の再開の後に知らせる（すぐ次の発言を送っても 409 にしない）
            self._send(last[0], turn, **last[1])

    def _answer(self, turn: int, audio: np.ndarray) -> tuple[str, dict] | None:
        """1 回の発言を処理する。返り値は最後に送るイベント（種類, 内容）。"""
        try:
            text = (self.transcribe(audio) or "").strip()
        except Exception as e:  # noqa: BLE001
            return "error", {"kind": "stt", "message": f"認識できませんでした（{type(e).__name__}）"}
        if is_noise(text):
            return "ignored", {"reason": "noise" if text.strip(" 　。、.!！?？") else "empty"}
        self._send("recognized", turn, text=text)
        if any(w in text for w in QUIT_WORDS) and len(text) <= 10:
            self.end(turn)
            return None
        self.ctx.stream = lambda chunk: self._send("answer_chunk", turn, text=chunk)
        try:
            # 確認の要るスキルは、画面での承認（T-1504）までは実行しない
            outcome = self.handle(text, self.ctx, ask=lambda prompt: "n")
        except Exception as e:  # noqa: BLE001
            return "error", {"kind": "answer", "message": f"答えを作れませんでした（{type(e).__name__}: {e}）"}
        finally:
            self.ctx.stream = None
        try:
            self.record(text, outcome)
        except Exception as e:  # noqa: BLE001  記録の失敗で会話を止めない（本文は出さない）
            print(f"[記録できませんでした] {type(e).__name__}", file=sys.stderr)
        # 履歴は文字の会話と同じ（読み上げない段階。T-1502 で「聞いた文だけ」に変える）
        if not (outcome.skill == "knowledge-capture" and outcome.method == "rule"):
            self.ctx.history.append(Turn(Who.OWNER, text))
            if outcome.ok and outcome.error is None:
                self.ctx.history.append(Turn(Who.AI, outcome.text))
        return "answer_done", {"text": outcome.text, "skill": outcome.skill, "ok": outcome.ok}
