"""Claude APIへの問い合わせを共通化するヘルパー。"""

import json
import subprocess
import tempfile

import anthropic

from agent_log import log_event
from config import (ANTHROPIC_API_KEY, CLAUDE_BIN, CLAUDE_CLI_TIMEOUT_SECONDS, ESCALATION_MODEL,
                    LLM_PROVIDER, SUMMARY_MODEL)

MIN_VALID_LENGTH = 5  # これより短い応答は検証失敗（空応答・応答エラー等）とみなす


def ask_claude(prompt: str, max_tokens: int = 1000, model: str = SUMMARY_MODEL) -> str:
    client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)
    message = client.messages.create(
        model=model,
        max_tokens=max_tokens,
        messages=[{"role": "user", "content": prompt}],
    )
    return message.content[0].text.strip()


def ask_claude_tiered(
    prompt: str,
    max_tokens: int = 1000,
    light_model: str = SUMMARY_MODEL,
    heavy_model: str = ESCALATION_MODEL,
    force_escalate: bool = False,
) -> str:
    """軽量レイヤーを既定とし、検証失敗時またはforce_escalate指定時のみ高精度レイヤーへ
    エスカレーションする（CLAUDE.md「エージェント・APIルーティング方針」参照）。
    エスカレーション発生時のみ jarvis_agent.log に記録する。
    """
    if not force_escalate:
        light_response = ask_claude(prompt, max_tokens=max_tokens, model=light_model)
        if len(light_response) >= MIN_VALID_LENGTH:
            return light_response
        reason = "light_response_too_short"
    else:
        reason = "force_escalate"

    heavy_response = ask_claude(prompt, max_tokens=max_tokens, model=heavy_model)
    log_event(
        "escalation",
        reason=reason,
        from_model=light_model,
        to_model=heavy_model,
        prompt_preview=prompt[:200],
    )
    return heavy_response


# --- プロバイダーを切り替えられる共通関数（../docs/decisions.md ADR-0020） ---
# 既存の ask_claude / ask_claude_tiered は変更しない。CKC など新しい処理は complete() を使う。


class LLMError(Exception):
    """LLM の呼び出しに失敗した（CLI の異常終了、上限到達、タイムアウトなど）。別のプロバイダーには切り替えない。"""


def _complete_claude_cli(prompt: str, timeout: int | None = None) -> str:
    """Claude Code CLI（claude -p）をツールなしで実行し、最終応答のテキストを返す。"""
    timeout = timeout or CLAUDE_CLI_TIMEOUT_SECONDS
    cmd = [CLAUDE_BIN, "-p", "--tools", "", "--no-session-persistence", "--output-format", "json"]
    with tempfile.TemporaryDirectory(prefix="jarvis-llm-") as workdir:
        try:
            proc = subprocess.run(cmd, input=prompt, capture_output=True, text=True,
                                  cwd=workdir, timeout=timeout)
        except FileNotFoundError:
            raise LLMError(f"Claude Code CLI が見つかりません: {CLAUDE_BIN}") from None
        except subprocess.TimeoutExpired:
            raise LLMError(f"Claude Code CLI が {timeout} 秒でタイムアウトしました") from None

    if proc.returncode != 0:
        detail = (proc.stderr or proc.stdout).strip()[:500]
        raise LLMError(f"Claude Code CLI が終了コード {proc.returncode} で終了しました（利用上限の可能性あり）: {detail}")
    try:
        envelope = json.loads(proc.stdout)
    except json.JSONDecodeError:
        raise LLMError("Claude Code CLI の出力が JSON ではありません") from None
    if envelope.get("is_error") or envelope.get("subtype") != "success":
        raise LLMError(f"Claude Code CLI がエラーを返しました: subtype={envelope.get('subtype')} "
                       f"result={str(envelope.get('result', ''))[:200]}")
    return envelope.get("result", "")


