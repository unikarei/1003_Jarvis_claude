"""蓄積したKnowledgeを検索し、Claude APIで出典URL付きの回答を生成するCLI。"""

import sys

import anthropic

from config import ANTHROPIC_API_KEY, SUMMARY_MODEL
from db import search_knowledge

ANSWER_PROMPT = """以下は蓄積されたニュース要約（Knowledge）の一覧です。これらの情報のみを根拠に、
質問に日本語で回答してください。回答には根拠とした出典URLを添えてください。
一覧の中に質問に答えられる情報がない場合は、情報がない旨を明示してください（創作しないこと）。

# Knowledge一覧
{context}

# 質問
{question}
"""


def build_context(knowledge_list) -> str:
    lines = []
    for k in knowledge_list:
        lines.append(f"- [{k['created_at']}] {k['title']}\n  要約: {k['content']}\n  出典: {k['url']}")
    return "\n".join(lines) if lines else "(該当するKnowledgeはありません)"


def answer(question: str) -> str:
    knowledge_list = search_knowledge(keyword=question)
    context = build_context(knowledge_list)

    client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)
    message = client.messages.create(
        model=SUMMARY_MODEL,
        max_tokens=1000,
        messages=[{"role": "user", "content": ANSWER_PROMPT.format(context=context, question=question)}],
    )
    return message.content[0].text.strip()


def main():
    if len(sys.argv) > 1:
        question = " ".join(sys.argv[1:])
    else:
        question = input("質問を入力してください: ")

    print(answer(question))


if __name__ == "__main__":
    main()
