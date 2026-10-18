"""遅延の記録（latency.py、T-1405、ADR-0027）のテスト。段階ごとの時間・保存・p50/p95 の集計・呼ぶ場所・起動時の読み込み。
応答の速さのテストは tests/test_latency.py（別物）。本物の Ollama・claude・マイクは呼ばない（偽の HTTP サーバー・
偽の claude・偽の時計）。"""

import io
import json
import queue
import sys
import threading
import time
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import numpy as np
import pytest

import config
import latency
import llm
import orchestrator as orc
import skills

SECRET_Q = "秘密の質問ゼブラ123"
SECRET_A = "秘密の答えキリン456"


class Clock:
    def __init__(self, now=100.0):
        self.now = now

    def __call__(self):
        return self.now


@pytest.fixture
def log(tmp_path, monkeypatch):
    path = tmp_path / "latency" / "latency.jsonl"
    monkeypatch.setattr(config, "LATENCY_LOG", path)
    latency.use(None)
    yield path
    latency.use(None)


def _records(path):
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]


# ---------------------------------------------------------------- Turn

def test_turn_records_elapsed_ms_with_fake_clock():
    clock = Clock()
    turn = latency.Turn("voice", clock=clock)
    clock.now += 0.25
    turn.mark("route")
    clock.now += 1.5
    turn.mark("first_text")
    turn.mark("route")                      # 2 回目は無視
    clock.now += 0.1
    turn.mark("first_text")
    assert turn.stages == {"route": 250, "first_text": 1750}
    turn.mark("segment", at=clock.now + 0.5)   # 時計の値を渡す
    assert turn.stages["segment"] == 2350


def test_turn_start_at_backdates():
    clock = Clock(10.0)
    turn = latency.Turn("voice", clock=clock, at=9.2)
    turn.mark("segment")
    assert turn.stages == {"segment": 800}


def test_mark_and_set_do_nothing_without_current_turn(log):
    assert latency.current() is None
    latency.mark("route")
    latency.set("skill", "general-chat")
    latency.set("unknown", "x")             # 今の Turn がなければ確かめもしない
    assert latency.finish() is None and not log.exists()


def test_unknown_attr_and_stage_are_rejected():
    turn = latency.Turn("text")
    for key, value in [("text", SECRET_Q), ("answer", SECRET_A), ("skill", 1), ("warm", "yes"), ("mode", "phone")]:
        with pytest.raises(ValueError):
            turn.set(key, value)
    with pytest.raises(ValueError):
        turn.mark("thinking")
    latency.use(turn)
    try:
        with pytest.raises(ValueError):
            latency.set("question", SECRET_Q)
    finally:
        latency.use(None)


def test_start_sets_current_and_finish_clears_it(log):
    clock = Clock()
    turn = latency.start("text", clock=clock)
    assert latency.current() is turn
    clock.now += 0.4
    latency.mark("route")
    latency.set("skill", "general-chat")
    clock.now += 0.6
    rec = latency.finish(ok=True)
    assert latency.current() is None
    assert rec["stages"] == {"route": 400, "done": 1000}
    assert rec["mode"] == "text" and rec["skill"] == "general-chat" and rec["ok"] is True


# ---------------------------------------------------------------- 保存

def test_saved_json_has_only_stages_attrs_and_time(log):
    turn = latency.start("voice")
    for stage in latency.STAGES:
        turn.mark(stage)
    for key, value in [("skill", "knowledge-answer"), ("provider", "claude_warm"), ("warm", True),
                       ("fallback", False), ("ok", True)]:
        turn.set(key, value)
    latency.finish()
    [rec] = _records(log)
    assert set(rec) == {"ts", "mode", "skill", "provider", "warm", "fallback", "ok", "stages"}
    assert set(rec["stages"]) == set(latency.STAGES)
    assert all(isinstance(v, int) for v in rec["stages"].values())
    datetime.fromisoformat(rec["ts"])


