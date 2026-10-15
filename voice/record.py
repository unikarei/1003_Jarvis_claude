"""マイクで 1 回分の発話を録音する（声での会話、T-1213）。

Enter で録音を始め、無音が続いたら区切る。音量（RMS）のしきい値は、最初の短い区間の周囲の音から決める。
録音はメモリ上だけに置き、ファイルに保存しない。新しい依存はない（sounddevice・numpy）。
"""

from collections.abc import Iterable

import numpy as np

SAMPLE_RATE = 16000
BLOCK_SECONDS = 0.1
SILENCE_SECONDS = 0.8        # この長さの無音で区切る
MAX_SECONDS = 20             # 1 回の発話の上限
START_TIMEOUT_SECONDS = 8    # 話し始めを待つ上限
MIN_THRESHOLD = 300.0        # int16 の RMS。静かな部屋の周囲の音（約 90）より十分大きい


def rms(block: np.ndarray) -> float:
    return float(np.sqrt(np.mean(block.astype(np.float32) ** 2))) if len(block) else 0.0


def threshold_from(noise_blocks: list[np.ndarray]) -> float:
    """周囲の音の 3 倍（下限 MIN_THRESHOLD）を、話しているかどうかのしきい値にする。"""
    floor = max((rms(b) for b in noise_blocks), default=0.0)
    return max(MIN_THRESHOLD, floor * 3)


def segment(blocks: Iterable[np.ndarray], threshold: float, block_seconds: float = BLOCK_SECONDS,
            silence_seconds: float = SILENCE_SECONDS, max_seconds: float = MAX_SECONDS,
            start_timeout: float = START_TIMEOUT_SECONDS) -> np.ndarray | None:
    """音のブロックの並びから 1 回分の発話を切り出す。話し始めがなければ None。
    時間はブロックの数で数える（0.1 秒の足し算の誤差で区切りが遅れないように）。"""
    limit_silent = round(silence_seconds / block_seconds)
    limit_total = round(max_seconds / block_seconds)
    limit_wait = round(start_timeout / block_seconds)
    voiced, silent, waited = [], 0, 0
    for block in blocks:
        loud = rms(block) >= threshold
        if not voiced:
            if loud:
                voiced.append(block)
            else:
                waited += 1
                if waited >= limit_wait:
                    return None
            continue
        voiced.append(block)
        silent = 0 if loud else silent + 1
        if silent >= limit_silent or len(voiced) > limit_total:
            break
    return np.concatenate(voiced) if voiced else None


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
