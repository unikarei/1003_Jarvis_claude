"""llm.py の共通関数（T-107）と、既存関数の後方互換のテスト。LLM はすべてモック。"""

import inspect
import json
import subprocess
from types import SimpleNamespace

import pytest

import llm


# --- 既存関数の後方互換 ---

def test_existing_signatures_unchanged():
    assert str(inspect.signature(llm.ask_claude)) == (
        "(prompt: str, max_tokens: int = 1000, model: str = 'claude-haiku-4-5-20251001') -> str")
    params = inspect.signature(llm.ask_claude_tiered).parameters
    assert list(params) == ["prompt", "max_tokens", "light_model", "heavy_model", "force_escalate"]
    assert params["max_tokens"].default == 1000
    assert params["force_escalate"].default is False


class _FakeClient:
    calls = []

    def __init__(self, api_key=None):
        self.messages = self

    def create(self, **kwargs):
        _FakeClient.calls.append(kwargs)
        return SimpleNamespace(content=[SimpleNamespace(text="  十分な長さの回答  ")])


def test_ask_claude_calls_api_as_before(monkeypatch):
    _FakeClient.calls = []
    monkeypatch.setattr(llm.anthropic, "Anthropic", _FakeClient)
    assert llm.ask_claude("質問") == "十分な長さの回答"
    assert _FakeClient.calls == [{
        "model": llm.SUMMARY_MODEL, "max_tokens": 1000,
        "messages": [{"role": "user", "content": "質問"}],
    }]


def test_ask_claude_tiered_still_uses_light_model_first(monkeypatch):
    _FakeClient.calls = []
    monkeypatch.setattr(llm.anthropic, "Anthropic", _FakeClient)
    assert llm.ask_claude_tiered("質問") == "十分な長さの回答"
    assert [c["model"] for c in _FakeClient.calls] == [llm.SUMMARY_MODEL]


# --- complete() ---

def _envelope(result="ok", subtype="success", is_error=False):
    return json.dumps({"type": "result", "subtype": subtype, "is_error": is_error, "result": result})


def _fake_run(stdout="", returncode=0, stderr="", captured=None):
    def run(cmd, **kwargs):
        if captured is not None:
            captured.update(cmd=cmd, **kwargs)
        return subprocess.CompletedProcess(cmd, returncode, stdout=stdout, stderr=stderr)
    return run


def test_default_provider_is_claude_cli():
    assert llm.LLM_PROVIDER == "claude_cli"


def test_claude_cli_command_disables_tools_and_uses_stdin(monkeypatch):
    captured = {}
    monkeypatch.setattr(llm.subprocess, "run", _fake_run(_envelope("結果"), captured=captured))
    assert llm.complete("本文", provider="claude_cli") == "結果"
    cmd = captured["cmd"]
    assert cmd[1] == "-p"
    assert cmd[cmd.index("--tools") + 1] == ""
    assert "--no-session-persistence" in cmd
    assert cmd[cmd.index("--output-format") + 1] == "json"
    assert captured["input"] == "本文"
    assert "本文" not in cmd
    assert "jarvis-llm-" in captured["cwd"]


@pytest.mark.parametrize("run, message", [
    (_fake_run("", returncode=1, stderr="usage limit reached"), "終了コード 1"),
    (_fake_run("not json"), "JSON ではありません"),
    (_fake_run(_envelope("", subtype="error_during_execution", is_error=True)), "エラーを返しました"),
])
def test_claude_cli_failures_raise(monkeypatch, run, message):
    monkeypatch.setattr(llm.subprocess, "run", run)
    with pytest.raises(llm.LLMError, match=message):
        llm.complete("本文", provider="claude_cli")


def test_claude_cli_timeout_raises(monkeypatch):
    def run(cmd, **kwargs):
        raise subprocess.TimeoutExpired(cmd, kwargs["timeout"])
    monkeypatch.setattr(llm.subprocess, "run", run)
    with pytest.raises(llm.LLMError, match="タイムアウト"):
        llm.complete("本文", provider="claude_cli")


