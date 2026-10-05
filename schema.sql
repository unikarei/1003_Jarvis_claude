-- JARVIS ナレッジグラフ スキーマ
-- ノード型: Category / Task / Knowledge / Source / Skill / Agent
-- エッジ型: belongs_to / produced / derived_from / depends_on / uses / executed_by

CREATE TABLE IF NOT EXISTS nodes (
    id TEXT PRIMARY KEY,
    type TEXT NOT NULL,
    category TEXT,
    title TEXT,
    content TEXT,
    url TEXT,
    created_at TEXT NOT NULL,
    meta TEXT
);

CREATE TABLE IF NOT EXISTS edges (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    src_id TEXT NOT NULL,
    dst_id TEXT NOT NULL,
    type TEXT NOT NULL,
    created_at TEXT NOT NULL,
    FOREIGN KEY (src_id) REFERENCES nodes(id),
    FOREIGN KEY (dst_id) REFERENCES nodes(id)
);

CREATE INDEX IF NOT EXISTS idx_nodes_type ON nodes(type);
CREATE INDEX IF NOT EXISTS idx_nodes_category ON nodes(category);
CREATE INDEX IF NOT EXISTS idx_nodes_url ON nodes(url);
CREATE INDEX IF NOT EXISTS idx_edges_src ON edges(src_id);
CREATE INDEX IF NOT EXISTS idx_edges_dst ON edges(dst_id);
CREATE INDEX IF NOT EXISTS idx_edges_type ON edges(type);
