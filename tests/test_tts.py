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
    late = "あ" * 70 + "、" + "い" * 10                                  # 最初の読点が 60 字より後（Codex の指摘 3）
    assert tts.split_long(late) == ("あ" * 70 + "、", "い" * 10)
    zenkaku = "売上は１，０００，０００円で" + "あ" * 70                    # 全角のカンマの数字でも切らない
    assert tts.split_long(zenkaku) == ("", zenkaku)
    assert tts.split_long("あ" * 20 + "，" + "い" * 50)[0] == "あ" * 20 + "，"   # 数字でない全角のカンマでは切る
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
    assert synth("文。", threading.Event()) is None                      # say 自体の失敗は None（その文は読まない）


def test_engine_failing_mid_conversation_falls_back_to_say(monkeypatch, fake_voicevox):
    """会話の途中で選んだエンジンが止まっても、その文は say（Kyoko）で読む（Codex の指摘 2）。"""
    engine, _ = fake_voicevox
    monkeypatch.setitem(tts.ENGINES, "voicevox", engine)
    said = []
    monkeypatch.setattr(tts.ENGINES["say"], "synthesize", lambda text, s, cancel=None: said.append(s.speaker) or np.ones(3))
    notes = []
    synth = tts.make_synth(tts.VoiceSettings(engine="voicevox", speaker="13"), notify=notes.append)
    assert len(synth("文。", threading.Event())) == 16000                 # まだ動いている（VOICEVOX の音）
    monkeypatch.setattr(engine, "synthesize", lambda *a, **k: (_ for _ in ()).throw(tts.TTSError("止まった")))
    assert synth("次の文。", threading.Event()) is not None and said == ["Kyoko"]
    assert any("say（Kyoko）で読み上げます" in n for n in notes)
    cancel = threading.Event(); cancel.set()
    assert synth("取り消し。", cancel) is None                           # 取り消しのときは読まない


def test_voice_chat_uses_saved_settings_in_every_mode(settings_path, monkeypatch, capsys):
    """保存した設定は、割り込みあり・なし・押してから話す形のどれでも合成に渡る（実際に main を動かして確かめる）。"""
    import queue as _queue
    import speech_to_text
    import voice_chat
    from voice import record as mic
    tts.save_settings(tts.VoiceSettings(engine="say", speaker="Reed (日本語（日本）)", rate=1.3))
    seen = []
    monkeypatch.setattr(tts, "make_synth", lambda s: seen.append((s.speaker, s.rate)) or (lambda sentence, c: None))

    class FakePlayer:
        def __init__(self, device=None): pass
        def stop(self): pass
        def speaking(self): return False
        def heard(self, t): return []
        def close(self): pass
        def enqueue(self, *a): pass
        def set_turn(self, t): pass
    monkeypatch.setattr(duplex, "Player", FakePlayer)
    monkeypatch.setattr(voice_chat, "init_db", lambda: None)

    def quit_keys():
        q = _queue.Queue(); q.put("q"); return q
    # 押してから話す形・割り込みなしの形
    for kwargs in ({"push_to_talk": True}, {"barge_in": False}):
        assert voice_chat.main(listen=lambda: None, transcribe=lambda a: "", keys=quit_keys(), **kwargs) == 0
    # 割り込みのある形（マイクと音声認識は偽物）
    monkeypatch.setattr(mic, "has_input_device", lambda: True)
    monkeypatch.setattr(speech_to_text, "load_model", lambda: None)
    captured = {}
    monkeypatch.setattr(voice_chat, "run_barge_in", lambda session, transcribe, keys: captured.setdefault("s", session) and 0)
    monkeypatch.setattr(duplex, "Session", lambda player, speaker: type("S", (), {"speaker": speaker})())
    assert voice_chat.main(keys=quit_keys(), barge_in=True) == 0
    assert seen == [("Reed (日本語（日本）)", 1.3)] * 3                    # 3 つの形すべてで同じ設定
    assert isinstance(captured["s"].speaker, duplex.DuplexSpeaker)


def test_unknown_or_paid_engine_in_settings_falls_back(settings_path):
    """設定ファイルに知らない・有料のエンジンがあっても、起動できるように say（Kyoko）に戻す（6 回目の指摘 1）。"""
    for name in ("unknown-engine", "elevenlabs"):
        settings_path.parent.mkdir(parents=True, exist_ok=True)
        settings_path.write_text(json.dumps({"engine": name, "speaker": "x"}))
        assert tts.load_settings() == tts.VoiceSettings()
    settings_path.write_text("[1, 2]")
    assert tts.load_settings() == tts.VoiceSettings()


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



def test_long_chunk_is_split_repeatedly():
    """1 回で届いた長い文も、読点ごとに繰り返し区切る（2 回目の指摘 3）。"""
    played = []

    class P:
        def enqueue(self, turn, s, a): played.append(s)
        def stop(self): pass
        def speaking(self): return False
        def heard(self, t): return []
        def close(self): pass
    sp = duplex.DuplexSpeaker(P(), synth=lambda s, c: np.zeros(10, dtype=np.float32))
    parts = ["一" * 50 + "、", "二" * 50 + "、", "三" * 50 + "、", "四" * 20]
    sp.feed("".join(parts))
    sp.wait()
    assert played == parts[:3]
    sp.flush()
    sp.wait()
    assert played == parts
    sp.close()


