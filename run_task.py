"""タスクの説明文を受け取り、コード関連かナレッジ関連かに分類して実行し、
Task→uses→Skill、Task→executed_by→Agentのグラフを記録するCLI。
"""

import sys

from ask import answer as ask_answer
from code_agent import run_code_task
from db import find_skill_by_title, get_or_create_agent, insert_edge, insert_node

CODE_KEYWORDS = ("コード", "実装", "関数", "バグ", "デバッグ", "スクリプト", "プログラム", "リファクタ")

CODE_SKILL_TITLE = "コードタスク実行（OpenAIサブエージェント委譲）"
KNOWLEDGE_SKILL_TITLE = "ナレッジ質問応答（Knowledge検索+Claude回答）"

CODE_AGENT_NAME = "code_agent"
ORCHESTRATOR_AGENT_NAME = "orchestrator"


def is_code_task(task: str) -> bool:
    return any(keyword in task for keyword in CODE_KEYWORDS)


def _get_or_create_skill(title: str) -> str:
    existing = find_skill_by_title(title)
    if existing:
        return existing["id"]
    return insert_node(node_type="Skill", title=title)


def run_task(task: str) -> str:
    """タスクを分類・実行し、Task/Skill/Agentのグラフを記録して結果テキストを返す。"""
    if is_code_task(task):
        result = run_code_task(task)
        agent_id = get_or_create_agent(CODE_AGENT_NAME)
        skill_id = _get_or_create_skill(CODE_SKILL_TITLE)
    else:
        result = ask_answer(task)
        agent_id = get_or_create_agent(ORCHESTRATOR_AGENT_NAME)
        skill_id = _get_or_create_skill(KNOWLEDGE_SKILL_TITLE)

    task_id = insert_node(node_type="Task", title=task, content=result)
    insert_edge(task_id, skill_id, "uses")
    insert_edge(task_id, agent_id, "executed_by")

    return result


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("使い方: python run_task.py <タスクの説明>")
        sys.exit(1)

    print(run_task(" ".join(sys.argv[1:])))
