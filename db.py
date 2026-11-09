"""ナレッジグラフ（nodes/edges）の初期化とCRUDヘルパー。"""

import json
import sqlite3
import unicodedata
import uuid
from datetime import datetime, timedelta

from janome.tokenizer import Tokenizer

from config import DB_PATH, SCHEMA_PATH

_tokenizer = Tokenizer()

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
    given = node_id is not None
    for attempt in range(5):
        if not given:
            node_id = generate_id(node_type, category)
        try:
            _execute(
                "INSERT INTO nodes (id, type, category, title, content, url, created_at, meta) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    node_id, node_type, category, title, content, url,
                    datetime.now().isoformat(), json.dumps(meta or {}, ensure_ascii=False),
                ),
            )
            return node_id
        except sqlite3.IntegrityError:
            # 同じ秒に作った ID の短縮部分（4 桁）が重なったときだけ作り直す。指定された ID の重複はそのまま知らせる
            if given or attempt == 4 or get_node(node_id) is None:
                raise


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


def update_node_content(node_id: str, content: str) -> None:
    _execute("UPDATE nodes SET content = ? WHERE id = ?", (content, node_id))


def update_node_title(node_id: str, title: str) -> None:
    _execute("UPDATE nodes SET title = ? WHERE id = ?", (title, node_id))


def update_node_meta(node_id: str, meta: dict) -> None:
    _execute("UPDATE nodes SET meta = ? WHERE id = ?", (json.dumps(meta, ensure_ascii=False), node_id))


def find_nodes_by_title_prefix(node_type: str, title_prefix: str) -> list[sqlite3.Row]:
    return _fetchall(
        "SELECT * FROM nodes WHERE type = ? AND title LIKE ? ORDER BY created_at ASC",
        (node_type, f"{title_prefix}%"),
    )


def find_knowledge_by_url(url: str) -> sqlite3.Row | None:
    return _fetchone("SELECT * FROM nodes WHERE type = 'Knowledge' AND url = ?", (url,))


def find_source_by_url(url: str) -> sqlite3.Row | None:
    return _fetchone("SELECT * FROM nodes WHERE type = 'Source' AND url = ?", (url,))


def normalize_title(title: str | None) -> str:
    """題名の比較用の形（NFKC で全角スペースなどをそろえ、連続する空白を1つにする）。"""
    return " ".join(unicodedata.normalize("NFKC", title or "").split())


def find_recent_source_by_title(category: str, title: str, days: int = 30) -> sqlite3.Row | None:
    """同じカテゴリ・同じ題名の Source を直近 days 日から探す（URL を変えて再配信される記事の重複を防ぐ）。

    保存済みの題名も同じ規則で正規化して比べる（空白をそろえる前に保存した題名に全角スペースが残っていて、
    完全一致では見つからなかった。2026-10-16）。"""
    since = (datetime.now() - timedelta(days=days)).isoformat()
    key = normalize_title(title)
    for row in _fetchall("SELECT * FROM nodes WHERE type = 'Source' AND category = ? AND created_at >= ?",
                         (category, since)):
        if normalize_title(row["title"]) == key:
            return row
    return None


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


def extract_keywords(text: str) -> list[str]:
    """質問文から名詞（固有名詞含む）を抽出する。質問文全体をそのままLIKE検索すると
    自然文はほぼ一致しないため、検索語として使える語だけを取り出す。
    """
    keywords = []
    for token in _tokenizer.tokenize(text):
        pos = token.part_of_speech.split(",")[0]
        if pos == "名詞" and len(token.surface) >= 2:
            keywords.append(token.surface)
    return list(dict.fromkeys(keywords))


def _keyword_match_clause(keywords: list[str]) -> tuple[str, list[str]]:
    clause = " OR ".join(["(title LIKE ? OR content LIKE ?)"] * len(keywords))
    params = [p for kw in keywords for p in (f"%{kw}%", f"%{kw}%")]
    return clause, params


def _search_by_keywords(keywords: list[str], limit: int) -> list[sqlite3.Row]:
    """いずれかのキーワードを含むKnowledgeを取得し、一致キーワード数が多い順に並べ替える。
    「結果」「判断」のような一般的な単語1つだけの一致が、複数語が一致する本命より
    上位に来ないようにするため。
    """
    clause, params = _keyword_match_clause(keywords)
    candidates = _fetchall(
        f"SELECT * FROM nodes WHERE type = 'Knowledge' AND ({clause}) "
        "ORDER BY created_at DESC LIMIT 200",
        params,
    )

    def match_count(row: sqlite3.Row) -> int:
        text = f"{row['title']} {row['content']}"
        return sum(1 for kw in keywords if kw in text)

    ranked = sorted(candidates, key=match_count, reverse=True)
    return ranked[:limit]


