"""コードタスク（生成・リファクタリング・デバッグ）をOpenAI APIに委譲するサブエージェント。

オーケストレーターからの委譲を受けて結果のみを返す（CLAUDE.md「エージェント・APIルーティング方針」参照）。
"""

from openai import OpenAI

from agent_log import log_event
from config import OPENAI_API_KEY

CODE_MODEL = "gpt-4o-mini"  # 低単価・コード特化レイヤー

SYSTEM_PROMPT = (
    "あなたはコード生成・リファクタリング・デバッグを専門に行うサブエージェントです。"
    "受け取ったタスクに対して、説明は最小限にし、実用的なコードまたは修正内容を返してください。"
)


def run_code_task(task: str) -> str:
    """コードタスクをOpenAI APIに委譲し、結果のテキストを返す。"""
    if not OPENAI_API_KEY:
        raise RuntimeError("OPENAI_API_KEYが設定されていません。.envに追加してください。")

    client = OpenAI(api_key=OPENAI_API_KEY)
    response = client.chat.completions.create(
        model=CODE_MODEL,
        messages=[
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": task},
        ],
    )
    result = response.choices[0].message.content.strip()

    log_event("code_delegation", model=CODE_MODEL, task_preview=task[:200])
    return result


if __name__ == "__main__":
    import sys

    if len(sys.argv) < 2:
        print("使い方: python code_agent.py <コードタスクの説明>")
        sys.exit(1)

    print(run_code_task(" ".join(sys.argv[1:])))
