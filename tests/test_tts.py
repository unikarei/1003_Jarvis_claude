"""読み上げの音声エンジンと設定（ADR-0029、T-1408）のテスト。say・VOICEVOX は偽物。有料の API は呼ばない。"""

import http.client
import io
import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import numpy as np
import pytest
import soundfile as sf

from voice import duplex, tts


@pytest.fixture
def settings_path(tmp_path, monkeypatch):
    p = tmp_path / "voice" / "settings.json"
    monkeypatch.setattr(tts, "SETTINGS_PATH", p)
    return p


def test_settings_roundtrip_and_clamp(settings_path):
    assert tts.load_settings() == tts.VoiceSettings()            # ファイルがなければ既定（say・Kyoko）
    tts.save_settings(tts.VoiceSettings(engine="voicevox", speaker="13", rate=5, intonation=-1, pitch=0.5))
    s = tts.load_settings()
    assert (s.engine, s.speaker, s.rate, s.intonation, s.pitch) == ("voicevox", "13", 2.0, 0.0, 0.15)
    settings_path.write_text('{"engine": "say", "unknown": 1, "rate": 1.2}')
    assert tts.load_settings().rate == 1.2
    settings_path.write_text("壊れた")
    assert tts.load_settings() == tts.VoiceSettings()


def test_resample_to_16k():
    audio = np.zeros(24000, dtype=np.float32)
    assert len(tts.resample(audio, 24000)) == 16000
    assert tts.resample(np.zeros((10, 2)), 16000).shape == (10,)


def test_split_long_sentence_only_at_commas():
    assert tts.split_long("短い文") == ("", "短い文")
    text = "大洗の物件については、所在地と駐車場と補助金の三つを確認しましたが、届出に必要な図面の準備がまだ終わっておらず" + "あ" * 20
    head, rest = tts.split_long(text)
    assert head.endswith("、") and head + rest == text and len(head) <= tts.LONG_SENTENCE
    no_comma = "あ" * 80
    assert tts.split_long(no_comma) == ("", no_comma)
    numbers = "売上は1,000,000円で" + "あ" * 70                     # 半角のカンマ（数字）では切らない
    assert tts.split_long(numbers) == ("", numbers)


def test_speaker_reads_long_sentence_before_the_period():
    """句点まで長い文は、読点の切れ目で先に読み始める（最初の声を早く。語の途中では切らない）。"""
    played = []

    class P:
        def enqueue(self, turn, s, a): played.append(s)
        def stop(self): pass
        def speaking(self): return False
        def heard(self, t): return []
        def close(self): pass
    sp = duplex.DuplexSpeaker(P(), synth=lambda s, c: np.zeros(10, dtype=np.float32))
    first = "大洗の物件については、所在地と駐車場と補助金の三つを確認しましたが、"
    rest = "消防の届出に必要な図面と書類の準備がまだ終わっておらず来月までかかる見込みで"
    assert len(first + rest) > tts.LONG_SENTENCE
    sp.feed(first + rest)
    sp.wait()
    assert played == [first]                                   # 句点の前でも、読点の切れ目で先に読む
    sp.feed("す。")
    sp.wait()
    assert played[-1] == rest + "す。"
    sp.close()


def test_say_speakers_are_parsed(monkeypatch):
    out = ("Kyoko               ja_JP    # こんにちは! 私の名前はKyokoです。\n"
           "Eddy (日本語（日本）)      ja_JP    # こんにちは!\n"
           "Alex                en_US    # Hello\n")
    monkeypatch.setattr(tts.subprocess, "run", lambda *a, **k: type("R", (), {"stdout": out})())
    assert [v["id"] for v in tts.SayEngine().speakers()] == ["Kyoko", "Eddy (日本語（日本）)"]


# ---------------------------------------------------------------- VOICEVOX・AivisSpeech（偽の HTTP エンジン）