def test_http_engine_only_on_this_mac_and_bad_audio(fake_voicevox, monkeypatch):
    """接続先は Mac の中だけ。壊れた音声は TTSError（say に切り替える）（2 回目の指摘 1・2）。"""
    remote = tts.VoicevoxEngine("voicevox", "VOICEVOX", "http://example.com:50021", "テスト")
    ok, reason = remote.available()
    assert not ok and "Mac の中" in reason
    with pytest.raises(tts.TTSError):
        remote.synthesize("文", tts.VoiceSettings(engine="voicevox", speaker="13"))
    engine, _ = fake_voicevox
    monkeypatch.setattr(engine, "_post", lambda path, body=None, timeout=10: b"{}" if "audio_query" in path else b"broken")
    with pytest.raises(tts.TTSError):
        engine.synthesize("文", tts.VoiceSettings(engine="voicevox", speaker="13"))



def test_long_sentence_with_period_in_the_same_chunk_is_split():
    """長い文と句点が 1 回で届いても、読点で区切る（3 回目の指摘 3）。"""
    played = []

    class P:
        def enqueue(self, turn, s, a): played.append(s)
        def stop(self): pass
        def speaking(self): return False
        def heard(self, t): return []
        def close(self): pass
    sp = duplex.DuplexSpeaker(P(), synth=lambda s, c: np.zeros(10, dtype=np.float32))
    first, rest = "一" * 50 + "、", "二" * 30 + "。"
    sp.feed(first + rest)
    sp.wait()
    assert played == [first, rest]
    sp.close()


def test_http_engine_does_not_follow_redirects():
    """ローカルのエンジンが外へ転送しても追わない（3 回目の指摘 1）。"""
    class Redirect(BaseHTTPRequestHandler):
        def log_message(self, *a): pass
        def do_POST(self):
            self.send_response(307); self.send_header("Location", "http://example.com/steal")
            self.send_header("Content-Length", "0"); self.end_headers()
        do_GET = do_POST
    srv = ThreadingHTTPServer(("127.0.0.1", 0), Redirect)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        e = tts.VoicevoxEngine("voicevox", "VOICEVOX", f"http://127.0.0.1:{srv.server_address[1]}", "テスト")
        with pytest.raises(tts.TTSError):
            e.synthesize("秘密の文", tts.VoiceSettings(engine="voicevox", speaker="13"))
    finally:
        srv.shutdown(); srv.server_close()


def test_http_synthesis_can_be_cancelled_mid_request():
    """遅いローカルのエンジンでも、取り消せばすぐ戻る（次の発言の合成を待たせない、3 回目の指摘 2）。"""
    import time as _time

    class Slow(BaseHTTPRequestHandler):
        def log_message(self, *a): pass
        def do_POST(self):
            _time.sleep(3)
            self.send_response(200); self.send_header("Content-Length", "2"); self.end_headers(); self.wfile.write(b"{}")
    srv = ThreadingHTTPServer(("127.0.0.1", 0), Slow)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        e = tts.VoicevoxEngine("voicevox", "VOICEVOX", f"http://127.0.0.1:{srv.server_address[1]}", "テスト")
        cancel = threading.Event()
        threading.Timer(0.2, cancel.set).start()
        started = _time.monotonic()
        with pytest.raises(tts.TTSError):
            e.synthesize("文", tts.VoiceSettings(engine="voicevox", speaker="13"), cancel)
        assert _time.monotonic() - started < 1.0
    finally:
        srv.shutdown(); srv.server_close()



def test_short_first_phrase_still_splits_long_sentence():
    """唯一の読点が先頭の近くでも、長い文は区切る（4 回目の指摘 2）。"""
    text = "はい、" + "あ" * 70
    assert tts.split_long(text) == ("はい、", "あ" * 70)


def test_code_fence_line_with_commas_is_not_spoken():
    """改行の前の長いコードの行（読点あり）を読まず、閉じた後の文は読む（4 回目の指摘 1）。"""
    played = []

    class P:
        def enqueue(self, turn, s, a): played.append(s)
        def stop(self): pass
        def speaking(self): return False
        def heard(self, t): return []
        def close(self): pass
    sp = duplex.DuplexSpeaker(P(), synth=lambda s, c: np.zeros(10, dtype=np.float32))
    sp.feed("```python print('あ' * 70, 'い、う、え、お') " + "x" * 50)
    sp.feed("\nprint(1)\n```\n終わりの文。")
    sp.wait()
    assert played == ["終わりの文。"]
    sp.close()



