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

-- CKC（Conversation Knowledge Collector、../docs/spec.md §3.3）: 取り込んだ会話と、承認前の候補。
-- 会話の原文は data/ckc/raw/<conversation_id>.json に置き、ここには参照だけを持つ（ADR-0018）。
CREATE TABLE IF NOT EXISTS ckc_conversations (
    conversation_id TEXT PRIMARY KEY,
    origin TEXT NOT NULL,
    title TEXT,
    external_id TEXT,
    content_hash TEXT NOT NULL UNIQUE,
    occurred_at TEXT,
    captured_at TEXT NOT NULL,
    raw_path TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS ckc_candidates (
    candidate_id TEXT PRIMARY KEY,
    conversation_id TEXT NOT NULL,
    kind TEXT NOT NULL,
    title TEXT NOT NULL,
    body TEXT NOT NULL,
    projects TEXT NOT NULL,        -- JSON 配列
    category TEXT,
    confidence TEXT NOT NULL,
    evidence TEXT NOT NULL,        -- JSON 配列 [{quote, speaker}]
    status TEXT NOT NULL,
    resource_id TEXT,
    similar TEXT NOT NULL,         -- JSON 配列（ckc_id）
    supersedes TEXT,               -- 置き換える既存知識の ckc_id（Owner が選んだ場合）
    ckc_id TEXT,                   -- 保存後の知識の ID
    note_relpath TEXT,             -- 保存後の Vault 内の相対パス
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    FOREIGN KEY (conversation_id) REFERENCES ckc_conversations(conversation_id)
);

CREATE INDEX IF NOT EXISTS idx_ckc_candidates_status ON ckc_candidates(status);
CREATE INDEX IF NOT EXISTS idx_ckc_candidates_conversation ON ckc_candidates(conversation_id);

-- 会話の記憶。原文 JSONL から作り直せる索引（T-2101）。
CREATE TABLE IF NOT EXISTS memory_sessions (
    session_id TEXT PRIMARY KEY,
    channel TEXT NOT NULL,
    started_at TEXT NOT NULL,
    last_at TEXT NOT NULL,
    ended_at TEXT,
    owner_turns INTEGER NOT NULL DEFAULT 0,
    summary TEXT,
    summary_status TEXT NOT NULL DEFAULT 'none',
    summary_tries INTEGER NOT NULL DEFAULT 0,
    summarized_at TEXT
);
CREATE TABLE IF NOT EXISTS memory_turns (
    session_id TEXT NOT NULL,
    seq INTEGER NOT NULL,
    speaker TEXT NOT NULL,
    text TEXT NOT NULL,
    created_at TEXT NOT NULL,
    PRIMARY KEY (session_id, seq)
);
