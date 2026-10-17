"""声の会話の割り込み（Barge-in、../docs/decisions.md ADR-0028、T-1407）。

JARVIS が考えている間・話している間もマイクを聞き続け、Owner が話し始めたら読み上げをすぐ止める。
- Player: 読み上げの音（`say -o` で作った音声）を自分で再生し、再生した音量を「参照」として残す。
- EchoGate: マイクの音が「参照 × 回り込み」の予測を十分に上回る状態が続いたときだけ、人が話したとみなす。
- DuplexSpeaker: voice.speak.Speaker と同じ使い方（feed・say・flush・stop・speaking・wait・close）で、
  発言の番号（turn）を持つ。番号が変わったら、古い発言の合成・未再生の音を捨てる。
- Session: マイクを開きっぱなしにし、割り込みの判定と発話の録音を同じ音の流れで行う。
新しい依存はない（sounddevice・soundfile・numpy）。録音・合成した音はファイルに残さない。
"""

import os
import subprocess
import tempfile
import threading
import time
from collections import deque
from collections.abc import Callable

import numpy as np

from voice import record as rec
from voice.speak import SENTENCE_END, Speaker, speakable

SAMPLE_RATE = 16000
FRAME = 320                    # 20 ms
FRAME_SECONDS = FRAME / SAMPLE_RATE
CONFIRM_SECONDS = 0.16         # 割り込みとみなすまでの、声が続く長さ（物音・回り込みの一瞬で止めない）
ECHO_MARGIN = 3.0              # 予測した回り込みの何倍を超えたら人の声とみなすか
ECHO_SAFETY = 1.5              # 測った回り込みに掛ける余裕
MAX_DELAY_SECONDS = 0.3        # 再生からマイクに届くまでの遅れの探索の上限


def _env_bool(name: str, default: bool) -> bool:
    value = os.environ.get(name)
    return default if value is None else value.strip().lower() not in ("0", "false", "no", "off", "")


def barge_in_enabled() -> bool:
    """JARVIS_VOICE_BARGE_IN=0 で割り込みを使わない（既定は使う）。"""
    return _env_bool("JARVIS_VOICE_BARGE_IN", True)


def frame_rms(frame: np.ndarray) -> float:
    return rec.rms(frame)


# ---------------------------------------------------------------- 判定

class EchoGate:
    """20 ms ごとに、マイクの音量と、その時点で届いているはずの再生の音量（参照）から、人が話しているかを判定する。"""

    def __init__(self, noise: float, coupling: float = 0.0, factor: float = rec.THRESHOLD_FACTOR,
                 margin: float = ECHO_MARGIN, confirm_seconds: float = CONFIRM_SECONDS):
        self.noise, self.coupling, self.factor, self.margin = noise, coupling, factor, margin
        self.confirm = max(1, round(confirm_seconds / FRAME_SECONDS))
        self.count = 0

    def threshold(self, ref: float) -> float:
        """周囲の音のしきい値と、回り込みの予測 × 余裕の大きいほう。"""
        base = max(rec.MIN_THRESHOLD, self.noise * self.factor)
        return max(base, self.noise + self.margin * self.coupling * ref)

    def update(self, mic: float, ref: float = 0.0) -> bool:
        """声が confirm フレーム続いたら True（1 回だけ。reset まで再び True にならない）。"""
        if mic >= self.threshold(ref):
            self.count += 1
            return self.count == self.confirm
        self.count = 0
        return False

    def reset(self) -> None:
        self.count = 0


def estimate_coupling(mic: list[float], ref: list[float], noise: float,
                      max_delay_frames: int = round(MAX_DELAY_SECONDS / FRAME_SECONDS)) -> tuple[float, int]:
    """再生の音量（ref）とマイクの音量（mic、同じ時刻の並び）から、回り込みの大きさと遅れ（フレーム数）を測る。
    遅れごとに「マイクの周囲の音を超えた分 ≒ 回り込み × 参照」を最小二乗で当て、相関が最も高い遅れを選ぶ。"""
    m = np.maximum(np.asarray(mic, dtype=np.float64) - noise, 0.0)
    r = np.asarray(ref, dtype=np.float64)
    best = (0.0, 0, -1.0)
    for delay in range(0, min(max_delay_frames, len(r) - 1) + 1):
        x, y = r[:len(r) - delay], m[delay:delay + len(r) - delay]
        if not len(x) or float(np.dot(x, x)) == 0.0:
            continue
        score = float(np.dot(x, y)) / (np.linalg.norm(x) * (np.linalg.norm(y) or 1.0))
        if score > best[2]:
            best = (max(0.0, float(np.dot(x, y) / np.dot(x, x))), delay, score)
    return best[0] * ECHO_SAFETY, best[1]


# ---------------------------------------------------------------- 再生

