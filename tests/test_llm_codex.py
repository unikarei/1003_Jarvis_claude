"""codex のプロバイダー（ADR-0035、T-2001）。本物の codex は呼ばない（JARVIS_CODEX_BIN の代わりに偽のスクリプト）。"""

import json
import os
import sys
import time

import pytest

import llm

FAKE_CODEX = """#!{python}
import json, os, sys, time
with open(os.environ["FAKE_CODEX_LOG"], "a", encoding="utf-8") as f:
    f.write(json.dumps({{"argv": sys.argv[1:], "pid": os.getpid(), "cwd": os.getcwd(),
                        "keys": sorted(k for k in ("OPENAI_API_KEY", "ANTHROPIC_API_KEY", "CODEX_API_KEY")
                                       if k in os.environ)}}) + "\\n")
prompt = sys.stdin.read()

def out(obj):
    sys.stdout.write(json.dumps(obj, ensure_ascii=False) + "\\n")
    sys.stdout.flush()

out({{"type": "thread.started", "thread_id": "t"}})
out({{"type": "turn.started"}})
if prompt == "limit":
    msg = "You've hit your usage limit. Upgrade to Pro or try again at 7:22 PM."
    out({{"type": "error", "message": msg}})
    out({{"type": "turn.failed", "error": {{"message": msg}}}})
    sys.exit(1)
if prompt == "auth":
    sys.stderr.write("Error: Not logged in. Please run codex login\\n")
    sys.exit(1)
if prompt == "tool":
    out({{"type": "item.started", "item": {{"id": "c1", "type": "command_execution", "command": "ls"}}}})
    time.sleep(10)
if prompt == "crash":
    out({{"type": "item.updated", "item": {{"id": "m1", "type": "agent_message", "text": "途中"}}}})
    sys.exit(2)
if prompt == "slow":
    out({{"type": "item.updated", "item": {{"id": "m1", "type": "agent_message", "text": "最初"}}}})
    time.sleep(10)
out({{"type": "item.completed", "item": {{"id": "r", "type": "reasoning", "text": "考え中"}}}})
if prompt == "reconnect":
    out({{"type": "error", "message": "Reconnecting... 1/5"}})
if prompt == "whole":
    out({{"type": "item.completed", "item": {{"id": "m1", "type": "agent_message", "text": "全文の答え"}}}})
else:
    out({{"type": "item.updated", "item": {{"id": "m1", "type": "agent_message", "text": "答え:"}}}})
    out({{"type": "item.updated", "item": {{"id": "m1", "type": "agent_message", "text": "答え:" + prompt}}}})
    out({{"type": "item.completed", "item": {{"id": "m1", "type": "agent_message", "text": "答え:" + prompt + "。"}}}})
out({{"type": "turn.completed", "usage": {{"input_tokens": 1, "output_tokens": 1}}}})
"""


@pytest.fixture
def fake_codex(tmp_path, monkeypatch):
    script = tmp_path / "codex"
    script.write_text(FAKE_CODEX.format(python=sys.executable), encoding="utf-8")
    script.chmod(0o755)
    log = tmp_path / "codex.jsonl"
    monkeypatch.setattr(llm, "CODEX_BIN", str(script))
    monkeypatch.setenv("FAKE_CODEX_LOG", str(log))
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test-not-used")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test-not-used")

    def calls():
        return [json.loads(x) for x in log.read_text().splitlines()] if log.exists() else []
    return calls


def test_codex_streams_increments_and_returns_answer(fake_codex):
    got = []
    answer = llm.complete_stream("問い", got.append, provider="codex")
    assert answer == "答え:問い。"
    assert got == ["答え:", "問い", "。"]          # 考えの項目は渡さない。増えた分だけ渡す


def test_codex_complete_without_stream(fake_codex):
    assert llm.complete("問い", provider="codex") == "答え:問い。"


def test_codex_whole_answer_at_completion(fake_codex):
    got = []
    assert llm.complete_stream("whole", got.append, provider="codex") == "全文の答え"
    assert got == ["全文の答え"]                  # 途中の文がない版では、できた時点で 1 回


def test_codex_command_is_answer_only_and_has_no_api_keys(fake_codex, monkeypatch):
    monkeypatch.setattr(llm, "CODEX_MODEL", "gpt-test")
    monkeypatch.setattr(llm, "CODEX_REASONING", "low")
    llm.complete("問い", provider="codex")
    call = fake_codex()[0]
    argv = call["argv"]
    assert argv[0] == "exec" and argv[-1] == "-"
    for flag in ("--json", "--ephemeral", "--skip-git-repo-check", "--ignore-user-config"):
        assert flag in argv
    assert argv[argv.index("--sandbox") + 1] == "read-only"
    assert argv[argv.index("--disable") + 1] == "shell_tool"
    assert 'web_search="disabled"' in argv and 'approval_policy="never"' in argv
    assert 'model_reasoning_effort="low"' in argv
    assert argv[argv.index("--model") + 1] == "gpt-test"
    workdir = argv[argv.index("-C") + 1]
    assert os.path.realpath(call["cwd"]) == os.path.realpath(workdir)
    assert os.path.basename(workdir).startswith("jarvis-codex-") and not os.path.exists(workdir)  # 使い終えたら消す
    assert call["keys"] == []                     # API キーを渡さない（サブスクリプションのログインだけ）