def test_log_has_no_utterance_or_answer_text(log, index, ollama):
    """発言と答えの本文を含む会話を記録しても、出力のファイルに本文が出ない。"""
    ollama.answer = [SECRET_A]
    shown = []
    latency.start("text")
    out = orc.handle(f"こんにちは {SECRET_Q}", orc.Context(stream=shown.append), use_llm=False)
    latency.finish()
    assert out.ok and SECRET_A in out.text and SECRET_Q in json.dumps(ollama.requests, ensure_ascii=False)
    content = log.read_text(encoding="utf-8")
    assert SECRET_Q not in content and SECRET_A not in content and "こんにちは" not in content


def test_rotates_when_over_5mb(log):
    log.parent.mkdir(parents=True)
    log.write_text("x" * (latency.MAX_BYTES + 1), encoding="utf-8")
    latency.finish(latency.Turn("text"))
    rotated = log.with_name("latency.jsonl.1")
    assert rotated.stat().st_size == latency.MAX_BYTES + 1
    assert len(_records(log)) == 1
    latency.finish(latency.Turn("text"))       # 5 MB 以下なら追記する
    assert len(_records(log)) == 2 and rotated.exists()


def test_keeps_only_one_old_generation(log):
    log.parent.mkdir(parents=True)
    log.with_name("latency.jsonl.1").write_text("old\n", encoding="utf-8")
    log.write_text("y" * (latency.MAX_BYTES + 1), encoding="utf-8")
    latency.finish(latency.Turn("text"))
    assert log.with_name("latency.jsonl.1").read_text().startswith("y")
    assert not log.with_name("latency.jsonl.2").exists()


def test_unwritable_place_does_not_raise(tmp_path, monkeypatch, capsys):
    blocker = tmp_path / "file"
    blocker.write_text("")
    monkeypatch.setattr(config, "LATENCY_LOG", blocker / "latency.jsonl")   # 親がファイルなので作れない
    latency.start("text")
    assert latency.finish() is None
    err = capsys.readouterr().err
    assert "遅延を記録できませんでした" in err and len(err.strip().splitlines()) == 1
    assert latency.current() is None


def test_default_log_path_is_under_data(monkeypatch):
    assert config.LATENCY_LOG.name == "latency.jsonl" and config.LATENCY_LOG.parent.name == "latency"
    assert config.LATENCY_LOG.parent.parent == config.CKC_DATA_DIR.parent   # テストでは一時フォルダ


# ---------------------------------------------------------------- 集計

def test_percentile_nearest_rank():
    values = list(range(1, 21))                # 1..20
    assert latency.percentile(values, 0.5) == 10      # ceil(10) 番目
    assert latency.percentile(values, 0.95) == 19     # ceil(19) 番目
    assert latency.percentile([7], 0.95) == 7
    assert latency.percentile([5, 1, 3], 0.5) == 3    # ceil(1.5) = 2 番目
    assert latency.percentile([5, 1, 3], 0.95) == 5   # ceil(2.85) = 3 番目
    assert latency.percentile([], 0.5) is None


NOW = datetime(2026, 10, 18, 12, 0, tzinfo=timezone(timedelta(hours=9)))


def _write_records(path, records):
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        for r in records:
            f.write(json.dumps(r, ensure_ascii=False) + "\n")


def _rec(days_ago, mode, skill, provider, warm, first_text, first_audio=None, route=10):
    stages = {"route": route, "first_text": first_text, "done": first_text + 100}
    if first_audio is not None:
        stages["first_audio"] = first_audio
    rec = {"ts": (NOW - timedelta(days=days_ago)).isoformat(timespec="seconds"), "mode": mode, "skill": skill,
           "provider": provider, "ok": True, "stages": stages}
    if warm is not None:
        rec["warm"] = warm
    return rec


def _report(path, *argv):
    return latency.main(["report", *argv], path=path, now=NOW)


