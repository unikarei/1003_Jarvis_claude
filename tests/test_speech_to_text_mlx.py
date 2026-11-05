import numpy as np
import pytest

import speech_to_text as stt


class FakeMlx:
    def __init__(self):
        self.calls = []

    def transcribe(self, audio, **kw):
        self.calls.append((len(audio), kw))
        return {"text": " 管理画面を見せて "}


@pytest.fixture
def mlx(monkeypatch):
    fake = FakeMlx()
    monkeypatch.setattr(stt, "_mlx", lambda: fake)
    monkeypatch.setattr(stt, "_backend", None)
    monkeypatch.setattr(stt, "STT_BACKEND", "mlx")
    yield fake
    stt._backend = None


def test_mlx_gets_only_speech_with_vocabulary(mlx, monkeypatch):
    monkeypatch.setattr(stt, "_speech_only", lambda a: a[:8000])
    assert stt.transcribe_array(np.zeros(16000, np.float32)) == "管理画面を見せて"
    n, kw = mlx.calls[0]
    assert n == 8000
    assert kw["initial_prompt"] == stt.VOCABULARY and kw["language"] == "ja"
    assert kw["condition_on_previous_text"] is False and kw["path_or_hf_repo"] == stt.MLX_REPO


def test_mlx_skips_audio_without_speech(mlx, monkeypatch):
    """声のない音は mlx に渡さない（雑音を「ご視聴ありがとうございました」と書き起こすため）。"""
    monkeypatch.setattr(stt, "_speech_only", lambda a: None)
    assert stt.transcribe_array(np.zeros(16000, np.float32)) == ""
    assert mlx.calls == []


def test_speech_only_returns_none_for_silence():
    assert stt._speech_only(np.zeros(32000, np.float32)) is None


@pytest.mark.parametrize("env,expected", [("faster", "faster"), ("mlx", "mlx")])
def test_backend_follows_env(monkeypatch, env, expected):
    monkeypatch.setattr(stt, "_backend", None)
    monkeypatch.setattr(stt, "STT_BACKEND", env)
    assert stt.backend() == expected
    stt._backend = None


def test_auto_falls_back_to_faster_without_mlx(monkeypatch):
    monkeypatch.setattr(stt, "_backend", None)
    monkeypatch.setattr(stt, "STT_BACKEND", "auto")
    monkeypatch.setattr(stt, "_mlx", lambda: None)
    assert stt.backend() == "faster"
    stt._backend = None


def test_auto_uses_mlx_only_on_apple_silicon(monkeypatch):
    monkeypatch.setattr(stt, "_backend", None)
    monkeypatch.setattr(stt, "STT_BACKEND", "auto")
    monkeypatch.setattr(stt, "_mlx", lambda: object())
    monkeypatch.setattr(stt.sys, "platform", "linux")
    assert stt.backend() == "faster"
    stt._backend = None