def test_codex_usage_limit_is_unavailable(fake_codex):
    with pytest.raises(llm.LLMUnavailable) as e:
        llm.complete("limit", provider="codex")
    assert e.value.reason == "limit" and llm.failure_reason(e.value) == "limit"
    assert "usage limit" in str(e.value)


def test_codex_not_logged_in_is_auth(fake_codex):
    with pytest.raises(llm.LLMError) as e:
        llm.complete("auth", provider="codex")
    assert llm.failure_reason(e.value) == "auth"


def test_codex_missing_binary(monkeypatch, tmp_path):
    monkeypatch.setattr(llm, "CODEX_BIN", str(tmp_path / "no-such-codex"))
    with pytest.raises(llm.LLMUnavailable) as e:
        llm.complete("問い", provider="codex")
    assert e.value.reason == "missing"


def test_codex_tool_use_is_stopped(fake_codex):
    started = time.monotonic()
    with pytest.raises(llm.LLMError, match="ツール"):
        llm.complete("tool", provider="codex")
    assert time.monotonic() - started < 3


def test_codex_crash_midway_is_error_not_success(fake_codex):
    got = []
    with pytest.raises(llm.LLMError) as e:
        llm.complete_stream("crash", got.append, provider="codex")
    assert got == ["途中"] and llm.failure_reason(e.value) is None   # 一時的な失敗（休止にしない）


def test_codex_transient_error_event_with_answer_succeeds(fake_codex):
    assert llm.complete("reconnect", provider="codex") == "答え:reconnect。"


def test_codex_cancel_stops_process(fake_codex):
    got = []
    started = time.monotonic()
    with pytest.raises(llm.LLMCancelled):
        llm.complete_stream("slow", got.append, provider="codex", cancel=lambda: bool(got))
    assert got == ["最初"] and time.monotonic() - started < 3
    with pytest.raises(OSError):
        os.kill(fake_codex()[0]["pid"], 0)


def test_codex_timeout(fake_codex):
    with pytest.raises(llm.LLMError, match="タイムアウト") as e:
        llm.complete_stream("slow", lambda _t: None, provider="codex", timeout=1)
    assert llm.failure_reason(e.value) is None


@pytest.mark.parametrize("message, reason", [
    ("Claude Code CLI がエラーを返しました: subtype=success result=Claude AI usage limit reached|1762850000", "limit"),
    ("Claude Code CLI が終了コード 1 で終了しました（利用上限の可能性あり）: 5-hour limit reached ∙ resets 7pm", "limit"),
    ("Claude Code CLI が終了コード 1 で終了しました（利用上限の可能性あり）: Invalid API key · Please run /login", "auth"),
    ("Claude Code CLI が見つかりません: /x/claude", "missing"),
    ("Claude Code CLI が 300 秒でタイムアウトしました", None),
    ("Claude Code CLI が終了コード 1 で終了しました（利用上限の可能性あり）: ", None),
    ("Ollama に接続できません（http://127.0.0.1:11434）: Connection refused", None),
])
def test_failure_reason_classifies_messages(message, reason):
    assert llm.failure_reason(llm.LLMError(message)) == reason


def test_failure_reason_cancel_is_none():
    assert llm.failure_reason(llm.LLMCancelled("取り消されました")) is None


def test_find_codex_bin_prefers_setting(monkeypatch, tmp_path):
    binary = tmp_path / "codex"
    binary.write_text("")
    monkeypatch.setattr(llm, "CODEX_BIN", str(binary))
    assert llm.find_codex_bin() == str(binary)
    monkeypatch.setattr(llm, "CODEX_BIN", str(tmp_path / "none"))
    assert llm.find_codex_bin() is None


def test_find_codex_bin_vscode_latest(monkeypatch, tmp_path):
    monkeypatch.setattr(llm, "CODEX_BIN", "")
    monkeypatch.setattr(llm.shutil, "which", lambda _name: None)
    monkeypatch.setattr(llm.os.path, "expanduser", lambda _p: str(tmp_path))
    for version in ("26.900.1-darwin-arm64", "26.1021.22027-darwin-arm64"):
        path = tmp_path / ".vscode" / "extensions" / f"openai.chatgpt-{version}" / "bin" / "macos-aarch64"
        path.mkdir(parents=True)
        (path / "codex").write_text("")
    assert "26.1021.22027" in llm.find_codex_bin()


def test_tests_never_find_real_codex():
    """テストでは本物の codex を見つけない（conftest の JARVIS_CODEX_BIN）。"""
    assert llm.find_codex_bin() is None
    with pytest.raises(llm.LLMUnavailable):
        llm.complete("問い", provider="codex")
