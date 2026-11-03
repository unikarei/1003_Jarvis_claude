"""ブラウザで再生する読み上げの音（T-1502、../docs/browser-voice-spec.md BV-05・BV-08）。

voice.duplex.Player の代わりに voice.duplex.DuplexSpeaker に渡す。同じメソッドの形（enqueue・set_turn・stop・
speaking・heard・close）を持ち、音を自分では鳴らさない。合成した 1 文を WAV（メモリの中）にして推測しにくい ID を
付けて保持し、イベント audio でブラウザに知らせる。ブラウザが再生を終えた（/api/played の end）文だけを
「聞いた文」にする。sounddevice は使わない。音声はファイル・ログに残さず、再生を終えた・捨てた音はすぐ消す。
"""

import io
import secrets
import threading
import time
import wave
from collections import OrderedDict
from collections.abc import Callable

import numpy as np

SAMPLE_RATE = 16000
MAX_ITEMS = 64                     # 会話ごとに保持する文の数の上限
MAX_BYTES = 20 * 1024 * 1024       # 会話ごとに保持する WAV の合計の上限
GRACE_SECONDS = 5.0                # 文の長さ ＋ これを過ぎても「終えた」の知らせがなければ、聞いていない文にする
KEEP_HEARD_TURNS = 4               # 聞いた文を覚えておく発言の数（古い発言の分は捨てる）
EVENTS = ("start", "end", "error")


def to_wav(audio: np.ndarray, rate: int = SAMPLE_RATE) -> bytes:
    """float32（-1〜1）を PCM 16 bit・モノラルの WAV にする。"""
    pcm = (np.clip(np.asarray(audio, dtype=np.float32), -1.0, 1.0) * 32767).astype("<i2")
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(rate)
        w.writeframes(pcm.tobytes())
    return buf.getvalue()


class _Item:
    __slots__ = ("id", "turn", "index", "text", "wav", "seconds", "ready_at")

    def __init__(self, id_, turn, index, text, wav, seconds):
        self.id, self.turn, self.index, self.text, self.wav, self.seconds = id_, turn, index, text, wav, seconds
        self.ready_at: float | None = None     # ブラウザが再生を始められる（始めた）時刻。待つ期限の起点


class BrowserPlayer:
    """emit(type, turn, **data) にイベントを渡す（WebSession の送り方）。clock は待つ期限の時計（テストで差し替える）。"""

    def __init__(self, emit: Callable | None = None, clock: Callable[[], float] = time.monotonic,
                 max_items: int = MAX_ITEMS, max_bytes: int = MAX_BYTES, grace: float = GRACE_SECONDS):
        self.emit = emit if emit is not None else (lambda type_, turn, **data: None)
        self.clock = clock
        self.max_items, self.max_bytes, self.grace = max_items, max_bytes, grace
        self.turn: int | None = None
        self.closed = False
        self._lock = threading.Lock()
        self._items: OrderedDict[str, _Item] = OrderedDict()   # まだ「終えた」の知らせがない文（送った順）
        self._bytes = 0
        self._next_index: dict[int, int] = {}
        self._heard: dict[int, dict[int, str]] = {}            # 発言の番号 → {順番: 文}

    # ------------------------------------------------------------ Player と同じ形

    def enqueue(self, turn: int, sentence: str, audio: np.ndarray) -> None:
        wav = to_wav(audio)
        seconds = round(len(audio) / SAMPLE_RATE, 3)
        with self._lock:
            if self.closed or (self.turn is not None and turn != self.turn):
                return                         # 今の発言の番号の音だけ
            index = self._next_index.get(turn, 0)
            self._next_index[turn] = index + 1
            item = _Item(secrets.token_urlsafe(16), turn, index, sentence, wav, seconds)
            if not self._items:
                item.ready_at = self.clock()   # 前に待っている文がなければ、すぐ再生を始められる
            self._items[item.id] = item
            self._bytes += len(wav)
            while self._items and (len(self._items) > self.max_items or self._bytes > self.max_bytes):
                self._drop(next(iter(self._items)))            # 古いものから捨てる
            kept = item.id in self._items
        if kept:
            self.emit("audio", turn, id=item.id, index=index, text=sentence, seconds=seconds)

    def set_turn(self, turn: int) -> None:
        """この番号の音だけを受け付け、それ以外の未再生の音を捨てる。"""
        with self._lock:
            self.turn = turn
            for old in [t for t in self._heard if t < turn - KEEP_HEARD_TURNS]:
                del self._heard[old]
            for old in [t for t in self._next_index if t != turn]:
                del self._next_index[old]
        self.stop()

    def stop(self) -> None:
        """未再生の音を捨てる（保持した WAV を消す）。再生し終えた文はそのまま聞いた文に残す。"""
        with self._lock:
            self._items.clear()
            self._bytes = 0

    def speaking(self) -> bool:
        """送った文のうち、ブラウザが「終えた」と知らせていないものがあれば True。
        文ごとに「長さ ＋ 5 秒」を過ぎても知らせがなければ、聞いていない文として待つのをやめる。"""
        with self._lock:
            now = self.clock()
            while self._items:
                first = next(iter(self._items.values()))
                if first.ready_at is None:
                    first.ready_at = now
                deadline = first.ready_at + first.seconds + self.grace
                if now <= deadline:
                    return True
                self._drop(first.id, at=deadline)
            return False

    def heard(self, turn: int) -> list[str]:
        """その発言で、ブラウザが再生し終えた文（順番どおり）。"""
        with self._lock:
            done = self._heard.get(turn, {})
            return [done[i] for i in sorted(done)]

    def close(self) -> None:
        with self._lock:
            self.closed = True
        self.stop()

    # ------------------------------------------------------------ ブラウザから

    def audio(self, audio_id: str) -> bytes | None:
        """保持している文の WAV（なければ None）。"""
        with self._lock:
            item = self._items.get(audio_id)
            return item.wav if item is not None else None

    def played(self, turn: int, audio_id: str, event: str) -> tuple[str, int | None]:
        """ブラウザの再生の知らせ。返り値は（"ok" / "stale"（別の発言の番号）/ "unknown"（保持していない ID）, 順番）。
        start: 待つ期限の起点をその時刻にする。end: 聞いた文にして音を消す。error: 聞いていない文として音を消す。"""
        if event not in EVENTS:
            raise ValueError(f"未知の知らせです: {event}")
        with self._lock:
            if self.closed or turn != self.turn:
                return "stale", None
            item = self._items.get(audio_id)
            if item is None or item.turn != turn:
                return "unknown", None
            now = self.clock()
            if event == "start":
                item.ready_at = now
            else:
                if event == "end":
                    self._heard.setdefault(turn, {})[item.index] = item.text
                self._drop(audio_id, at=now)
            return "ok", item.index

    def held(self) -> tuple[int, int]:
        """保持している（文の数, WAV の合計バイト）。"""
        with self._lock:
            return len(self._items), self._bytes

    # ------------------------------------------------------------ 内部

    def _drop(self, audio_id: str, at: float | None = None) -> None:
        """音を消す（ロックの中で呼ぶ）。先頭の文が消えたら、次の文の待つ期限の起点を at（なければ今）にする。"""
        item = self._items.pop(audio_id, None)
        if item is None:
            return
        self._bytes -= len(item.wav)
        item.wav = b""
        if self._items:
            nxt = next(iter(self._items.values()))
            if nxt.ready_at is None:
                nxt.ready_at = self.clock() if at is None else at
