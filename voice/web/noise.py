"""State-free, bounded local speech confirmation for provisional interruptions."""
import threading
from voice.web.session import AudioError, decode_wav
from voice import tts

_GATE = threading.BoundedSemaphore(1)


class CheckBusy(Exception):
    pass


def speech_check(data: bytes, rate: int) -> bool:
    audio = decode_wav(data, rate)
    if len(audio) > rate:
        raise AudioError(413, "speech check exceeds one second")
    if not _GATE.acquire(blocking=False):
        raise CheckBusy()
    try:
        from faster_whisper.vad import VadOptions, get_speech_timestamps
        samples = tts.resample(audio, rate)
        return bool(get_speech_timestamps(samples, VadOptions(
            threshold=0.65, min_speech_duration_ms=120,
            min_silence_duration_ms=80, speech_pad_ms=0)))
    finally:
        _GATE.release()
