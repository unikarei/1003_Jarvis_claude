"""Claude APIへの問い合わせを共通化するヘルパー。"""

import anthropic

from agent_log import log_event
from config import ANTHROPIC_API_KEY, ESCALATION_MODEL, SUMMARY_MODEL

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