class Player:
    """読み上げの音を再生する。再生した音量を時刻つきで残し（参照）、stop() で次の 20 ms から無音にする。"""

    def __init__(self, device: str | None = None, stream_factory: Callable | None = None):
        self._lock = threading.Lock()
        self._queue: deque = deque()            # (turn, sentence, audio float32)
        self._current: list | None = None       # [turn, sentence, audio, position]
        self.ref: deque = deque(maxlen=3000)    # (時刻, 再生した音量 int16 換算)
        self.played: list[tuple[int, str, float]] = []   # 最後まで再生した文と、その時刻（聞いた文として履歴に残す）
        self.output_latency = 0.0
        self.turn: int | None = None             # 受け付ける発言の番号（set_turn。古い番号の音は enqueue しない）
        self._last_sound = 0.0                   # 最後に音を出力に渡した時刻
        if stream_factory is None:
            import sounddevice as sd
            stream_factory = lambda cb: sd.OutputStream(samplerate=SAMPLE_RATE, channels=1, dtype="float32",  # noqa: E731
                                                       blocksize=FRAME, device=device, callback=cb)
        self._stream = stream_factory(self._callback)
        self._stream.start()
        self.output_latency = float(getattr(self._stream, "latency", 0.0) or 0.0) + FRAME_SECONDS  # 機器 + 1 ブロック

    def _callback(self, outdata, frames, time_info, status) -> None:
        out = np.zeros(frames, dtype=np.float32)
        filled = 0
        with self._lock:
            while filled < frames:
                if self._current is None:
                    if not self._queue:
                        break
                    turn, sentence, audio = self._queue.popleft()
                    self._current = [turn, sentence, audio, 0]
                turn, sentence, audio, pos = self._current
                n = min(frames - filled, len(audio) - pos)
                out[filled:filled + n] = audio[pos:pos + n]
                filled += n
                self._current[3] = pos + n
                self._last_sound = time.monotonic()
                if pos + n >= len(audio):
                    self.played.append((turn, sentence, time.monotonic()))
                    self._current = None
            self.ref.append((time.monotonic(), float(np.sqrt(np.mean((out * 32768.0) ** 2)))))
        outdata[:, 0] = out

    def enqueue(self, turn: int, sentence: str, audio: np.ndarray) -> None:
        with self._lock:
            if self.turn is not None and turn != self.turn:
                return                            # 割り込まれた発言の音は再生しない（合成と停止の競合）
            self._queue.append((turn, sentence, audio.astype(np.float32)))

    def set_turn(self, turn: int) -> None:
        """この番号の音だけを受け付け、それ以外の未再生の音を捨てる。"""
        with self._lock:
            self.turn = turn
        self.stop()

    def stop(self) -> None:
        """未再生の音を捨てる。再生中の文も途中で止める（その文は聞いた文にしない）。出力の遅れの間に
        渡し終えた文は、まだスピーカーから出きっていないため、聞いた文から除く。"""
        with self._lock:
            self._queue.clear()
            self._current = None
            self._last_sound = 0.0                # 止めたので、出力の遅れの分を待たない
            heard_before = time.monotonic() - self.output_latency
            self.played = [p for p in self.played if p[2] <= heard_before]

    def speaking(self) -> bool:
        """再生中か、最後の音がまだスピーカーから出ている（出力の遅れの間）なら True。"""
        with self._lock:
            return (self._current is not None or bool(self._queue)
                    or time.monotonic() < self._last_sound + self.output_latency)

    def level(self, start: float, end: float) -> float:
        """時刻 start〜end に再生した音量の最大（参照）。"""
        with self._lock:
            values = [r for t, r in self.ref if start <= t <= end]
        return max(values, default=0.0)

    def heard(self, turn: int) -> list[str]:
        with self._lock:
            return [s for t, s, _ in self.played if t == turn]

    def close(self) -> None:
        self.stop()
        try:
            self._stream.stop()
            self._stream.close()
        except Exception:
            pass


