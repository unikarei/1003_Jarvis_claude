"""会話でのスキルの実行の記録（FR-014、T-1209）のテスト。DB は一時フォルダ。"""

import json

import db
import orchestrator as orc
import run_task


def _outcome(skill="knowledge-search", ok=True, error=None, method="rule"):
    return orc.Outcome(skill, method, {"query": "補助金"}, "結果の文章", ok, 1.25, [], error)


def _skill_nodes():
    return db._fetchall("SELECT * FROM nodes WHERE type = 'Skill'")


def test_record_creates_task_with_edges_and_meta(db_path):
    db.init_db()
    task_id = run_task.record("補助金を探して", _outcome())
    task = db.get_node(task_id)
    meta = json.loads(task["meta"])
    assert task["type"] == "Task" and task["title"] == "補助金を探して" and task["content"] == "結果の文章"
    assert meta == {"skill": "knowledge-search", "method": "rule", "ok": True, "error": None, "seconds": 1.25,
                    "inputs": {"query": "補助金"}}
    edges = {e["type"]: e["dst_id"] for e in db._fetchall("SELECT * FROM edges WHERE src_id = ?", (task_id,))}
    skill = db.get_node(edges["uses"])
    assert skill["title"] == "knowledge-search" and json.loads(skill["meta"])["skill_name"] == "knowledge-search"
    assert db.get_node(edges["executed_by"])["title"] == "orchestrator"


def test_skill_node_is_reused(db_path):
    db.init_db()
    run_task.record("a", _outcome())
    run_task.record("b", _outcome(ok=False, error="RuntimeError"))
    assert len(_skill_nodes()) == 1


def test_legacy_skill_nodes_are_linked(db_path):
    db.init_db()
    legacy_answer = db.insert_node("Skill", title=run_task.KNOWLEDGE_SKILL_TITLE)
    legacy_code = db.insert_node("Skill", title=run_task.CODE_SKILL_TITLE)
    t1 = run_task.record("質問", _outcome("knowledge-answer", method="default"))
    t2 = run_task.record("関数を書いて", _outcome("code-task"))
    uses = lambda t: db._fetchone("SELECT dst_id FROM edges WHERE src_id = ? AND type = 'uses'", (t,))["dst_id"]
    assert uses(t1) == legacy_answer and uses(t2) == legacy_code
    assert json.loads(db.get_node(legacy_answer)["meta"])["skill_name"] == "knowledge-answer"
    assert len(_skill_nodes()) == 2
    executed = db._fetchone("SELECT dst_id FROM edges WHERE src_id = ? AND type = 'executed_by'", (t2,))["dst_id"]
    assert db.get_node(executed)["title"] == "code_agent"
