import importlib

import speech_to_text


def test_default_model_is_large_v3_turbo(monkeypatch):
    monkeypatch.delenv("JARVIS_WHISPER_MODEL", raising=False)
    monkeypatch.delenv("JARVIS_WHISPER_THREADS", raising=False)
    m = importlib.reload(speech_to_text)
    assert (m.WHISPER_MODEL_SIZE, m.WHISPER_THREADS) == ("large-v3-turbo", 8)


def test_model_can_be_switched_back_by_env(monkeypatch):
    monkeypatch.setenv("JARVIS_WHISPER_MODEL", "small")
    monkeypatch.setenv("JARVIS_WHISPER_THREADS", "4")
    m = importlib.reload(speech_to_text)
    assert (m.WHISPER_MODEL_SIZE, m.WHISPER_THREADS) == ("small", 4)
    monkeypatch.delenv("JARVIS_WHISPER_MODEL")
    monkeypatch.delenv("JARVIS_WHISPER_THREADS")
    importlib.reload(speech_to_text)
