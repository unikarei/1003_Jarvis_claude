"""ナレッジグラフ（nodes/edges）の初期化とCRUDヘルパー。"""

import json
import sqlite3
import uuid
from datetime import datetime

from config import DB_PATH, SCHEMA_PATH

NODE_TYPES = {"Category", "Task", "Knowledge", "Source", "Skill", "Agent"}
EDGE_TYPES = {"belongs_to", "produced", "derived_from", "depends_on", "uses", "executed_by"}


def get_connection():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
    conn = get_connection()
    with open(SCHEMA_PATH, "r", encoding="utf-8") as f:
        conn.executescript(f.read())
    conn.commit()
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
    conn = get_connection()
    conn.execute(
        "INSERT INTO nodes (id, type, category, title, content, url, created_at, meta) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        (
            node_id, node_type, category, title, content, url,
            datetime.now().isoformat(), json.dumps(meta or {}, ensure_ascii=False),
        ),
    )
    conn.commit()
    conn.close()
    return node_id


def insert_edge(src_id: str, dst_id: str, edge_type: str) -> int:
    if edge_type not in EDGE_TYPES:
        raise ValueError(f"unknown edge type: {edge_type}")
    conn = get_connection()
    cur = conn.execute(
        "INSERT INTO edges (src_id, dst_id, type, created_at) VALUES (?, ?, ?, ?)",
        (src_id, dst_id, edge_type, datetime.now().isoformat()),
    )
    conn.commit()
    edge_id = cur.lastrowid
    conn.close()
    return edge_id


def get_node(node_id: str) -> sqlite3.Row | None:
    conn = get_connection()
    row = conn.execute("SELECT * FROM nodes WHERE id = ?", (node_id,)).fetchone()
    conn.close()
    return row


def find_source_by_url(url: str) -> sqlite3.Row | None:
    conn = get_connection()
    row = conn.execute(
        "SELECT * FROM nodes WHERE type = 'Source' AND url = ?", (url,)
    ).fetchone()
    conn.close()
    return row


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
    conn = get_connection()
    rows = conn.execute(query, params).fetchall()
    conn.close()
    return rows


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
    conn = get_connection()
    rows = conn.execute(query, params).fetchall()
    conn.close()
    return rows


def get_source_for_knowledge(knowledge_id: str) -> sqlite3.Row | None:
    conn = get_connection()
    row = conn.execute(
        "SELECT n.* FROM nodes n "
        "JOIN edges e ON e.dst_id = n.id "
        "WHERE e.src_id = ? AND e.type = 'derived_from' AND n.type = 'Source'",
        (knowledge_id,),
    ).fetchone()
    conn.close()
    return row
