"""llm.py の Ollama と待機させた claude -p のプロバイダー（T-1402、ADR-0027）のテスト。
本物の Ollama・claude は呼ばない（偽の HTTP サーバーと、CLAUDE_BIN に差し替えた偽の claude を使う）。"""

import json
import socket
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

import llm


# --- Ollama（偽の HTTP サーバー） ---

class _Handler(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def do_GET(self):
        self.server.requests.append(("GET", self.path, None))
        self.server.on_get(self)

    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        self.server.requests.append(("POST", self.path, body))
        self.server.on_post(self, body)


def _line(text, done=False):
    return (json.dumps({"message": {"role": "assistant", "content": text}, "done": done},
                       ensure_ascii=False) + "\n").encode()


def _send_json(handler, obj, status=200):
    data = json.dumps(obj, ensure_ascii=False).encode()
    handler.send_response(status)
    handler.send_header("Content-Type", "application/json")
    handler.send_header("Content-Length", str(len(data)))
    handler.end_headers()
    handler.wfile.write(data)


def _start_stream(handler):
    handler.send_response(200)
    handler.send_header("Content-Type", "application/x-ndjson")
    handler.end_headers()


@pytest.fixture
def ollama(monkeypatch):
    server = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
    server.daemon_threads = True
    server.requests = []
    server.closed = threading.Event()
    server.on_get = lambda h: _send_json(h, {"models": [{"name": "gemma4:12b", "model": "gemma4:12b"}]})
    server.on_post = lambda h, body: _send_json(h, {"message": {"role": "assistant", "content": "全文です"},
                                                    "done": True})
    threading.Thread(target=server.serve_forever, daemon=True).start()
    monkeypatch.setattr(llm, "OLLAMA_URL", f"http://127.0.0.1:{server.server_address[1]}")
    monkeypatch.setattr(llm, "CHAT_MODEL", "gemma4:12b")
    yield server
    server.shutdown()
    server.server_close()


@pytest.fixture
def unreachable(monkeypatch):
    """何も待ち受けていない接続先。"""
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    sock.close()
    monkeypatch.setattr(llm, "OLLAMA_URL", f"http://127.0.0.1:{port}")


def test_ollama_complete_returns_full_text_and_sends_expected_json(ollama):
    assert llm.complete("質問", provider="ollama") == "全文です"
    method, path, body = ollama.requests[-1]
    assert (method, path) == ("POST", "/api/chat")
    assert body == {"model": "gemma4:12b", "messages": [{"role": "user", "content": "質問"}], "stream": False,
                    "think": False, "keep_alive": "30m"}


def test_ollama_stream_passes_each_delta(ollama):
    def stream(h, body):
        _start_stream(h)
        for piece in ["こん", "にち", "は"]:
            h.wfile.write(_line(piece))
            h.wfile.flush()
        h.wfile.write(_line("", done=True))
    ollama.on_post = stream
    got = []
    assert llm.complete_stream("質問", got.append, provider="ollama") == "こんにちは"
    assert got == ["こん", "にち", "は"]
    body = ollama.requests[-1][2]
    assert body["stream"] is True and body["think"] is False and body["keep_alive"] == "30m"
    assert body["model"] == "gemma4:12b"


def test_ollama_stream_reads_lines_split_across_chunks(ollama):
    """行が複数の chunk に分かれて（日本語の途中で切れて）届いても正しく読む（本物の Ollama は chunked）。"""
    data = _line("こんにちは") + _line("、世界") + _line("", done=True)

    def stream(h, body):
        h.protocol_version = "HTTP/1.1"
        h.send_response(200)
        h.send_header("Content-Type", "application/x-ndjson")
        h.send_header("Transfer-Encoding", "chunked")
        h.end_headers()
        for i in range(0, len(data), 7):
            piece = data[i:i + 7]
            h.wfile.write(f"{len(piece):x}\r\n".encode() + piece + b"\r\n")
            h.wfile.flush()
            time.sleep(0.005)
        h.wfile.write(b"0\r\n\r\n")
        h.close_connection = True
    ollama.on_post = stream
    got = []
    assert llm.complete_stream("質問", got.append, provider="ollama") == "こんにちは、世界"
    assert got == ["こんにちは", "、世界"]


def test_ollama_stream_cancel_stops_reading_and_closes_connection(ollama):
    def stream(h, body):
        _start_stream(h)
        h.wfile.write(_line("最初"))
        h.wfile.flush()
        try:
            for _ in range(100):                       # 取り消されなければ 5 秒書き続ける
                time.sleep(0.05)
                h.wfile.write(_line("続き"))
                h.wfile.flush()
        except OSError:
            ollama.closed.set()
    ollama.on_post = stream
    got = []
    started = time.monotonic()
    with pytest.raises(llm.LLMCancelled):
        llm.complete_stream("質問", got.append, provider="ollama", cancel=lambda: bool(got))
    assert got == ["最初"]
    assert time.monotonic() - started < 1.5
    assert ollama.closed.wait(3)                       # 接続が閉じられた


@pytest.mark.parametrize("protocol, connection", [("HTTP/1.0", None), ("HTTP/1.1", "close")])
def test_ollama_stream_cancel_closes_socket_held_by_response(ollama, protocol, connection):
    """HTTP/1.0・Connection: close の応答（getresponse() の後に conn.sock が None）でも、取り消したら
    接続を閉じ、読み取りのスレッドが残らない（相手が何も書かずに止まっていても）。"""
    def stream(h, body):
        h.protocol_version = protocol
        h.send_response(200)
        h.send_header("Content-Type", "application/x-ndjson")
        if connection:
            h.send_header("Connection", connection)
        h.end_headers()
        h.wfile.write(_line("最初"))
        h.wfile.flush()
        time.sleep(3)                                  # 何も書かずに待つ
        h.close_connection = True
    ollama.on_post = stream
    got = []
    with pytest.raises(llm.LLMCancelled):
        llm.complete_stream("質問", got.append, provider="ollama", cancel=lambda: bool(got))
    assert got == ["最初"]
    deadline = time.monotonic() + 1.0
    while time.monotonic() < deadline and any(t.name == "jarvis-ollama-reader" and t.is_alive()
                                              for t in threading.enumerate()):
        time.sleep(0.02)
    assert not any(t.name == "jarvis-ollama-reader" and t.is_alive() for t in threading.enumerate())


def test_ollama_stream_cancel_in_last_delta_with_done(ollama):
    """done: true の行に本文があり、その on_text の中で取り消されたら、全文を返さず LLMCancelled。"""
    def stream(h, body):
        _start_stream(h)
        h.wfile.write(_line("最初"))
        h.wfile.write(_line("最後", done=True))
        h.wfile.flush()
    ollama.on_post = stream
    got = []
    with pytest.raises(llm.LLMCancelled):
        llm.complete_stream("質問", got.append, provider="ollama", cancel=lambda: "最後" in got)
    assert got == ["最初", "最後"]


def test_ollama_stream_cancel_while_waiting(ollama):
    """最初の行が届く前でも、0.1 秒ごとに取り消しを確かめる。"""
    ollama.on_post = lambda h, body: (_start_stream(h), h.wfile.flush(), time.sleep(3))
    started = time.monotonic()
    with pytest.raises(llm.LLMCancelled):
        llm.complete_stream("質問", lambda t: None, provider="ollama",
                            cancel=lambda: time.monotonic() - started > 0.2)
    assert time.monotonic() - started < 1.0


def test_ollama_stream_timeout_raises(ollama):
    ollama.on_post = lambda h, body: (_start_stream(h), h.wfile.flush(), time.sleep(3))
    started = time.monotonic()
    with pytest.raises(llm.LLMError) as e:
        llm.complete_stream("質問", lambda t: None, provider="ollama", timeout=0.5)
    assert not isinstance(e.value, llm.LLMCancelled)
    assert time.monotonic() - started < 2.0


def test_ollama_http_500_raises_llm_error(ollama):
    ollama.on_post = lambda h, body: _send_json(h, {"error": "model crashed"}, status=500)
    with pytest.raises(llm.LLMError, match="500"):
        llm.complete("質問", provider="ollama")
    with pytest.raises(llm.LLMError, match="500"):
        llm.complete_stream("質問", lambda t: None, provider="ollama")


def test_ollama_unreachable_raises_llm_error_without_fallback(unreachable, monkeypatch):
    called = []
    monkeypatch.setitem(llm.PROVIDERS, "anthropic_api", lambda *a, **k: called.append("api") or "x")
    monkeypatch.setitem(llm.PROVIDERS, "claude_cli", lambda *a, **k: called.append("cli") or "x")
    monkeypatch.setattr(llm, "ask_claude", lambda *a, **k: called.append("ask_claude") or "x")
    with pytest.raises(llm.LLMError, match="接続できません"):
        llm.complete("質問", provider="ollama")
    with pytest.raises(llm.LLMError, match="接続できません"):
        llm.complete_stream("質問", lambda t: None, provider="ollama")
    assert called == []


def test_ollama_available(ollama, monkeypatch):
    assert llm.ollama_available() is True
    assert ollama.requests[-1][:2] == ("GET", "/api/tags")
    monkeypatch.setattr(llm, "CHAT_MODEL", "other:7b")
    assert llm.ollama_available() is False
    monkeypatch.setattr(llm, "CHAT_MODEL", "gemma4")           # タグを省略すると :latest
    ollama.on_get = lambda h: _send_json(h, {"models": [{"name": "gemma4:latest"}]})
    assert llm.ollama_available() is True


def test_ollama_available_false_when_unreachable(unreachable):
    assert llm.ollama_available(timeout=0.5) is False


def test_warm_up_ollama_sends_keep_alive_only(ollama):
    assert llm.warm_up_ollama() is True
    body = ollama.requests[-1][2]
    assert body["messages"] == [] and body["keep_alive"] == "30m" and body["model"] == "gemma4:12b"


def test_warm_up_ollama_failure_returns_false(ollama, monkeypatch):
    ollama.on_post = lambda h, body: _send_json(h, {"error": "x"}, status=500)
    assert llm.warm_up_ollama() is False
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    sock.close()
    monkeypatch.setattr(llm, "OLLAMA_URL", f"http://127.0.0.1:{port}")
    assert llm.warm_up_ollama() is False


# --- 待機させた claude -p（偽の claude） ---

FAKE_CLAUDE = """#!{python}
import json, os, sys, time
with open(os.environ["FAKE_CLAUDE_LOG"], "a") as f:
    f.write(json.dumps({{"pid": os.getpid(), "argv": sys.argv[1:],
                        "has_key": "ANTHROPIC_API_KEY" in os.environ}}) + "\\n")
line = sys.stdin.readline()
if not line:
    sys.exit(1)
content = json.loads(line)["message"]["content"]

def out(obj):
    sys.stdout.write(json.dumps(obj, ensure_ascii=False) + "\\n")
    sys.stdout.flush()

def delta(text):
    out({{"type": "stream_event", "event": {{"delta": {{"type": "text_delta", "text": text}}}}}})

out({{"type": "system", "subtype": "init"}})
if content == "slow":
    delta("最初")
    time.sleep(10)
if content == "burst":                          # 複数の行を一度に書く（1 回の読み取りで届く）
    sys.stdout.write("".join(json.dumps({{"type": "stream_event", "event": {{"delta": {{
        "type": "text_delta", "text": t}}}}}}, ensure_ascii=False) + "\\n" for t in ["一", "二", "三"]))
    sys.stdout.flush()
    time.sleep(10)
pieces = ["答え:", content, "@" + str(os.getpid())]
for piece in pieces:
    delta(piece)
out({{"type": "result", "subtype": "success", "is_error": False, "result": "".join(pieces)}})
"""


@pytest.fixture
def fake_claude(tmp_path, monkeypatch):
    script = tmp_path / "claude"
    script.write_text(FAKE_CLAUDE.format(python=sys.executable), encoding="utf-8")
    script.chmod(0o755)
    log = tmp_path / "starts.jsonl"
    monkeypatch.setattr(llm, "CLAUDE_BIN", str(script))
    monkeypatch.setenv("FAKE_CLAUDE_LOG", str(log))
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test-not-used")
    llm.stop_warm_claude()

    def starts(at_least=0):
        deadline = time.monotonic() + 5
        while True:
            entries = [json.loads(x) for x in log.read_text().splitlines()] if log.exists() else []
            if len(entries) >= at_least or time.monotonic() > deadline:
                return entries
            time.sleep(0.02)
    yield starts
    llm.stop_warm_claude()


def test_claude_warm_second_call_uses_prestarted_process(fake_claude):
    got = []
    first = llm.complete_stream("一", got.append, provider="claude_warm")
    assert first.startswith("答え:一@") and got[:2] == ["答え:", "一"]
    second = llm.complete("二", provider="claude_warm")
    starts = fake_claude(at_least=3)
    assert len(starts) == 3                    # 1 回目はその場で起動、その後は前もって起動した分だけ
    assert second == f"答え:二@{starts[1]['pid']}"   # 2 回目は 1 回目の後に起動しておいたプロセス
    assert llm._warm is not None and llm._warm[0].pid == starts[2]["pid"]
    argv = starts[0]["argv"]
    assert argv[0] == "-p" and "--no-session-persistence" in argv
    assert argv[argv.index("--input-format") + 1] == "stream-json"
    assert argv[argv.index("--output-format") + 1] == "stream-json"
    assert argv[argv.index("--tools") + 1] == ""


def test_claude_warm_restarts_dead_process(fake_claude):
    assert llm.warm_up_claude() is True
    dead = llm._warm[0]
    assert fake_claude(at_least=1)[0]["pid"] == dead.pid     # 起動を記録してから止める
    dead.kill()
    dead.wait()
    answer = llm.complete("問い", provider="claude_warm")
    starts = fake_claude(at_least=3)
    assert len(starts) == 3
    assert answer == f"答え:問い@{starts[1]['pid']}" and starts[1]["pid"] != dead.pid


def test_claude_warm_cancel(fake_claude):
    got = []
    started = time.monotonic()
    with pytest.raises(llm.LLMCancelled):
        llm.complete_stream("slow", got.append, provider="claude_warm", cancel=lambda: bool(got))
    assert got == ["最初"] and time.monotonic() - started < 3
    used = fake_claude(at_least=1)[0]["pid"]
    with pytest.raises(OSError):
        import os
        os.kill(used, 0)                       # 取り消したプロセスは止まっている
    assert llm._warm is not None and llm._warm[0].poll() is None   # 次のプロセスは起動しておく


def test_claude_warm_cancel_between_lines_read_at_once(fake_claude):
    """一度に読んだ複数の行の途中で取り消しても、残りの差分を渡さない。"""
    got = []
    started = time.monotonic()
    with pytest.raises(llm.LLMCancelled):
        llm.complete_stream("burst", got.append, provider="claude_warm", cancel=lambda: bool(got))
    assert got == ["一"] and time.monotonic() - started < 3


def test_claude_warm_child_has_no_api_key(fake_claude):
    llm.complete("問い", provider="claude_warm")
    starts = fake_claude(at_least=2)
    assert starts and not any(s["has_key"] for s in starts)


def test_claude_warm_registers_atexit_and_stops(fake_claude, monkeypatch):
    registered = []
    monkeypatch.setattr(llm, "_atexit_registered", False)
    monkeypatch.setattr(llm.atexit, "register", registered.append)
    assert llm.warm_up_claude() is True
    assert registered == [llm.stop_warm_claude]
    proc = llm._warm[0]
    llm.warm_up_claude()                       # 待機させるのは 1 つだけ
    assert llm._warm[0] is proc and registered == [llm.stop_warm_claude]
    llm.stop_warm_claude()
    assert llm._warm is None and proc.poll() is not None


def test_warm_up_claude_failure_returns_false(monkeypatch, tmp_path):
    llm.stop_warm_claude()
    monkeypatch.setattr(llm, "CLAUDE_BIN", str(tmp_path / "no-such-claude"))
    assert llm.warm_up_claude() is False
    with pytest.raises(llm.LLMError, match="見つかりません"):
        llm.complete("問い", provider="claude_warm")


def test_new_providers_registered():
    assert {"claude_cli", "anthropic_api", "ollama", "claude_warm"} <= set(llm.PROVIDERS)



def test_ollama_stream_cut_before_done_raises(ollama):
    """done: true の前に接続が切れたら、受け取った断片を成功として返さず LLMError（Codex の 3 回目の指摘）。"""
    def stream(h, body):
        _start_stream(h)
        h.wfile.write(_line("途中まで"))
        h.wfile.flush()
        h.close_connection = True                          # done を送らずに終わる
    ollama.on_post = stream
    got = []
    with pytest.raises(llm.LLMError, match="途中で切れました"):
        llm.complete_stream("質問", got.append, provider="ollama")
