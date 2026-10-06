"""未要約のSourceノードをClaude APIで要約し、Knowledge/Taskノードを作る。"""

from db import get_unsummarized_sources, insert_edge, insert_node
from llm import ask_claude

SUMMARY_PROMPT = (
    "以下の記事タイトルと概要から、日本語で2〜3文の要約を作成してください。"
    "要約のみを出力し、前置きや見出しは不要です。\n\n"
    "タイトル: {title}\n概要: {content}"
)


def summarize_text(title: str, content: str) -> str:
    return ask_claude(SUMMARY_PROMPT.format(title=title, content=content), max_tokens=300)


def summarize_all() -> list[str]:
    """未要約のSourceをすべて要約し、作成したKnowledgeノードIDの一覧を返す。"""
    sources = get_unsummarized_sources()
    if not sources:
        return []

    task_id = insert_node(
        node_type="Task",
        category=sources[0]["category"],
        title="ニュース要約タスク",
        content=f"{len(sources)}件のSourceを要約",
    )

    knowledge_ids = []
    for source in sources:
        summary = summarize_text(source["title"] or "", source["content"] or "")
        knowledge_id = insert_node(
            node_type="Knowledge",
            category=source["category"],
            title=source["title"],
            content=summary,
            url=source["url"],
        )
        insert_edge(knowledge_id, source["id"], "derived_from")
        insert_edge(task_id, knowledge_id, "produced")
        knowledge_ids.append(knowledge_id)

    return knowledge_ids


if __name__ == "__main__":
    new_knowledge_ids = summarize_all()
    print(f"新規Knowledge: {len(new_knowledge_ids)}件")
    for kid in new_knowledge_ids:
        print(f"  {kid}")
