"""タスクの説明文を受け取り、コード関連かナレッジ関連かに分類して実行し、
Task→uses→Skill、Task→executed_by→Agentのグラフを記録するCLI。
"""

import json
import sys

from ask import answer as ask_answer
from code_agent import run_code_task
from db import (find_skill_by_name, find_skill_by_title, get_or_create_agent, insert_edge, insert_node,
                update_node_meta)

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


# FR-014 より前に作られた Skill ノード（題名で記録していた）と、jarvis-skills のスキル名の対応
LEGACY_SKILL_TITLES = {"knowledge-answer": KNOWLEDGE_SKILL_TITLE, "code-task": CODE_SKILL_TITLE}


def skill_node(name: str) -> str:
    """スキル名に対応する Skill ノードの ID。既存の題名のノードがあれば、それにスキル名を結び付けて再利用する。"""
    row = find_skill_by_name(name)
    if row:
        return row["id"]
    legacy = LEGACY_SKILL_TITLES.get(name)
    row = find_skill_by_title(legacy) if legacy else None
    if row:
        meta = json.loads(row["meta"] or "{}")
        update_node_meta(row["id"], {**meta, "skill_name": name})
        return row["id"]
    return insert_node(node_type="Skill", title=name, meta={"skill_name": name})


def record(text: str, outcome) -> str:
    """会話でのスキルの実行を記録する（Task → uses → Skill、Task → executed_by → Agent。FR-014、T-1209）。"""
    from ckc.secrets import mask_secrets   # 発言や結果に貼られた秘密情報を記録に残さない（FR-014 F-4）

    agent = CODE_AGENT_NAME if outcome.skill == "code-task" else ORCHESTRATOR_AGENT_NAME
    if getattr(outcome, "private", False):
        # 発言・ノートの本文を残さない（T-1702）: 題名は「スキル名: 操作」、本文は結果の短い定型文、
        # 入力は操作・Vault・パスだけ
        given = outcome.inputs or {}
        inputs = {k: given.get(k) for k in ("op", "vault", "path")}
        title = f"{outcome.skill}: {given.get('op') or '-'}"
        content = given.get("result") or ("終わりました" if outcome.ok else "できませんでした")
        if not isinstance(content, str) or len(content) > 80:
            content = "終わりました" if outcome.ok else "できませんでした"
        task_id = insert_node(node_type="Task", title=title, content=mask_secrets(content), meta={
            "skill": outcome.skill, "method": outcome.method, "ok": outcome.ok, "error": outcome.error,
            "seconds": outcome.seconds, "inputs": inputs, "private": True})
        insert_edge(task_id, skill_node(outcome.skill), "uses")
        insert_edge(task_id, get_or_create_agent(agent), "executed_by")
        return task_id
    inputs = {k: mask_secrets(v) if isinstance(v, str) else v for k, v in outcome.inputs.items()}
    task_id = insert_node(node_type="Task", title=mask_secrets(text), content=mask_secrets(outcome.text), meta={
        "skill": outcome.skill, "method": outcome.method, "ok": outcome.ok, "error": outcome.error,
        "seconds": outcome.seconds, "inputs": inputs})
    insert_edge(task_id, skill_node(outcome.skill), "uses")
    insert_edge(task_id, get_or_create_agent(agent), "executed_by")
    return task_id


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