def _complete_anthropic_api(prompt: str, timeout: int | None = None) -> str:  # timeout は SDK の既定に任せる
    try:
        return ask_claude(prompt, max_tokens=4000, model=ESCALATION_MODEL)
    except anthropic.APIError as e:
        raise LLMError(f"Claude API の呼び出しに失敗しました: {e}") from None


PROVIDERS = {"claude_cli": _complete_claude_cli, "anthropic_api": _complete_anthropic_api}


def complete(prompt: str, provider: str | None = None, timeout: int | None = None) -> str:
    """設定（JARVIS_LLM_PROVIDER）のプロバイダーでプロンプトを実行する。失敗時は LLMError。

    timeout（秒）は claude -p の上限。省略すると従来の既定（CLAUDE_CLI_TIMEOUT_SECONDS）。"""
    name = provider or LLM_PROVIDER
    if name not in PROVIDERS:
        raise LLMError(f"未知の LLM プロバイダーです: {name}（{', '.join(PROVIDERS)}）")
    return PROVIDERS[name](prompt, timeout=timeout) if timeout else PROVIDERS[name](prompt)


def _stream_claude_cli(prompt: str, on_text, timeout: int | None = None) -> str:
    """claude -p の逐次出力（stream-json）。差分のテキストを on_text に渡し、最後に全文を返す（FR-014、T-1212）。"""
    import selectors
    import time

    timeout = timeout or CLAUDE_CLI_TIMEOUT_SECONDS
    cmd = [CLAUDE_BIN, "-p", "--tools", "", "--no-session-persistence", "--output-format", "stream-json",
           "--verbose", "--include-partial-messages"]
    deadline = time.monotonic() + timeout
    with tempfile.TemporaryDirectory(prefix="jarvis-llm-") as workdir:
        try:
            proc = subprocess.Popen(cmd, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                    text=True, cwd=workdir)
        except FileNotFoundError:
            raise LLMError(f"Claude Code CLI が見つかりません: {CLAUDE_BIN}") from None
        proc.stdin.write(prompt)
        proc.stdin.close()
        selector = selectors.DefaultSelector()
        selector.register(proc.stdout, selectors.EVENT_READ)
        result = None
        try:
            while True:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise LLMError(f"Claude Code CLI が {timeout} 秒でタイムアウトしました")
                if not selector.select(timeout=remaining):
                    continue
                line = proc.stdout.readline()
                if not line:
                    break
                try:
                    event = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if event.get("type") == "stream_event":
                    delta = (event.get("event") or {}).get("delta") or {}
                    if delta.get("type") == "text_delta" and delta.get("text"):
                        on_text(delta["text"])
                elif event.get("type") == "result":
                    result = event
        finally:
            selector.close()
            if proc.poll() is None:
                proc.kill()
            proc.wait()
    if result is None:
        detail = (proc.stderr.read() if proc.stderr else "").strip()[:500]
        raise LLMError(f"Claude Code CLI が終了コード {proc.returncode} で終了しました（利用上限の可能性あり）: {detail}")
    if result.get("is_error") or result.get("subtype") != "success":
        raise LLMError(f"Claude Code CLI がエラーを返しました: subtype={result.get('subtype')} "
                       f"result={str(result.get('result', ''))[:200]}")
    return result.get("result", "")


def complete_stream(prompt: str, on_text, provider: str | None = None, timeout: int | None = None) -> str:
    """complete() の逐次表示版。claude_cli では生成中の差分を on_text に渡す。
    逐次出力のないプロバイダーは、全文を一度だけ on_text に渡す。失敗時は LLMError（別のプロバイダーに切り替えない）。"""
    name = provider or LLM_PROVIDER
    if name == "claude_cli" and PROVIDERS.get(name) is _complete_claude_cli:
        return _stream_claude_cli(prompt, on_text, timeout)
    text = complete(prompt, provider=provider, timeout=timeout)
    on_text(text)
    return text