def test_report_stage_table_and_groups(tmp_path, capsys):
    path = tmp_path / "latency.jsonl"
    recs = [_rec(0, "text", "knowledge-answer", "claude_warm", True, ft) for ft in range(100, 2100, 100)]  # 20 件
    recs.append(_rec(1, "voice", "general-chat", "ollama", None, 200, first_audio=900))
    _write_records(path, recs)
    assert _report(path) == 0
    out = capsys.readouterr().out
    lines = out.splitlines()
    first_text = next(line for line in lines if line.startswith("first_text"))
    assert first_text.split() == ["first_text", "21", "1000", "1900"]       # 21 件: 11 番目・20 番目
    assert next(line for line in lines if line.startswith("first_audio")).split() == ["first_audio", "1", "900", "900"]
    assert not any(line.startswith("search") for line in lines)             # ない段階は出さない
    ka = next(line for line in lines if line.startswith("knowledge-answer"))
    assert ka.split() == ["knowledge-answer", "claude_warm", "True", "20", "1000", "1900", "-", "-"]
    gc = next(line for line in lines if line.startswith("general-chat"))
    assert gc.split() == ["general-chat", "ollama", "-", "1", "200", "200", "900", "900"]


def test_report_filters(tmp_path, capsys):
    path = tmp_path / "latency.jsonl"
    _write_records(path, [
        _rec(0, "text", "general-chat", "ollama", None, 100),
        _rec(0, "voice", "general-chat", "ollama", None, 300, first_audio=800),
        _rec(0, "voice", "knowledge-answer", "claude_warm", False, 5000, first_audio=6000),
        _rec(10, "voice", "knowledge-answer", "claude_warm", True, 9999, first_audio=9999),
    ])

    def first_text_row(*argv):
        _report(path, *argv)
        row = next((line for line in capsys.readouterr().out.splitlines() if line.startswith("first_text")), "")
        return row.split()

    assert first_text_row() == ["first_text", "4", "300", "9999"]
    assert first_text_row("--mode", "text") == ["first_text", "1", "100", "100"]
    assert first_text_row("--mode", "voice") == ["first_text", "3", "5000", "9999"]
    assert first_text_row("--skill", "knowledge-answer") == ["first_text", "2", "5000", "9999"]
    assert first_text_row("--days", "7") == ["first_text", "3", "300", "5000"]
    assert first_text_row("--days", "7", "--mode", "voice", "--skill", "knowledge-answer") == \
        ["first_text", "1", "5000", "5000"]


def test_report_reads_rotated_generation(tmp_path, capsys):
    path = tmp_path / "latency.jsonl"
    _write_records(path.with_name("latency.jsonl.1"), [_rec(0, "text", "general-chat", "ollama", None, 100)])
    _write_records(path, [_rec(0, "text", "general-chat", "ollama", None, 300)])
    _report(path)
    assert "記録 2 件" in capsys.readouterr().out


def test_report_without_records(tmp_path, capsys):
    path = tmp_path / "latency.jsonl"
    _report(path)
    assert capsys.readouterr().out.strip() == "記録がありません"
    _write_records(path, [_rec(30, "text", "general-chat", "ollama", None, 100)])
    _report(path, "--days", "7")
    assert capsys.readouterr().out.strip() == "記録がありません"