def test_local_engine_ignores_proxy_and_rejects_odd_query(fake_voicevox, monkeypatch):
    """HTTP_PROXY があってもプロキシを使わない。/audio_query の応答が辞書でなければ TTSError（5 回目の指摘 1・2）。"""
    monkeypatch.setenv("HTTP_PROXY", "http://127.0.0.1:9")              # 使われたら接続に失敗する
    monkeypatch.setenv("NO_PROXY", "")
    engine, _ = fake_voicevox
    assert len(engine.synthesize("文", tts.VoiceSettings(engine="voicevox", speaker="13"))) == 16000
    monkeypatch.setattr(engine, "_post", lambda path, body=None, timeout=10: b"[]")
    with pytest.raises(tts.TTSError):
        engine.synthesize("文", tts.VoiceSettings(engine="voicevox", speaker="13"))


def test_settings_page_rejects_bad_content_length(server):
    """負の・大きすぎる・数でない Content-Length は読まずに断る（5 回目の指摘 3）。"""
    port = server.server_address[1]
    for value in ("-1", "100000", "abc"):
        c = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
        c.putrequest("POST", "/api/settings")
        for k, v in (("Host", f"127.0.0.1:{port}"), ("Cookie", f"jarvis_voice_settings={TOKEN}"),
                     ("X-Jarvis-Console", "1"), ("Content-Length", value)):
            c.putheader(k, v)
        c.endheaders()
        assert c.getresponse().status == 400, value
        c.close()
    assert req(server, "POST", "/api/settings", [1, 2])[0] == 400



def test_settings_with_non_string_engine_fall_back(settings_path):
    """engine が配列・オブジェクトでも起動できるように既定に戻す（7 回目の指摘 1）。"""
    settings_path.parent.mkdir(parents=True, exist_ok=True)
    for value in ([1], {"a": 1}, 3):
        settings_path.write_text(json.dumps({"engine": value}))
        assert tts.load_settings() == tts.VoiceSettings()


def test_odd_speakers_response_is_handled(fake_voicevox, monkeypatch):
    """/speakers の応答の形が違っても例外にせず、話者なし（使えない）とする（7 回目の指摘 2）。"""
    engine, _ = fake_voicevox
    monkeypatch.setattr(engine, "_get", lambda path, timeout=10: {"not": "a list"} if path == "/speakers" else "0.1")
    assert engine.speakers() == []
    monkeypatch.setattr(engine, "_get", lambda path, timeout=10: [1, {"name": "x", "styles": "bad"}] if path == "/speakers" else "0.1")
    assert engine.speakers() == []
    from acc import voice_settings
    monkeypatch.setitem(tts.ENGINES, "voicevox", engine)
    info = {e["name"]: e for e in voice_settings.engines_info()}["voicevox"]
    assert not info["available"] and "話者の一覧を読めません" in info["reason"]


def test_candidates_resolve_installed_voices_and_mark_missing():
    """候補の比較: 入っている話者・スタイルは番号に、入っていない声は理由付きで使えないとする（2026-10-18）。"""
    from acc import voice_settings
    engines = [
        {"name": "aivisspeech", "label": "AivisSpeech", "available": True, "reason": "",
         "speakers": [{"id": "u1", "name": "阿井田 茂", "styles": [{"id": "1310138977", "name": "Calm"}]},
                      {"id": "u2", "name": "まお", "styles": [{"id": "None", "name": "おちつき"}]}]},
        {"name": "voicevox", "label": "VOICEVOX", "available": False, "reason": "VOICEVOX が起動していません", "speakers": []},
        {"name": "say", "label": "say", "available": True, "reason": "",
         "speakers": [{"id": "Kyoko", "name": "Kyoko", "styles": []},
                      {"id": "Otoya（拡張）", "name": "Otoya（拡張）", "styles": []}]},
    ]
    info = {c["label"]: c for c in voice_settings.candidates_info(engines)}
    assert info["AivisSpeech 阿井田 茂（Calm）"]["speaker"] == "1310138977" and info["AivisSpeech 阿井田 茂（Calm）"]["available"]
    assert not info["AivisSpeech まお（おちつき）"]["available"] and "ありません" in info["AivisSpeech まお（おちつき）"]["reason"]
    assert info["VOICEVOX 青山龍星（しっとり）"]["reason"] == "VOICEVOX が起動していません"
    assert info["macOS Otoya（拡張）"]["speaker"] == "Otoya（拡張）"
    assert not info["macOS Kyoko（拡張）"]["available"] and info["macOS Kyoko"]["speaker"] == "Kyoko"
    assert all(not c["engine"] in ("elevenlabs", "openai") for c in voice_settings.candidates_info(engines))


def test_candidates_api_requires_token(server, monkeypatch):
    monkeypatch.setattr(tts.ENGINES["say"], "available", lambda: (True, ""))   # macOS 以外でも同じ結果にする
    assert req(server, "GET", "/api/candidates", cookie=False)[0] == 401
    code, body, _ = req(server, "GET", "/api/candidates")
    cands = json.loads(body)["candidates"]
    assert code == 200 and any(c["label"] == "macOS Kyoko" and c["available"] for c in cands)