def synthesize_say(sentence: str, voice: str = "Kyoko", cancel: threading.Event | None = None) -> np.ndarray | None:
    """macOS の say で 1 文を 16 kHz の音声にする（一時ファイルは読んだらすぐ消す）。cancel で途中で止める。"""
    import soundfile as sf

    with tempfile.TemporaryDirectory(prefix="jarvis-tts-") as tmp:
        path = os.path.join(tmp, "s.wav")
        proc = subprocess.Popen(["say", "-v", voice, "-o", path, "--file-format=WAVE", "--data-format=LEF32@16000",
                                 sentence], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        while proc.poll() is None:
            if cancel is not None and cancel.is_set():
                proc.terminate()
                proc.wait()
                return None
            time.sleep(0.01)
        if proc.returncode != 0 or not os.path.exists(path):
            return None
        audio, _ = sf.read(path, dtype="float32")
    return audio if audio.ndim == 1 else audio[:, 0]


class DuplexSpeaker(Speaker):
    """voice.speak.Speaker と同じ使い方で、音声を Player で再生する。発言の番号（turn）が変わったら、
    古い番号の文は合成も再生もしない。"""

    def __init__(self, player: Player, voice: str = "Kyoko", synth: Callable | None = None):
        self.player = player
        self.turn = 0
        self._synth = synth or (lambda sentence, cancel: synthesize_say(sentence, voice, cancel))
        self._cancel = threading.Event()
        self._busy = False
        super().__init__(voice=voice, runner=lambda cmd: None)

    def _loop(self) -> None:
        while True:
            item = self._queue.get()
            if item is None:
                return
            turn, sentence = item
            try:
                with self._lock:                      # 番号の確認と取り消しの用意を、new_turn と同じロックで
                    if turn != self.turn:
                        continue
                    self._busy = True
                    self._cancel = cancel = threading.Event()
                audio = self._synth(sentence, cancel)
                if audio is not None and turn == self.turn and not cancel.is_set():
                    self.player.enqueue(turn, sentence, audio)
            finally:
                with self._lock:
                    self._busy = False
                self._queue.task_done()

    def say(self, text: str, turn: int | None = None) -> None:
        """1 文を読む。turn を渡したら、その番号が今の発言のときだけ（番号の確認と投入を同じロックで）。"""
        text = speakable(text)
        if not text:
            return
        with self._lock:
            if turn is not None and turn != self.turn:
                return
            self._queue.put((self.turn if turn is None else turn, text))

    def feed(self, chunk: str, turn: int | None = None) -> None:
        """逐次表示の差分を文に区切って読む。turn を渡したら、その番号の発言としてだけ読む。"""
        if turn is not None and turn != self.turn:
            return
        self._buffer += chunk
        parts = SENTENCE_END.split(self._buffer)
        self._buffer = parts.pop()
        for part in parts:
            if part.strip().startswith("```"):
                self._in_code = not self._in_code
                continue
            if not self._in_code:
                self.say(part, turn)

    def new_turn(self) -> int:
        """新しい発言を始める（古い発言の合成・未再生の音を捨てる）。"""
        with self._lock:
            self.turn += 1
            turn = self.turn
        if hasattr(self.player, "set_turn"):
            self.player.set_turn(turn)            # 再生の側でも古い番号の音を受け付けない
        self.stop()
        return turn

    def stop(self) -> None:
        self._buffer, self._in_code = "", False
        with self._lock:
            self._cancel.set()
        import queue as _queue
        try:
            while True:
                self._queue.get_nowait()
                self._queue.task_done()
        except _queue.Empty:
            pass
        self.player.stop()

    def speaking(self) -> bool:
        with self._lock:
            busy = self._busy
        return busy or not self._queue.empty() or self.player.speaking()

    def wait(self) -> None:
        while self.speaking():
            time.sleep(0.02)

    def heard(self, turn: int) -> list[str]:
        return self.player.heard(turn)

    def close(self) -> None:
        self.stop()
        self._queue.put(None)
        self.player.close()


# ---------------------------------------------------------------- マイクと割り込み

class Session:
    """マイクを開きっぱなしにして、割り込みの判定（arm の間）と発話の録音を行う。"""

    PREROLL_FRAMES = 15        # 割り込みの検出の前 0.3 秒も発話に含める

    def __init__(self, player: Player, speaker: DuplexSpeaker, device: str | None = None,
                 stream_factory: Callable | None = None):
        self.player, self.speaker = player, speaker
        self._cond = threading.Condition()
        self.frames: list[tuple[float, np.ndarray, float]] = []   # (時刻, 20 ms の音, 音量)
        self.base = 0                                              # frames[0] の通し番号（古い音を捨てた数）
        self.gate: EchoGate | None = None
        self.delay = 0.0
        self.armed = False
        self.barged = threading.Event()
        self.barge_index: int | None = None
        self.stop_latency: list[float] = []   # 検出から再生の停止の指示までの時間（記録用）
        self.events: list[dict] = []          # 割り込みごとの時間（話し始め → 検出 → 停止の指示、ミリ秒）
        self._pending: list[np.ndarray] = []
        if stream_factory is None:
            import sounddevice as sd
            stream_factory = lambda cb: sd.InputStream(samplerate=SAMPLE_RATE, channels=1, dtype="int16",  # noqa: E731
                                                      blocksize=FRAME, device=device, callback=cb)
        self._stream = stream_factory(self._callback)
        self._stream.start()

    def _callback(self, indata, frames, time_info, status) -> None:
        self.push(indata[:, 0].copy())

    def push(self, frame: np.ndarray, now: float | None = None) -> None:
        """20 ms の音を 1 つ受け取る（マイクのスレッド。テストからも呼ぶ）。"""
        now = time.monotonic() if now is None else now
        level = frame_rms(frame)
        with self._cond:
            self.frames.append((now, frame, level))
            if len(self.frames) > 50 * 120:          # 2 分を超えたら古いものを捨てる（通し番号は変えない）
                drop = len(self.frames) - 50 * 60
                del self.frames[:drop]
                self.base += drop
            index = self.base + len(self.frames) - 1  # 通し番号
            self._cond.notify_all()
        if self.armed and self.gate is not None:
            ref = self.player.level(now - self.delay - 0.04, now - self.delay + 0.04)
            if self.gate.update(level, ref):
                self.armed = False
                # まず止める（目標 200 ms 以内）。番号も進め、検出の後に届く古い発言の文・生成をすぐ捨てさせる
                self.speaker.new_turn()
                stopped = time.monotonic()
                self.stop_latency.append(stopped - now)
                with self._cond:
                    self.barge_index = max(self.base, index - self.gate.confirm - self.PREROLL_FRAMES)
                    onset = self.frames[max(0, index - self.gate.confirm + 1 - self.base)][0]
                out_ms = round(getattr(self.player, "output_latency", 0.0) * 1000)
                self.events.append({"detect_ms": round((now - onset) * 1000 + FRAME_SECONDS * 1000),
                                    "stop_call_ms": round((stopped - now) * 1000, 1), "output_ms": out_ms})
                self.barged.set()

    # 校正 -------------------------------------------------------------

    def noise_floor(self, seconds: float = 0.5) -> float:
        with self._cond:
            levels = [lv for _, _, lv in self.frames[-round(seconds / FRAME_SECONDS):]]
        return float(np.median(levels)) if levels else 0.0

    def calibrate(self, greeting: str, timeout: float = 10.0) -> dict:
        """あいさつを読み上げながら、回り込みの大きさと遅れを測り、判定を用意する。"""
        time.sleep(0.5)
        noise = self.noise_floor()
        with self._cond:
            start = len(self.frames)
        t0 = time.monotonic()
        self.speaker.say(greeting)
        deadline = time.monotonic() + timeout
        time.sleep(0.2)
        while self.speaker.speaking() and time.monotonic() < deadline:
            time.sleep(0.02)
        time.sleep(MAX_DELAY_SECONDS)
        with self._cond:
            frames = self.frames[start:]
        mic = [lv for _, _, lv in frames]
        ref = [self.player.level(t - FRAME_SECONDS / 2, t + FRAME_SECONDS / 2) for t, _, _ in frames]
        coupling, delay_frames = estimate_coupling(mic, ref, noise)
        self.delay = delay_frames * FRAME_SECONDS
        self.gate = EchoGate(noise=noise, coupling=coupling)
        return {"noise": round(noise), "coupling": round(coupling, 3), "delay_ms": round(self.delay * 1000),
                "seconds": round(time.monotonic() - t0, 1)}

    # 割り込み ---------------------------------------------------------

    def arm(self) -> None:
        """考え中・読み上げ中に割り込みを受け付ける。"""
        if self.gate is not None:
            self.gate.reset()
        self.barged.clear()
        self.armed = True

    def disarm(self) -> None:
        self.armed = False

    # 録音 -------------------------------------------------------------

    def _blocks(self, start: int):
        """通し番号 start 以降の音を 0.1 秒のブロックで返す（新しい音が来るまで待つ）。"""
        index = start
        while True:
            with self._cond:
                index = max(index, self.base)         # 捨てられた分は飛ばす
                while self.base + len(self.frames) < index + 5:
                    self._cond.wait(timeout=1.0)
                    index = max(index, self.base)
                rel = index - self.base
                chunk = [f for _, f, _ in self.frames[rel:rel + 5]]
            index += 5
            yield np.concatenate(chunk)

    def listen(self, from_barge_in: bool = False) -> np.ndarray | None:
        """1 回分の発話を録る。割り込みの直後なら、検出の少し前から録る（話し始めを落とさない）。"""
        noise = self.gate.noise if self.gate is not None else self.noise_floor()
        threshold = max(rec.MIN_THRESHOLD, noise * rec.THRESHOLD_FACTOR)
        with self._cond:
            barge = from_barge_in and self.barge_index is not None
            start = self.barge_index if barge else max(self.base, self.base + len(self.frames) - 15)
            self.barge_index = None
        # 割り込みの後は、判定がすでに人の声と確かめているので、短い相づち（「うん」「はい」）も録る
        audio = rec.segment(self._blocks(start), threshold, preroll_blocks=3,
                            min_speech_seconds=0.1 if barge else rec.MIN_SPEECH_SECONDS)
        return None if audio is None else audio.astype(np.float32) / 32768.0

    def close(self) -> None:
        self.armed = False
        try:
            self._stream.stop()
            self._stream.close()
        except Exception:
            pass
