"""Local speech probes never modify the conversation or run transcription/tools."""
import io
import json
from types import SimpleNamespace

import numpy as np
import pytest

from voice.web import noise, server
from voice.web.session import AudioError
from tests.test_voice_web import wav, served, call


def test_real_silero_rejects_silence_and_constant_noise():
    assert noise.speech_check(wav(.8, 16000, value=0), 16000) is False
    assert noise.speech_check(wav(.8, 16000, value=1000), 16000) is False


def test_probe_duration_and_gate():
    with pytest.raises(AudioError) as error:
        noise.speech_check(wav(1.01, 16000), 16000)
    assert error.value.status == 413
    assert noise._GATE.acquire(False)
    try:
        with pytest.raises(noise.CheckBusy):
            noise.speech_check(wav(.5, 16000), 16000)
    finally:
        noise._GATE.release()


def handler(data, query, session):
    """Exercise the actual endpoint without opening a socket."""
    outcomes = []
    cls = server.make_handler('unused', SimpleNamespace(get=lambda _: session), 1)
    fake = SimpleNamespace(
        _length=lambda: len(data), headers={'Content-Type': 'audio/wav'}, rfile=io.BytesIO(data),
        _json=lambda status, body: outcomes.append((status, body)))
    cls._speech_check(fake, query)
    return outcomes[-1]


def test_probe_is_read_only_and_checks_turn_twice(monkeypatch):
    calls = []
    active = [True, True]
    session = SimpleNamespace(can_check_speech=lambda turn: calls.append(turn) or active.pop(0))
    monkeypatch.setattr(noise, 'speech_check', lambda data, rate: True)
    assert handler(wav(), {'session':'sid','turn':'7','rate':'48000'}, session) == (200, {'speech':True})
    assert calls == [7, 7]
    active[:] = [True, False]
    assert handler(wav(), {'turn':'7','rate':'48000'}, session)[0] == 409


@pytest.mark.parametrize('kind,status', [('busy',429),('failure',503),('invalid',400)])
def test_probe_failures_are_safe(monkeypatch, kind, status):
    def check(*args):
        raise {'busy':noise.CheckBusy(), 'failure':RuntimeError('private'),
               'invalid':AudioError(400,'private')}[kind]
    monkeypatch.setattr(noise,'speech_check',check)
    session=SimpleNamespace(can_check_speech=lambda turn: True)
    result=handler(wav(), {'turn':'1','rate':'48000'}, session)
    assert result[0] == status
    assert 'private' not in json.dumps(result)


def test_stale_probe_does_not_classify(monkeypatch):
    monkeypatch.setattr(noise,'speech_check',lambda *args: pytest.fail('stale classification'))
    assert handler(wav(), {'turn':'1','rate':'48000'}, None)[0] == 409
    assert handler(b'x' * (256 * 1024 + 1), {}, None)[0] == 413


def test_probe_uses_existing_http_authentication(served):
    port, fake, made = served
    path = '/api/speech-check?session=invalid&turn=1&rate=16000'
    for kwargs in ({'cookie':False},{'origin':False},{'csrf':False}):
        assert call(port,'POST',path,body=wav(.5,16000),
                    headers={'Content-Type':'audio/wav'},**kwargs)[0].status == 403
    assert call(port,'POST',path,body=wav(.5,16000),
                headers={'Content-Type':'audio/wav'})[0].status == 409
    assert not made