def test_cli_entry(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(config, "LATENCY_LOG", tmp_path / "none.jsonl")
    monkeypatch.setattr(sys, "argv", ["latency", "report"])
    assert latency.main() == 0 and "記録がありません" in capsys.readouterr().out


# ---------------------------------------------------------------- 呼ぶ場所（偽の Ollama・偽の claude）

R1003 = "repo-1003-jarvis-claude"
QUESTION = {"question": {"type": "string", "required": True}}


def _s(desc):
    return {"description": desc, "status": "active", "permission": "read", "entry": {"repo": R1003, "function": "x"},
            "inputs": QUESTION, "phrases": []}


def _write_index(tmp_path, monkeypatch, names):
    data = {n: _s(n) for n in names}
    path = tmp_path / "skills.json"
    path.write_text(json.dumps({"skills": data}, ensure_ascii=False), encoding="utf-8")
    monkeypatch.setenv("JARVIS_SKILLS_JSON", str(path))
    skills.reset_cache()


@pytest.fixture
def index(tmp_path, monkeypatch, db_path):
    import db
    db.insert_node("Knowledge", category="minpaku", title="大洗の補助金", content="民泊の補助金の条件",
                   url="https://example.com/a")
    _write_index(tmp_path, monkeypatch, ["general-chat", "knowledge-answer"])
    yield
    skills.reset_cache()


class _OllamaHandler(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        self.server.requests.append(body)
        self.send_response(200)
        self.send_header("Content-Type", "application/x-ndjson")
        self.end_headers()
        for i, text in enumerate(self.server.answer):
            self.wfile.write((json.dumps({"message": {"content": text}, "done": False}, ensure_ascii=False)
                              + "\n").encode())
        self.wfile.write(b'{"message": {"content": ""}, "done": true}\n')


@pytest.fixture
def ollama(monkeypatch):
    server = ThreadingHTTPServer(("127.0.0.1", 0), _OllamaHandler)
    server.daemon_threads = True
    server.requests, server.answer = [], ["こんにちは、", "JARVIS です。"]
    threading.Thread(target=server.serve_forever, daemon=True).start()
    monkeypatch.setattr(llm, "OLLAMA_URL", f"http://127.0.0.1:{server.server_address[1]}")
    yield server
    server.shutdown()
    server.server_close()


FAKE_CLAUDE = """#!{python}
import json, sys
line = sys.stdin.readline()
if not line:
    sys.exit(1)
def out(obj):
    sys.stdout.write(json.dumps(obj, ensure_ascii=False) + "\\n")
    sys.stdout.flush()
out({{"type": "system", "subtype": "init"}})
for piece in ["補助金には", "条件があります [1]"]:
    out({{"type": "stream_event", "event": {{"delta": {{"type": "text_delta", "text": piece}}}}}})
out({{"type": "result", "subtype": "success", "is_error": False, "result": "補助金には条件があります [1]"}})
"""


@pytest.fixture
def fake_claude(tmp_path, monkeypatch):
    script = tmp_path / "claude"
    script.write_text(FAKE_CLAUDE.format(python=sys.executable), encoding="utf-8")
    script.chmod(0o755)
    monkeypatch.setattr(llm, "CLAUDE_BIN", str(script))
    llm.stop_warm_claude()
    yield
    llm.stop_warm_claude()


def _in_order(stages, names):
    values = [stages[n] for n in names]
    return values == sorted(values)


def test_general_chat_records_ollama_stages(log, index, ollama):
    shown = []
    latency.start("text")
    out = orc.handle("こんにちは", orc.Context(stream=shown.append), use_llm=False)
    rec = latency.finish()
    assert out.ok and out.skill == "general-chat" and shown == ["こんにちは、", "JARVIS です。"]
    assert set(rec["stages"]) == {"route", "llm_ready", "first_text", "done"}      # 検索しない
    assert _in_order(rec["stages"], ["route", "llm_ready", "first_text", "done"])
    assert (rec["skill"], rec["provider"], rec["ok"]) == ("general-chat", "ollama", True)
    assert "fallback" not in rec and "warm" not in rec


def test_knowledge_answer_records_search_and_warm(log, index, fake_claude):
    ctx = orc.Context(stream=lambda t: None)
    records = []
    for _ in range(2):                         # 1 回目はその場で起動、2 回目は待機中の claude を使う
        latency.start("text")
        out = orc.handle("大洗の物件の補助金は？", ctx, use_llm=False)
        records.append(latency.finish())
        assert out.ok and out.skill == "knowledge-answer"
    for rec in records:
        assert set(rec["stages"]) == {"route", "search", "llm_ready", "first_text", "done"}
        assert _in_order(rec["stages"], ["route", "search", "llm_ready", "first_text", "done"])
        assert (rec["skill"], rec["provider"]) == ("knowledge-answer", "claude_warm")
    assert [r["warm"] for r in records] == [False, True]
    assert len(_records(log)) == 2


def test_ollama_failure_records_fallback(log, index, fake_claude):
    """Ollama に接続できない（conftest の接続先）→ claude_warm に切り替えたら fallback=True。"""
    latency.start("text")
    out = orc.handle("こんにちは", orc.Context(stream=lambda t: None), use_llm=False)
    rec = latency.finish()
    assert out.ok and out.skill == "general-chat"
    assert rec["fallback"] is True and rec["provider"] == "claude_warm" and rec["warm"] is False
    assert {"route", "llm_ready", "first_text", "done"} <= set(rec["stages"])


def test_handle_without_turn_records_nothing(log, index, ollama):
    out = orc.handle("こんにちは", orc.Context(stream=lambda t: None), use_llm=False)
    assert out.ok and not log.exists()


def test_failed_answer_records_ok_false(log, index, monkeypatch):
    def fail(*a, **k):
        raise llm.LLMError("x")
    monkeypatch.setattr(llm, "complete_stream", fail)
    latency.start("text")
    out = orc.handle("こんにちは", orc.Context(stream=lambda t: None), use_llm=False)
    rec = latency.finish()
    assert not out.ok and rec["ok"] is False and rec["fallback"] is True and "first_text" not in rec["stages"]


# ---------------------------------------------------------------- 起動時の読み込み

@pytest.fixture
def warm_calls(monkeypatch):
    calls = []
    monkeypatch.setattr(llm, "warm_up_ollama", lambda *a, **k: calls.append("ollama") or True)
    monkeypatch.setattr(llm, "warm_up_claude", lambda *a, **k: calls.append("claude") or True)
    return calls


@pytest.mark.parametrize("names,expected", [
    (["general-chat", "knowledge-answer"], ["ollama", "claude"]),
    (["general-chat"], ["ollama"]),
    (["knowledge-answer"], ["claude"]),
    (["knowledge-stats"], []),
])
def test_warm_up_follows_index(tmp_path, monkeypatch, warm_calls, names, expected):
    _write_index(tmp_path, monkeypatch, names)
    orc.warm_up()
    assert warm_calls == expected
    skills.reset_cache()


def test_warm_up_swallows_errors(tmp_path, monkeypatch, capsys):
    _write_index(tmp_path, monkeypatch, ["general-chat", "knowledge-answer"])
    calls = []

    def boom(*a, **k):
        calls.append(1)
        raise RuntimeError("Ollama がない")
    monkeypatch.setattr(llm, "warm_up_ollama", boom)
    monkeypatch.setattr(llm, "warm_up_claude", boom)
    orc.warm_up()
    assert calls == [1, 1]                     # 1 つ目が失敗しても 2 つ目を試す
    monkeypatch.setattr(skills, "active", lambda: (_ for _ in ()).throw(RuntimeError("索引が壊れている")))
    orc.warm_up()
    assert capsys.readouterr() == ("", "")
    skills.reset_cache()


def test_warm_up_without_ollama_or_claude_does_not_raise(tmp_path, monkeypatch):
    """本物の warm_up_*（conftest で Ollama の接続先と claude を無効にしてある）でも例外を出さない。"""
    _write_index(tmp_path, monkeypatch, ["general-chat", "knowledge-answer"])
    llm.stop_warm_claude()
    orc.warm_up()
    assert llm._warm is None
    skills.reset_cache()


def test_start_warm_up_runs_in_background_thread(monkeypatch):
    seen = []
    release = threading.Event()
    monkeypatch.setattr(orc, "warm_up", lambda: (release.wait(2), seen.append(threading.current_thread())))
    thread = orc.start_warm_up()
    assert thread is not threading.current_thread() and thread.daemon and seen == []   # 待たずに戻る
    release.set()
    thread.join(2)
    assert seen == [thread]


@pytest.fixture
def chat(monkeypatch):
    import importlib
    monkeypatch.setattr(sys, "stdin", io.TextIOWrapper(io.BytesIO()))
    monkeypatch.setattr(sys, "stdout", io.TextIOWrapper(io.BytesIO()))
    sys.modules.pop("chat", None)
    module = importlib.import_module("chat")
    monkeypatch.setattr(sys, "stdout", sys.__stdout__)
    yield module


def test_chat_starts_warm_up_and_records_each_turn(chat, log, index, ollama, monkeypatch):
    started = []
    monkeypatch.setattr(orc, "start_warm_up", lambda: started.append(1))
    answers = iter(["こんにちは", SECRET_Q, "exit"])
    monkeypatch.setattr("builtins.input", lambda prompt="": next(answers))
    chat.main()
    assert started == [1]
    recs = _records(log)
    assert [r["mode"] for r in recs] == ["text", "text"]
    assert all({"route", "llm_ready", "first_text", "done"} <= set(r["stages"]) for r in recs)
    assert SECRET_Q not in log.read_text(encoding="utf-8")
    assert latency.current() is None


def _keys(*lines):
    q = queue.Queue()
    for line in lines:
        q.put(line)
    return q


class _Player:
    def __init__(self):
        self.played = []

    def enqueue(self, turn, sentence, audio):
        self.played.append((turn, sentence))

    def stop(self): pass
    def speaking(self): return False
    def heard(self, turn): return [s for t, s in self.played if t == turn]
    def level(self, a, b): return 0.0
    def close(self): pass


@pytest.mark.parametrize("push_to_talk", [False, True])
def test_voice_chat_starts_warm_up_and_records_voice_stages(log, index, ollama, monkeypatch, push_to_talk):
    import voice_chat
    from voice import duplex
    started = []
    monkeypatch.setattr(orc, "start_warm_up", lambda: started.append(1))
    monkeypatch.setenv("JARVIS_VOICE_SILENCE_SECONDS", "0.8")
    texts = iter(["こんにちは", "終了"])
    sp = duplex.DuplexSpeaker(_Player(), synth=lambda sentence, cancel: np.zeros(10, dtype=np.float32))
    keys = _keys("", "", "q") if push_to_talk else _keys()
    assert voice_chat.main(listen=lambda: np.zeros(10, dtype=np.float32), transcribe=lambda a: next(texts),
                           speaker=sp, keys=keys, push_to_talk=push_to_talk) == 0
    assert started == [1]
    [rec] = _records(log)
    assert rec["mode"] == "voice" and rec["skill"] == "general-chat" and rec["provider"] == "ollama"
    stages = rec["stages"]
    assert _in_order(stages, ["segment", "stt", "route", "llm_ready", "first_text"])
    if not push_to_talk:                        # ハンズフリーは読み上げを待ってから保存する（最初の声が必ず入る）
        assert stages["first_audio"] >= stages["first_text"]
    assert stages["segment"] >= 800             # 話し終えてから区切りが決まるまで（無音の長さ）
    assert stages["done"] >= stages["first_text"]
    assert latency.current() is None


def test_voice_chat_without_microphone_does_not_warm_up(monkeypatch):
    import voice_chat
    from voice import record as rec
    started = []
    monkeypatch.setattr(orc, "start_warm_up", lambda: started.append(1))
    monkeypatch.setattr(rec, "has_input_device", lambda: False)
    assert voice_chat.main() == 1 and started == []


def test_barge_in_records_interrupted_turn_as_not_ok(log, kb_index, monkeypatch, capsys):
    """割り込まれた発言は ok=False、次の発言は ok=True で記録する。"""
    from tests.test_voice_barge_in import FakeSession, _llm, _run
    _llm(monkeypatch, [["一文目です。", "二文目です。", "三文目です。"], ["新しい答えです。"]])
    session = FakeSession(["民泊の補助金の条件は？", "ちょっと待って、別の質問"], barge_on=["二文目です。"])
    assert _run(session) == 0
    recs = _records(log)
    assert [r["ok"] for r in recs] == [False, True] and all(r["mode"] == "voice" for r in recs)
    assert all({"segment", "stt", "route", "first_text", "first_audio", "done"} <= set(r["stages"]) for r in recs)
    assert "ちょっと待って" not in log.read_text(encoding="utf-8")


@pytest.fixture
def kb_index(db_path):
    import db
    db.insert_node("Knowledge", category="minpaku", title="大洗の補助金", content="民泊の補助金の条件",
                   url="https://example.com/a")
    skills.reset_cache()
    yield
    skills.reset_cache()


def test_voice_speech_end_uses_silence_length(monkeypatch):
    import voice_chat
    monkeypatch.setenv("JARVIS_VOICE_SILENCE_SECONDS", "0.5")
    assert voice_chat.speech_end(10.0) == pytest.approx(9.5)
    turn = voice_chat.start_turn(10.0, 10.3)
    assert turn.stages == {"segment": 500, "stt": 800} and turn.attrs == {"mode": "voice"}
