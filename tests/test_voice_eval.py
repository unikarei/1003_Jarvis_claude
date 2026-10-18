"""実マイクの評価の画面（ADR-0027、T-1406）のテスト。マイク・認識・LLM・音声は偽物。"""

import http.client
import json
import tempfile
import threading
import time

import numpy as np
import pytest

import llm
import orchestrator
import skills
from acc import voice_eval
from voice import duplex
from voice import record as mic

TOKEN = "voice-eval-token-0123456789"
R1003 = "repo-1003-jarvis-claude"


def _s(desc, permission="read", phrases=(), inputs=None):
    return {"description": desc, "status": "active", "permission": permission,
            "entry": {"repo": R1003, "function": "x"}, "inputs": inputs or {}, "phrases": list(phrases)}


QUESTION = {"question": {"type": "string", "required": True}}
INDEX = {
    "knowledge-answer": _s("出典付きで答える", inputs=QUESTION),
    "general-chat": _s("通常の会話", inputs=QUESTION),
    "system-status": _s("構成と実態の照合", phrases=["システムの状態"]),
    "knowledge-capture": _s("会話をナレッジ化する", "local_write", ["ナレッジ化して"]),
}


@pytest.fixture
def index(tmp_path, monkeypatch):
    path = tmp_path / "skills.json"
    path.write_text(json.dumps({"skills": INDEX}, ensure_ascii=False), encoding="utf-8")
    monkeypatch.setenv("JARVIS_SKILLS_JSON", str(path))
    skills.reset_cache()
    yield
    skills.reset_cache()


@pytest.fixture
def tmpdir_watch(tmp_path, monkeypatch):
    """一時ファイルの置き場を空のフォルダにする（音声のファイルが作られないことを確かめる）。"""
    folder = tmp_path / "tmp"
    folder.mkdir()
    monkeypatch.setattr(tempfile, "tempdir", str(folder))
    return folder


def _blocks(level: int) -> np.ndarray:
    return np.full(int(mic.SAMPLE_RATE * mic.BLOCK_SECONDS), level, dtype=np.int16)


@pytest.fixture
def fake_mic(monkeypatch):
    """周囲の音 0.3 秒 → 声 1 秒 → 無音（決まった配列）。認識は決まった文を返す。"""
    heard = {"text": ""}

    def blocks(samplerate, block_seconds):
        for _ in range(3):
            yield _blocks(50)
        for _ in range(10):
            yield _blocks(3000)
        while True:
            yield _blocks(50)
    monkeypatch.setattr(mic, "_blocks_from_microphone", blocks)
    monkeypatch.setattr(mic, "has_input_device", lambda: True)
    import speech_to_text
    monkeypatch.setattr(speech_to_text, "transcribe_array", lambda audio, language="ja": heard["text"])
    return heard


class FakeOut:
    """出力の機器の代わり。5 ms ごとに再生のコールバックを呼ぶ。"""

    latency = 0.0

    def __init__(self, cb):
        self.cb, self.running = cb, False

    def start(self):
        self.running = True
        threading.Thread(target=self._run, daemon=True).start()

    def _run(self):
        while self.running:
            self.cb(np.zeros((duplex.FRAME, 1), dtype=np.float32), duplex.FRAME, None, None)
            time.sleep(0.005)

    def stop(self):
        self.running = False

    def close(self):
        pass


@pytest.fixture
def fake_speaker(monkeypatch):
    import voice_chat
    said = []

    def synth(sentence, cancel):
        said.append(sentence)
        return np.zeros(1600, dtype=np.float32)
    built = []

    def build(*args, **kwargs):
        sp = duplex.DuplexSpeaker(duplex.Player(stream_factory=FakeOut), synth=synth)
        built.append(sp)
        return sp
    monkeypatch.setattr(voice_chat, "build_speaker", build)
    return said, built


@pytest.fixture
def server(tmp_path):
    evaluator = voice_eval.Evaluator(folder=tmp_path / "eval")
    srv, _ = voice_eval.make_server(port=0, token=TOKEN, evaluator=evaluator)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    srv.evaluator = evaluator
    yield srv
    srv.shutdown()
    srv.server_close()


def req(srv, method, path, body=None, cookie=True, csrf=True, host=None, origin=None, raw=None):
    port = srv.server_address[1]
    c = http.client.HTTPConnection("127.0.0.1", port, timeout=30)
    h = {"Host": host or f"127.0.0.1:{port}"}
    if cookie:
        h["Cookie"] = f"jarvis_voice_eval={TOKEN}"
    if csrf and method == "POST":
        h["X-Jarvis-Console"] = "1"
    if origin:
        h["Origin"] = origin
    data = raw if raw is not None else (json.dumps(body) if body is not None else None)
    c.request(method, path, body=data, headers=h)
    r = c.getresponse()
    return r.status, r.read(), r


