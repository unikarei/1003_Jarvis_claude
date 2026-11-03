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
from voice import tts
from voice.speak import SENTENCE_END, Speaker, speakable

SAMPLE_RATE = 16000
FRAME = 320                    # 20 ms
FRAME_SECONDS = FRAME / SAMPLE_RATE
CONFIRM_SECONDS = 0.12         # 割り込みとみなす声の長さ（物音・回り込みの一瞬で止めない）
WINDOW_SECONDS = 0.16          # この長さの中で CONFIRM_SECONDS 分が声なら割り込み（「ちょっと」の「っ」の間で数え直さない）
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
                 margin: float = ECHO_MARGIN, confirm_seconds: float | None = None,
                 window_seconds: float = WINDOW_SECONDS):
        self.noise, self.coupling, self.factor, self.margin = noise, coupling, factor, margin
        if confirm_seconds is None:
            confirm_seconds = float(os.environ.get("JARVIS_VOICE_BARGE_CONFIRM_SECONDS", CONFIRM_SECONDS))
        self.confirm = max(1, round(confirm_seconds / FRAME_SECONDS))
        self.window = max(self.confirm, round(window_seconds / FRAME_SECONDS))
        self.recent: deque = deque(maxlen=self.window)   # 直近のフレームが声だったか
        self.fired = False
        self.onset_back = 0                                # 割り込みの時、声の始まりは何フレーム前か

    def threshold(self, ref: float) -> float:
        """周囲の音のしきい値と、回り込みの予測 × 余裕の大きいほう。"""
        base = max(rec.MIN_THRESHOLD, self.noise * self.factor)
        return max(base, self.noise + self.margin * self.coupling * ref)

    def update(self, mic: float, ref: float = 0.0) -> bool:
        """直近 window フレームのうち confirm フレームが声なら True（1 回だけ。reset まで再び True にならない）。"""
        self.recent.append(mic >= self.threshold(ref))
        if self.fired or not self.recent[-1] or sum(self.recent) < self.confirm:
            return False
        self.fired = True
        self.onset_back = len(self.recent) - 1 - list(self.recent).index(True)
        return True

    def reset(self) -> None:
        self.recent.clear()
        self.fired = False


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
            outdata[:, 0] = out                   # stop() と同じロックの中で出力を確定する（停止の後に古い音を渡さない）

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
        self._buffers: dict[int, tuple[str, bool]] = {}   # 発言ごとの区切りの途中の文字とコードの区間
        self._pending = 0                                  # キューに入れてから、合成・再生に渡し終えるまでの文の数
        super().__init__(voice=voice, runner=lambda cmd: None)
        # 番号の確認から表示・投入までを 1 つの区間にできるよう、入れ子にできるロックにする（会話の側も使う）
        self._lock = threading.RLock()

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
                try:
                    audio = self._synth(sentence, cancel)
                except Exception:                     # 合成の失敗でスレッドを止めない（その文は読まない）
                    audio = None
                if audio is not None and turn == self.turn and not cancel.is_set():
                    self.player.enqueue(turn, sentence, audio)
            finally:
                with self._lock:
                    self._busy = False
                    self._pending = max(0, self._pending - 1)
                self._queue.task_done()

    # 番号の確認・文の区切りの途中の文字（発言ごと）・キューへの投入は、すべて new_turn と同じロックの中で行う。
    # 割り込みと逐次表示・読み上げの完了が重なっても、古い発言の文字が新しい発言に混ざらない。

    def _put(self, turn: int, text: str) -> None:
        text = speakable(text)
        if text:
            self._pending += 1                    # ロックの中で数える（取り出してから作業中になるまでの隙間をなくす）
            self._queue.put((turn, text))

    def say(self, text: str, turn: int | None = None) -> None:
        """1 文を読む。turn を渡したら、その番号が今の発言のときだけ。"""
        with self._lock:
            if turn is not None and turn != self.turn:
                return
            self._put(self.turn if turn is None else turn, text)

    def feed(self, chunk: str, turn: int | None = None) -> None:
        """逐次表示の差分を文に区切って読む。turn を渡したら、その番号の発言としてだけ読む。"""
        with self._lock:
            turn = self.turn if turn is None else turn
            if turn != self.turn:
                return
            buffer, in_code = self._buffers.get(turn, ("", False))
            parts = SENTENCE_END.split(buffer + chunk)
            buffer = parts.pop()
            for part in parts:
                if part.strip().startswith("```"):
                    in_code = not in_code
                    continue
                if not in_code:
                    while True:                  # 句点まで届いた長い文も、読点の切れ目で区切って読む
                        head, part = tts.split_long(part)
                        if not head:
                            break
                        self._put(turn, head)
                    self._put(turn, part)
            # コードの区間の始まり（```）かもしれない行は区切らない（改行まで待って区間を判定する）
            while not in_code and not buffer.lstrip().startswith("`"):
                head, buffer = tts.split_long(buffer)   # 句点まで長い文は、読点の切れ目で先に読み始める（繰り返し）
                if not head:
                    break
                self._put(turn, head)
            self._buffers[turn] = (buffer, in_code)

    def flush(self, turn: int | None = None) -> None:
        """区切りの途中の残りを読む（その番号が今の発言のときだけ）。"""
        with self._lock:
            turn = self.turn if turn is None else turn
            buffer, in_code = self._buffers.pop(turn, ("", False))
            if turn == self.turn and buffer and not in_code:
                self._put(turn, buffer)

    def new_turn(self) -> int:
        """新しい発言を始める（古い発言の合成・未再生の音・区切りの途中の文字を捨てる）。
        番号の更新から再生の停止までを 1 つのロックの区間で行い、同時に呼ばれても番号が戻らない。"""
        with self._lock:
            self.turn += 1
            turn = self.turn
            self._buffers.clear()
            if hasattr(self.player, "set_turn"):
                self.player.set_turn(turn)        # 再生の側でも古い番号の音を受け付けない
            self.stop()
        return turn

    def stop(self) -> None:
        import queue as _queue
        with self._lock:
            self._buffers.clear()
            self._cancel.set()
            try:
                while True:
                    self._queue.get_nowait()
                    self._queue.task_done()
                    self._pending = max(0, self._pending - 1)
            except _queue.Empty:
                pass
        self.player.stop()

    def synthesizing(self) -> bool:
        """合成を待っている・合成している文があれば True（再生の側は見ない）。"""
        with self._lock:
            return self._busy or self._pending > 0

    def speaking(self) -> bool:
        return self.synthesizing() or self.player.speaking()

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

    PREROLL_FRAMES = 3         # 割り込みの声の始まり（判定の最初のフレーム）の 60 ms 前から録る。それより前は
                               # JARVIS の声の回り込みが混ざりうるので含めない

    def __init__(self, player: Player, speaker: DuplexSpeaker, device: str | None = None,
                 stream_factory: Callable | None = None):
        self.player, self.speaker = player, speaker
        self._cond = threading.Condition()
        self.frames: list[tuple[float, np.ndarray, float]] = []   # (時刻, 20 ms の音, 音量)
        self.base = 0                                              # frames[0] の通し番号（古い音を捨てた数）
        self.gate: EchoGate | None = None
        self.delay = 0.0
        self.armed = False
        self._arm_lock = threading.Lock()
        self._arm_turn: int | None = None          # arm したときの発言の番号（割り込みはこの発言だけを止める）
        self._arm_gen = 0                          # arm のたびに進める（判定の途中で arm し直されたら確定しない）
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
        # 判定・録音の開始位置の確定・停止（聞いた文の整理を含む）・通知を、すべて arm のロックの中でこの順に行う。
        # 会話の側は disarm（同じロック）を待ち合わせにして、割り込みを確定し終えた状態だけを見る
        with self._arm_lock:
            if not self.armed or self.gate is None:
                return
            ref = self.player.level(now - self.delay - 0.04, now - self.delay + 0.04)
            if not self.gate.update(level, ref):
                return
            if self._arm_turn != getattr(self.speaker, "turn", self._arm_turn):
                self.gate.reset()                 # 発言が変わっていたら確定しない（判定はやり直し）
                return
            self.armed = False
            with self._cond:
                self.barge_index = max(self.base, index - self.gate.onset_back - self.PREROLL_FRAMES)
                onset = self.frames[max(0, index - self.gate.onset_back - self.base)][0]
            # 止める（目標 200 ms 以内）。番号も進め、検出の後に届く古い発言の文・生成をすぐ捨てさせる
            self.speaker.new_turn()
            stopped = time.monotonic()
            self.stop_latency.append(stopped - now)
            out_ms = round(getattr(self.player, "output_latency", 0.0) * 1000)
            self.events.append({"detect_ms": round((now - onset) * 1000 + FRAME_SECONDS * 1000),
                                "stop_call_ms": round((stopped - now) * 1000, 1), "output_ms": out_ms})
            self.barged.set()                     # 最後に知らせる

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
        """考え中・読み上げ中に割り込みを受け付ける（今の発言の番号に対してだけ）。"""
        with self._arm_lock:
            if self.gate is not None:
                self.gate.reset()
            self.barged.clear()
            self._arm_turn = getattr(self.speaker, "turn", None)
            self._arm_gen += 1
            self.armed = True

    def disarm(self, turn: int | None = None) -> None:
        """割り込みを受け付けない。turn を渡したら、その発言に対して arm しているときだけ。"""
        with self._arm_lock:
            if turn is None or self._arm_turn == turn:
                self.armed = False
                self._arm_gen += 1

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
            if barge:
                start = self.barge_index
            else:
                # 普通の聞き取りは、JARVIS の声がマイクに届き終わった後から（直前 0.3 秒に自分の声を含めない）
                quiet = getattr(self.player, "_last_sound", 0.0) + getattr(self.player, "output_latency", 0.0) \
                    + self.delay
                start = max(self.base, self.base + len(self.frames) - 15)
                for i in range(len(self.frames) - 1, -1, -1):
                    if self.frames[i][0] < quiet:
                        start = max(start, self.base + i + 1)
                        break
            self.barge_index = None
        # 割り込みの後は、判定がすでに人の声と確かめているので、短い相づち（「うん」「はい」）も録る
        audio = rec.segment(self._blocks(start), threshold, preroll_blocks=0 if barge else 3,
                            min_speech_seconds=0.1 if barge else rec.MIN_SPEECH_SECONDS)
        return None if audio is None else audio.astype(np.float32) / 32768.0

    def close(self) -> None:
        self.armed = False
        try:
            self._stream.stop()
            self._stream.close()
        except Exception:
            pass
