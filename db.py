"""ナレッジグラフ（nodes/edges）の初期化とCRUDヘルパー。"""

import json
import sqlite3
import uuid
from datetime import datetime, timedelta

from config import DB_PATH, SCHEMA_PATH

NODE_TYPES = {"Category", "Task", "Knowledge", "Source", "Skill", "Agent"}
EDGE_TYPES = {"belongs_to", "produced", "derived_from", "depends_on", "uses", "executed_by"}


def get_connection():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def _fetchall(query: str, params=()) -> list[sqlite3.Row]:
    conn = get_connection()
    try:
        return conn.execute(query, params).fetchall()
    finally:
        conn.close()


def _fetchone(query: str, params=()) -> sqlite3.Row | None:
    conn = get_connection()
    try:
        return conn.execute(query, params).fetchone()
    finally:
        conn.close()


def _execute(query: str, params=()) -> sqlite3.Cursor:
    conn = get_connection()
    try:
        cur = conn.execute(query, params)
        conn.commit()
        return cur
    finally:
        conn.close()


def init_db():
    conn = get_connection()
    try:
        with open(SCHEMA_PATH, "r", encoding="utf-8") as f:
            conn.executescript(f.read())
        conn.commit()
    finally:
        conn.close()


def generate_id(node_type: str, category: str | None = None) -> str:
    prefix = node_type.lower()
    cat = category or "none"
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    short_id = uuid.uuid4().hex[:4]
    return f"{prefix}_{cat}_{timestamp}_{short_id}"


def insert_node(node_type: str, category: str | None = None, title: str | None = None,
                content: str | None = None, url: str | None = None, meta: dict | None = None,
                node_id: str | None = None) -> str:
    if node_type not in NODE_TYPES:
        raise ValueError(f"unknown node type: {node_type}")
    node_id = node_id or generate_id(node_type, category)
    _execute(
        "INSERT INTO nodes (id, type, category, title, content, url, created_at, meta) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        (
            node_id, node_type, category, title, content, url,
            datetime.now().isoformat(), json.dumps(meta or {}, ensure_ascii=False),
        ),
    )
    return node_id


def insert_edge(src_id: str, dst_id: str, edge_type: str) -> int:
    if edge_type not in EDGE_TYPES:
        raise ValueError(f"unknown edge type: {edge_type}")
    cur = _execute(
        "INSERT INTO edges (src_id, dst_id, type, created_at) VALUES (?, ?, ?, ?)",
        (src_id, dst_id, edge_type, datetime.now().isoformat()),
    )
    return cur.lastrowid


def get_node(node_id: str) -> sqlite3.Row | None:
    return _fetchone("SELECT * FROM nodes WHERE id = ?", (node_id,))


def find_source_by_url(url: str) -> sqlite3.Row | None:
    return _fetchone("SELECT * FROM nodes WHERE type = 'Source' AND url = ?", (url,))


def get_recent_knowledge(category: str | None = None, since: str | None = None) -> list[sqlite3.Row]:
    query = "SELECT * FROM nodes WHERE type = 'Knowledge'"
    params: list[str] = []
    if category:
        query += " AND category = ?"
        params.append(category)
    if since:
        query += " AND created_at >= ?"
        params.append(since)
    query += " ORDER BY created_at DESC"
    return _fetchall(query, params)


def search_knowledge(keyword: str | None = None, since_days: int = 14, limit: int = 20) -> list[sqlite3.Row]:
    """質問文でKnowledgeをLIKE検索する。0件の場合は直近since_days日の最新Knowledgeにフォールバックする。"""
    since = (datetime.now() - timedelta(days=since_days)).isoformat()
    base_query = "SELECT * FROM nodes WHERE type = 'Knowledge' AND created_at >= ?"

    if keyword:
        rows = _fetchall(
            f"{base_query} AND (title LIKE ? OR content LIKE ?) ORDER BY created_at DESC LIMIT ?",
            (since, f"%{keyword}%", f"%{keyword}%", limit),
        )
        if rows:
            return rows

    return _fetchall(f"{base_query} ORDER BY created_at DESC LIMIT ?", (since, limit))


def get_unsummarized_sources(category: str | None = None) -> list[sqlite3.Row]:
    query = (
        "SELECT * FROM nodes WHERE type = 'Source' AND id NOT IN "
        "(SELECT dst_id FROM edges WHERE type = 'derived_from')"
    )
    params: list[str] = []
    if category:
        query += " AND category = ?"
        params.append(category)
    query += " ORDER BY created_at ASC"
    return _fetchall(query, params)


def get_source_for_knowledge(knowledge_id: str) -> sqlite3.Row | None:
    return _fetchone(
        "SELECT n.* FROM nodes n "
        "JOIN edges e ON e.dst_id = n.id "
        "WHERE e.src_id = ? AND e.type = 'derived_from' AND n.type = 'Source'",
        (knowledge_id,),
    )


def get_or_create_agent(name: str) -> str:
    """名前が一致するAgentノードを探し、なければ作成してidを返す（委譲先エージェントの識別に使う）。"""
    row = _fetchone("SELECT * FROM nodes WHERE type = 'Agent' AND title = ?", (name,))
    if row:
        return row["id"]
    return insert_node(node_type="Agent", title=name)


def find_skill_by_title(title: str) -> sqlite3.Row | None:
    """タイトルが部分一致するSkillノードを探す（同種タスクの再実行時に既存Skillを再利用するため）。"""
    return _fetchone(
        "SELECT * FROM nodes WHERE type = 'Skill' AND title LIKE ? ORDER BY created_at DESC LIMIT 1",
        (f"%{title}%",),
    )
