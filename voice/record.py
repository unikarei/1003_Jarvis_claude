"""マイクで 1 回分の発話を録音する（声での会話、T-1213・T-1404）。

周囲の音から決めたしきい値で話し始めを検出し、無音が続いたら区切る。話し始めの直前（プリロール）も含める。
録音はメモリ上だけに置き、ファイルに保存しない。新しい依存はない（sounddevice・numpy）。
"""

import os
from collections import deque
from collections.abc import Iterable

import numpy as np

SAMPLE_RATE = 16000
BLOCK_SECONDS = 0.1
SILENCE_SECONDS = 0.8        # この長さの無音で区切る（JARVIS_VOICE_SILENCE_SECONDS で変えられる）
MAX_SECONDS = 20             # 1 回の発話の上限
START_TIMEOUT_SECONDS = 8    # 話し始めを待つ上限
MIN_THRESHOLD = 300.0        # int16 の RMS。静かな部屋の周囲の音（約 90）より十分大きい
THRESHOLD_FACTOR = 2.0       # しきい値 = 周囲の音の中央値 × 倍率（JARVIS_VOICE_THRESHOLD_FACTOR で変えられる）
PREROLL_BLOCKS = 3           # 話し始めの直前 0.3 秒も含める（最初の音節が落ちないように）
START_BLOCKS = 2             # しきい値を超えるブロックがこの数続いたら話し始め（「カチッ」では始めない）
RELEASE_RATIO = 0.7          # 発話の途中は、しきい値のこの倍を下回ったブロックだけを無音と数える
MIN_SPEECH_SECONDS = 0.3     # これより短い発話（咳・物音）は捨てる


def _env_float(name: str, default: float) -> float:
    try:
        return float(os.environ.get(name, default))
    except ValueError:
        return default


def rms(block: np.ndarray) -> float:
    return float(np.sqrt(np.mean(block.astype(np.float32) ** 2))) if len(block) else 0.0


def threshold_from(noise_blocks: list[np.ndarray]) -> float:
    """周囲の音（ブロックごとの RMS の中央値）× 倍率（下限 MIN_THRESHOLD）を、話しているかどうかのしきい値にする。
    最大値ではなく中央値にするのは、測っている間の一瞬の物音でしきい値が上がりすぎないように。"""
    levels = [rms(b) for b in noise_blocks]
    floor = float(np.median(levels)) if levels else 0.0
    return max(MIN_THRESHOLD, floor * _env_float("JARVIS_VOICE_THRESHOLD_FACTOR", THRESHOLD_FACTOR))


def segment(blocks: Iterable[np.ndarray], threshold: float, block_seconds: float = BLOCK_SECONDS,
            silence_seconds: float | None = None, max_seconds: float = MAX_SECONDS,
            start_timeout: float = START_TIMEOUT_SECONDS, preroll_blocks: int = PREROLL_BLOCKS,
            start_blocks: int = START_BLOCKS, release_ratio: float = RELEASE_RATIO,
            min_speech_seconds: float = MIN_SPEECH_SECONDS) -> np.ndarray | None:
    """音のブロックの並びから 1 回分の発話を切り出す。話し始めがない・短すぎるときは None。
    時間はブロックの数で数える（0.1 秒の足し算の誤差で区切りが遅れないように）。
    silence_seconds を省くと環境変数 JARVIS_VOICE_SILENCE_SECONDS（既定 0.8 秒）。"""
    if silence_seconds is None:
        silence_seconds = _env_float("JARVIS_VOICE_SILENCE_SECONDS", SILENCE_SECONDS)
    limit_silent = max(1, round(silence_seconds / block_seconds))
    limit_total = round(max_seconds / block_seconds)
    limit_wait = round(start_timeout / block_seconds)
    limit_short = round(min_speech_seconds / block_seconds)
    release = threshold * release_ratio

    before: deque = deque(maxlen=max(0, preroll_blocks))   # 話し始めの前の直近のブロック
    pending: list[np.ndarray] = []                         # しきい値を超えて続いているブロック（話し始めの候補）
    voiced: list[np.ndarray] = []
    speech, silent, waited = 0, 0, 0                       # speech は話し始めからのブロック数
    for block in blocks:
        level = rms(block)
        if not voiced:
            if level >= threshold:
                pending.append(block)
                if len(pending) >= start_blocks:
                    voiced = list(before) + pending
                    speech = len(pending)
                continue
            for b in pending:                              # 続かなかった大きな音は周囲の音として扱う
                before.append(b)
            waited += len(pending) + 1
            pending = []
            before.append(block)
            if waited >= limit_wait:
                return None
            continue
        voiced.append(block)
        speech += 1
        silent = silent + 1 if level < release else 0
        if silent >= limit_silent or speech > limit_total:
            break
    if not voiced or speech - silent < limit_short:
        return None
    return np.concatenate(voiced)


def _blocks_from_microphone(samplerate: int, block_seconds: float):
    import sounddevice as sd

    frames = int(samplerate * block_seconds)
    with sd.InputStream(samplerate=samplerate, channels=1, dtype="int16", blocksize=frames) as stream:
        while True:
            data, _ = stream.read(frames)
            yield data[:, 0].copy()


def record_utterance(samplerate: int = SAMPLE_RATE) -> np.ndarray | None:
    """マイクから 1 回分の発話を録る。最初の 0.3 秒で周囲の音を測ってしきい値を決める。
    戻り値は faster-whisper にそのまま渡せる float32（-1〜1）。話し始めがなければ None。"""
    blocks = _blocks_from_microphone(samplerate, BLOCK_SECONDS)
    noise = [next(blocks) for _ in range(3)]
    audio = segment(blocks, threshold_from(noise))
    blocks.close()
    return None if audio is None else audio.astype(np.float32) / 32768.0


def has_input_device() -> bool:
    import sounddevice as sd

    try:
        return any(d.get("max_input_channels", 0) > 0 for d in sd.query_devices())
    except Exception:
        return False