def test_failure_does_not_fall_back_to_paid_api(monkeypatch):
    monkeypatch.setattr(llm.subprocess, "run", _fake_run("", returncode=1, stderr="usage limit reached"))
    called = []
    monkeypatch.setitem(llm.PROVIDERS, "anthropic_api", lambda prompt: called.append(prompt) or "x")
    monkeypatch.setattr(llm, "ask_claude", lambda *a, **k: called.append("ask_claude") or "x")
    with pytest.raises(llm.LLMError):
        llm.complete("本文")
    assert called == []


def test_unknown_provider_raises():
    with pytest.raises(llm.LLMError, match="未知"):
        llm.complete("本文", provider="nope")


def test_complete_passes_timeout_only_when_given(monkeypatch):
    """timeout を渡したときだけプロバイダーに渡す（既定の動作は変えない、FR-014 F-3）。"""
    seen = []
    monkeypatch.setitem(llm.PROVIDERS, "fake", lambda prompt, timeout=None: seen.append(timeout) or "ok")
    assert llm.complete("p", provider="fake") == "ok"
    assert llm.complete("p", provider="fake", timeout=60) == "ok"
    assert seen == [None, 60]


def test_claude_cli_never_gets_api_keys(monkeypatch):
    """.env の ANTHROPIC_API_KEY が見えると claude -p は API の従量課金で動くため、子プロセスに渡さない（ADR-0020）。"""
    for key in ("ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN", "ANTHROPIC_BASE_URL"):
        monkeypatch.setenv(key, "x")
    monkeypatch.setenv("JARVIS_KEEP_ME", "1")
    captured = {}
    monkeypatch.setattr(llm.subprocess, "run", _fake_run(_envelope("結果"), captured=captured))
    llm.complete("本文", provider="claude_cli")
    env = captured["env"]
    assert not {"ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN", "ANTHROPIC_BASE_URL"} & set(env)
    assert env["JARVIS_KEEP_ME"] == "1"

    # 逐次表示の経路（conftest が差し替えているため、モジュールを別に読み込んで確かめる）
    import importlib.util
    spec = importlib.util.spec_from_file_location("llm_fresh", llm.__file__)
    fresh = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(fresh)
    seen = {}

    class FakePopen:
        def __init__(self, cmd, **kwargs):
            seen.update(kwargs)
            raise FileNotFoundError
    monkeypatch.setattr(fresh.subprocess, "Popen", FakePopen)
    with pytest.raises(fresh.LLMError):
        fresh._stream_claude_cli("本文", lambda t: None)
    assert "ANTHROPIC_API_KEY" not in seen["env"] and seen["env"]["JARVIS_KEEP_ME"] == "1"


def test_stream_stops_claude_when_cancelled(monkeypatch):
    """取り消し（声の割り込み、ADR-0028）があれば、最初の文字が出る前でも claude -p を止める。"""
    import importlib.util
    import time as _time
    spec = importlib.util.spec_from_file_location("llm_fresh2", llm.__file__)
    fresh = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(fresh)
    r, w = __import__("os").pipe()
    state = {"killed": False}

    class SilentProc:                                   # 何も出力しない claude -p
        def __init__(self, cmd, **kwargs):
            self.stdin = open(__import__("os").devnull, "w")
            self.stdout = __import__("os").fdopen(r, "r")
            self.stderr = None
            self.returncode = None
        def poll(self): return None if not state["killed"] else -9
        def kill(self): state["killed"] = True
        def wait(self): return -9
    monkeypatch.setattr(fresh.subprocess, "Popen", SilentProc)
    started = _time.monotonic()
    with pytest.raises(fresh.LLMCancelled):
        fresh._stream_claude_cli("本文", lambda t: None, timeout=30, cancel=lambda: _time.monotonic() - started > 0.2)
    assert state["killed"] and _time.monotonic() - started < 1.0
    __import__("os").close(w)
