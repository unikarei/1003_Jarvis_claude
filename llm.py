"""Claude APIへの問い合わせを共通化するヘルパー。"""

import anthropic

from config import ANTHROPIC_API_KEY, SUMMARY_MODEL


def ask_claude(prompt: str, max_tokens: int = 1000, model: str = SUMMARY_MODEL) -> str:
    client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)
    message = client.messages.create(
        model=model,
        max_tokens=max_tokens,
        messages=[{"role": "user", "content": prompt}],
    )
    return message.content[0].text.strip()