@pytest.fixture
def fake_voicevox():
    seen = {}

    class H(BaseHTTPRequestHandler):
        def log_message(self, *a): pass
        def _out(self, code, body, ctype="application/json"):
            self.send_response(code); self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body))); self.end_headers(); self.wfile.write(body)
        def do_GET(self):
            if self.path == "/version":
                return self._out(200, b'"0.1.0"')
            if self.path == "/speakers":
                return self._out(200, json.dumps([{"name": "テスト話者", "speaker_uuid": "u1",
                                                   "styles": [{"id": 13, "name": "ノーマル"}]}]).encode())
            self._out(404, b"{}")
        def do_POST(self):
            length = int(self.headers.get("Content-Length") or 0)
            body = self.rfile.read(length)
            if self.path.startswith("/audio_query"):
                seen["query_path"] = self.path
                return self._out(200, json.dumps({"speedScale": 1.0, "intonationScale": 1.0, "pitchScale": 0.0}).encode())
            if self.path.startswith("/synthesis"):
                seen["query"] = json.loads(body)
                buf = io.BytesIO()
                sf.write(buf, np.zeros(24000, dtype=np.float32), 24000, format="WAV")
                return self._out(200, buf.getvalue(), "audio/wav")
            self._out(404, b"{}")
    srv = ThreadingHTTPServer(("127.0.0.1", 0), H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    yield tts.VoicevoxEngine("voicevox", "VOICEVOX", f"http://127.0.0.1:{srv.server_address[1]}", "テスト"), seen
    srv.shutdown(); srv.server_close()


def test_voicevox_engine_synthesizes_with_settings(fake_voicevox):
    engine, seen = fake_voicevox
    assert engine.available() == (True, "")
    assert engine.speakers() == [{"id": "u1", "name": "テスト話者", "styles": [{"id": "13", "name": "ノーマル"}]}]
    audio = engine.synthesize("こんにちは", tts.VoiceSettings(engine="voicevox", speaker="13", rate=1.3,
                                                             intonation=1.5, pitch=0.05))
    assert len(audio) == 16000 and audio.dtype == np.float32            # 24 kHz → 16 kHz
    assert seen["query"] == {"speedScale": 1.3, "intonationScale": 1.5, "pitchScale": 0.05}
    assert "speaker=13" in seen["query_path"]
    with pytest.raises(tts.TTSError):
        engine.synthesize("こんにちは", tts.VoiceSettings(engine="voicevox", speaker="Kyoko"))


def test_unavailable_local_engine_and_paid_engines():
    down = tts.VoicevoxEngine("aivisspeech", "AivisSpeech", "http://127.0.0.1:9", "テスト")
    ok, reason = down.available()
    assert not ok and "起動していません" in reason
    for name in ("elevenlabs", "openai"):
        ok, reason = tts.engine(name).available()
        assert not ok and "Owner の承認" in reason and tts.engine(name).paid


def test_make_synth_falls_back_to_say(monkeypatch):
    """選んだエンジンが使えなければ say（Kyoko）で読む。会話は止めない。"""
    calls = []
    monkeypatch.setattr(tts.ENGINES["say"], "synthesize", lambda text, s, cancel=None: calls.append(s) or np.zeros(5))
    notes = []
    synth = tts.make_synth(tts.VoiceSettings(engine="elevenlabs", speaker="x"), notify=notes.append)
    assert synth("文。", threading.Event()) is not None
    assert calls[0].engine == "say" and calls[0].speaker == "Kyoko" and "使えません" in notes[0]
    monkeypatch.setattr(tts.ENGINES["say"], "synthesize",
                        lambda *a, **k: (_ for _ in ()).throw(tts.TTSError("x")))
    assert synth("文。", threading.Event()) is None                      # 合成の失敗は None（その文は読まない）


# ---------------------------------------------------------------- 設定と試聴の画面

TOKEN = "voice-token-0123456789"


@pytest.fixture
def server(settings_path, monkeypatch):
    from acc import voice_settings
    monkeypatch.setattr(tts.ENGINES["say"], "synthesize", lambda text, s, cancel=None: np.zeros(1600, dtype=np.float32))
    monkeypatch.setattr(tts.ENGINES["say"], "speakers", lambda: [{"id": "Kyoko", "name": "Kyoko", "styles": []}])
    srv, _ = voice_settings.make_server(port=0, token=TOKEN)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    yield srv
    srv.shutdown(); srv.server_close()


def req(srv, method, path, body=None, cookie=True, csrf=True, host=None):
    port = srv.server_address[1]
    c = http.client.HTTPConnection("127.0.0.1", port, timeout=30)
    h = {"Host": host or f"127.0.0.1:{port}"}
    if cookie:
        h["Cookie"] = f"jarvis_voice_settings={TOKEN}"
    if csrf and method == "POST":
        h["X-Jarvis-Console"] = "1"
    c.request(method, path, body=json.dumps(body) if body is not None else None, headers=h)
    r = c.getresponse()
    data = r.read()
    return r.status, data, r


def test_settings_page_requires_token_and_host(server):
    assert req(server, "GET", "/api/engines", cookie=False)[0] == 401
    assert req(server, "GET", "/api/engines", host="evil:80")[0] == 403
    assert req(server, "POST", "/api/preview", {}, csrf=False)[0] == 403
    code, body, _ = req(server, "GET", "/")
    assert code == 200 and "声の会話の音声" in body.decode()


def test_engines_list_and_preview_wav(server):
    engines = json.loads(req(server, "GET", "/api/engines")[1])["engines"]
    names = {e["name"]: e for e in engines}
    assert names["say"]["available"] and names["say"]["speakers"][0]["id"] == "Kyoko"
    assert not names["elevenlabs"]["available"] and names["elevenlabs"]["paid"]
    code, body, r = req(server, "POST", "/api/preview", {"engine": "say", "speaker": "Kyoko", "text": "テスト"})
    assert code == 200 and r.getheader("Content-Type") == "audio/wav" and body[:4] == b"RIFF"
    assert float(r.getheader("X-Synth-Seconds")) >= 0
    code, body, _ = req(server, "POST", "/api/preview", {"engine": "openai"})
    assert code == 400 and "Owner の承認" in json.loads(body)["error"]


def test_save_settings_and_refuse_paid(server, settings_path):
    code, _, _ = req(server, "POST", "/api/settings", {"engine": "say", "speaker": "Kyoko", "rate": 1.2})
    assert code == 200 and tts.load_settings().rate == 1.2
    code, body, _ = req(server, "POST", "/api/settings", {"engine": "elevenlabs", "speaker": "x"})
    assert code == 400 and tts.load_settings().engine == "say"
    assert json.loads(req(server, "GET", "/api/settings")[1])["rate"] == 1.2