def search_knowledge(keyword: str | None = None, since_days: int = 14, limit: int = 20) -> list[sqlite3.Row]:
    """質問文からキーワードを抽出してKnowledgeをLIKE検索する（全履歴対象。ニュースだけでなく
    長期参照用ナレッジも検索するため）。一致キーワード数が多い順に並べる。
    0件の場合は、直近since_days日の最新Knowledgeにフォールバックする（曖昧な質問向け）。
    """
    if keyword:
        keywords = extract_keywords(keyword) or [keyword]
        rows = _search_by_keywords(keywords, limit)
        if rows:
            return rows

    since = (datetime.now() - timedelta(days=since_days)).isoformat()
    return _fetchall(
        "SELECT * FROM nodes WHERE type = 'Knowledge' AND created_at >= ? ORDER BY created_at DESC LIMIT ?",
        (since, limit),
    )


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


def find_skill_by_name(name: str) -> sqlite3.Row | None:
    """jarvis-skills のスキル名（meta の skill_name）に対応する Skill ノード（FR-014）。"""
    return _fetchone(
        "SELECT * FROM nodes WHERE type = 'Skill' AND json_extract(meta, '$.skill_name') = ? "
        "ORDER BY created_at LIMIT 1", (name,))


def find_skill_by_title(title: str) -> sqlite3.Row | None:
    """タイトルが部分一致するSkillノードを探す（同種タスクの再実行時に既存Skillを再利用するため）。"""
    return _fetchone(
        "SELECT * FROM nodes WHERE type = 'Skill' AND title LIKE ? ORDER BY created_at DESC LIMIT 1",
        (f"%{title}%",),
    )


# ---------------------------------------------------------------- 会話からのノートの編集（T-1702）

def find_knowledge_by_note(resource_id: str, relpath: str) -> list[sqlite3.Row]:
    """CKC が保存したノート（meta の resource_id・note_relpath が一致する Knowledge）。"""
    return _fetchall(
        "SELECT * FROM nodes WHERE type = 'Knowledge' AND json_extract(meta, '$.resource_id') = ? "
        "AND json_extract(meta, '$.note_relpath') = ?", (resource_id, relpath))


def deletion_plan(knowledge_ids: list[str]) -> tuple[list[dict], list[dict]]:
    """Knowledge を消すときに一緒に消す行。（nodes の行, edges の行）を dict で返す（バックアップに書く）。
    Source は、消す Knowledge の根拠（derived_from）で、ほかの Knowledge から参照されていないものだけ。
    エッジは、消すノードに付くもの（向きはどちらも）。"""
    if not knowledge_ids:
        return [], []
    marks = ",".join("?" * len(knowledge_ids))
    sources = [r["dst_id"] for r in _fetchall(
        f"SELECT DISTINCT e.dst_id FROM edges e JOIN nodes n ON n.id = e.dst_id "
        f"WHERE e.type = 'derived_from' AND n.type = 'Source' AND e.src_id IN ({marks}) "
        f"AND NOT EXISTS (SELECT 1 FROM edges o WHERE o.type = 'derived_from' AND o.dst_id = e.dst_id "
        f"AND o.src_id NOT IN ({marks}))", (*knowledge_ids, *knowledge_ids))]
    ids = list(dict.fromkeys([*knowledge_ids, *sources]))
    marks = ",".join("?" * len(ids))
    nodes = [dict(r) for r in _fetchall(f"SELECT * FROM nodes WHERE id IN ({marks})", ids)]
    edges = [dict(r) for r in _fetchall(
        f"SELECT * FROM edges WHERE src_id IN ({marks}) OR dst_id IN ({marks})", (*ids, *ids))]
    return nodes, edges


def delete_nodes(node_ids: list[str]) -> int:
    """ノードと、それに付くエッジを 1 つの取引で消す。消したノードの数を返す。失敗したら何も消さない（スキーマは変えない）。"""
    if not node_ids:
        return 0
    marks = ",".join("?" * len(node_ids))
    conn = get_connection()
    try:
        with conn:
            conn.execute(f"DELETE FROM edges WHERE src_id IN ({marks}) OR dst_id IN ({marks})", (*node_ids, *node_ids))
            return conn.execute(f"DELETE FROM nodes WHERE id IN ({marks})", node_ids).rowcount
    finally:
        conn.close()