# ---------------------------------------------------------------- 守り

def test_requires_token_host_and_csrf(server):
    port = server.server_address[1]
    assert req(server, "GET", "/api/results", cookie=False)[0] == 401
    assert req(server, "GET", "/api/results", host="evil:80")[0] == 403
    assert req(server, "POST", "/api/measure", {"index": 1}, csrf=False)[0] == 403
    assert req(server, "POST", "/api/measure", {"index": 1}, origin="http://evil.example")[0] == 403
    assert req(server, "POST", "/api/measure", raw="x" * (voice_eval.MAX_BODY + 1))[0] == 400
    code, _, r = req(server, "GET", f"/?token={TOKEN}", cookie=False)
    assert code == 303 and "HttpOnly" in r.getheader("Set-Cookie")
    assert req(server, "GET", "/?token=wrong", cookie=False)[0] == 401
    code, body, _ = req(server, "GET", "/")
    assert code == 200 and "実マイクの評価" in body.decode() and "静かな場所" in body.decode()
    sentences = json.loads(req(server, "GET", "/api/sentences")[1])["sentences"]
    assert [s["text"] for s in sentences] == list(voice_eval.EVAL_SENTENCES) and len(sentences) == 10
    assert port == server.server_address[1]


def test_bad_index_and_mode(server):
    assert req(server, "POST", "/api/measure", {"index": 0, "mode": "stt"})[0] == 400
    assert req(server, "POST", "/api/measure", {"index": 11, "mode": "stt"})[0] == 400
    assert req(server, "POST", "/api/measure", {"index": "1", "mode": "stt"})[0] == 400
    assert req(server, "POST", "/api/measure", {"index": 1, "mode": "voice"})[0] == 400


# ---------------------------------------------------------------- CER

def test_normalize_and_cer():
    assert voice_eval.normalize("システムの　状態は？ ") == "システムの状態は"
    assert voice_eval.normalize("ＡＩニュース、１２３！") == "AIニュース123"       # NFKC
    assert voice_eval.edit_distance("かきくけ", "かくけこ") == 2
    assert voice_eval.cer("システムの状態は？", "システムの状態は。") == 0.0
    assert voice_eval.cer("システムの状態は？", "システムの常態は") == round(1 / 8, 4)
    assert voice_eval.cer("今の会話をナレッジ化して", "") == 1.0
    assert voice_eval.cer("あい", "あいうえ") == 1.0                               # 挿入も数える


# ---------------------------------------------------------------- 計測

def test_measure_stt_returns_cer_and_stages_without_audio_files(server, fake_mic, tmpdir_watch, tmp_path):
    fake_mic["text"] = "システムの常態は？"
    code, body, _ = req(server, "POST", "/api/measure", {"index": 8, "mode": "stt"})
    data = json.loads(body)
    assert code == 200 and data["index"] == 8 and data["text"] == "システムの常態は？"
    assert data["cer"] == round(1 / 8, 4)
    assert set(data["stages"]) == {"segment", "stt"} and data["executed"] is False
    assert data["skill"] is None
    assert list(tmpdir_watch.iterdir()) == []                                  # 音声の一時ファイルもない
    files = [p.name for p in (tmp_path / "eval").iterdir()]
    assert len(files) == 1 and files[0].endswith(".json")
    saved = json.loads((tmp_path / "eval" / files[0]).read_text(encoding="utf-8"))
    assert set(saved["items"][0]) == set(voice_eval.SAVED_KEYS)                 # 音声は含めない
    assert saved["summary"]["count"] == 1 and saved["summary"]["p50_ms"] is None


def test_measure_full_runs_read_skill_until_first_audio(server, fake_mic, fake_speaker, index, monkeypatch):
    said, built = fake_speaker
    fake_mic["text"] = "システムの状態は？"
    monkeypatch.setitem(orchestrator.HANDLERS, "system-status",
                        lambda inputs, ctx, text: ("エラー 0 件・警告 1 件・情報 2 件（前回の照合結果）", []))
    code, body, _ = req(server, "POST", "/api/measure", {"index": 8, "mode": "full"})
    data = json.loads(body)
    assert code == 200 and data["skill"] == "system-status" and data["executed"] is True
    st = data["stages"]
    assert {"segment", "stt", "route", "first_text", "first_audio"} <= set(st)
    assert st["segment"] <= st["stt"] <= st["route"] <= st["first_text"] <= st["first_audio"]
    assert said == ["エラー 0 件、警告 1 件です。"] and data["cer"] == 0.0
    assert not built[0].speaking()                                              # 測ったら止めた


def test_measure_full_streams_answer_and_stops_after_first_sentence(server, fake_mic, fake_speaker, index,
                                                                   db_path, monkeypatch):
    import db
    said, _ = fake_speaker
    db.insert_node("Knowledge", category="minpaku", title="大洗の補助金", content="民泊の補助金の申請期限",
                   url="https://example.com/a")
    sent = []

    def fake_stream(prompt, on_text, provider=None, timeout=None, cancel=None):
        on_text("申請期限は来月末です [1]。")
        for part in ("書類は", "三つあります。"):
            time.sleep(0.3)
            if cancel and cancel():
                raise llm.LLMCancelled("取り消し")
            on_text(part)
            sent.append(part)
        return "申請期限は来月末です [1]。書類は三つあります。"
    monkeypatch.setattr(llm, "complete_stream", fake_stream)
    fake_mic["text"] = "大洗の民泊の物件について、補助金の申請期限はいつだった？"
    code, body, _ = req(server, "POST", "/api/measure", {"index": 2, "mode": "full"})
    data = json.loads(body)
    assert code == 200 and data["skill"] == "knowledge-answer" and data["provider"] == "claude_warm"
    assert data["executed"] is True and "first_audio" in data["stages"] and data["cer"] == 0.0
    assert said == ["申請期限は来月末です。"]                                     # 最初の 1 文だけ
    assert sent == []                                                            # 生成も止めた


def test_measure_full_does_not_run_write_skill(server, fake_mic, fake_speaker, index, monkeypatch):
    said, built = fake_speaker
    calls = []
    monkeypatch.setitem(orchestrator.HANDLERS, "knowledge-capture",
                        lambda inputs, ctx, text: calls.append(text) or ("ナレッジ化を終えました。", []))
    fake_mic["text"] = "今の会話をナレッジ化して"
    code, body, _ = req(server, "POST", "/api/measure", {"index": 9, "mode": "full"})
    data = json.loads(body)
    assert code == 200 and data["skill"] == "knowledge-capture" and data["executed"] is False
    assert data["note"] == "実行しない（書き込みのあるスキル）"
    assert calls == [] and said == [] and built == []
    assert "route" in data["stages"] and "first_audio" not in data["stages"]


def test_concurrent_measure_is_409_and_no_microphone_is_503(server, fake_mic, monkeypatch):
    server.evaluator._busy.acquire()
    try:
        code, body, _ = req(server, "POST", "/api/measure", {"index": 1, "mode": "stt"})
        assert code == 409 and "実行中" in json.loads(body)["error"]
    finally:
        server.evaluator._busy.release()
    monkeypatch.setattr(mic, "has_input_device", lambda: False)
    code, body, _ = req(server, "POST", "/api/measure", {"index": 1, "mode": "stt"})
    assert code == 503 and "マイク" in json.loads(body)["error"]


# ---------------------------------------------------------------- 集計と保存

def test_summary_percentiles_and_targets():
    items = [{"index": i, "cer": c, "stages": {"first_audio": ms}}
             for i, (c, ms) in enumerate([(0.0, 1000), (0.1, 1500), (0.2, 1800), (0.1, 2500), (0.0, 6000)], 1)]
    items.append({"index": 6, "cer": 0.2, "stages": {"stt": 300}})               # stt だけの件は時間に数えない
    s = voice_eval.summarize(items)
    assert s["count"] == 6 and s["cer_mean"] == round(0.6 / 6, 4)
    assert s["first_audio_count"] == 5 and s["p50_ms"] == 1800 and s["p95_ms"] == 6000
    assert s["p50_met"] is True and s["p95_met"] is False
    assert voice_eval.summarize([])["p50_met"] is None


def test_results_lists_runs_and_new_run(server, fake_mic):
    fake_mic["text"] = "システムの状態は"
    req(server, "POST", "/api/measure", {"index": 8, "mode": "stt"})
    fake_mic["text"] = "ピアノ教室"
    req(server, "POST", "/api/measure", {"index": 10, "mode": "stt"})
    data = json.loads(req(server, "GET", "/api/results")[1])
    assert [i["index"] for i in data["current"]["items"]] == [8, 10]
    assert len(data["runs"]) == 1 and data["runs"][0]["summary"]["count"] == 2
    first_id = data["current"]["id"]
    time.sleep(1.1)                                                              # ファイル名は秒まで
    assert req(server, "POST", "/api/new", {})[0] == 200
    req(server, "POST", "/api/measure", {"index": 1, "mode": "stt"})
    data = json.loads(req(server, "GET", "/api/results")[1])
    assert data["current"]["id"] != first_id and len(data["runs"]) == 2
    assert data["runs"][0]["id"] == data["current"]["id"]                        # 新しい順
    for run in data["runs"]:
        for item in run["items"]:
            assert set(item) == set(voice_eval.SAVED_KEYS)
