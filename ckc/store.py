"""CKC の記録: 取り込んだ会話（原文は data/ckc/raw/）と承認前の候補（jarvis.db）。"""

import json
import os
from contextlib import contextmanager
from dataclasses import asdict
from pathlib import Path

import config
from ckc.models import (Candidate, CandidateStatus, Confidence, Conversation, Evidence, Kind, Origin, Speaker,
                        Turn, now_iso)
from db import get_connection

# 状態の遷移（承認の取り消し・却下の見直しは可。保存済みは変えない）
TRANSITIONS = {
    CandidateStatus.PENDING: {CandidateStatus.APPROVED, CandidateStatus.REJECTED},
    CandidateStatus.APPROVED: {CandidateStatus.PENDING, CandidateStatus.REJECTED, CandidateStatus.PUBLISHED},
    CandidateStatus.REJECTED: {CandidateStatus.PENDING},
    CandidateStatus.PUBLISHED: set(),
}


class StoreError(Exception):
    pass


@contextmanager
def _db():
    """成功したらコミットし、必ず閉じる接続。"""
    conn = get_connection()
    try:
        with conn:
            yield conn
    finally:
        conn.close()


# --- 会話 ---

def _raw_path(conversation_id: str) -> Path:
    return config.CKC_DATA_DIR / "raw" / f"{conversation_id}.json"


def find_conversation_by_hash(content_hash: str) -> str | None:
    with _db() as conn:
        row = conn.execute("SELECT conversation_id FROM ckc_conversations WHERE content_hash = ?",
                           (content_hash,)).fetchone()
    return row["conversation_id"] if row else None


def save_conversation(conv: Conversation) -> Path:
    """原文を権限 600 のファイルに保存し、会話を登録する。同じ内容の会話が既にあれば StoreError。"""
    if find_conversation_by_hash(conv.content_hash()):
        raise StoreError("同じ内容の会話は取り込み済みです")
    path = _raw_path(conv.conversation_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        json.dump(asdict(conv), f, ensure_ascii=False, indent=2)
    with _db() as conn:
        conn.execute(
            "INSERT INTO ckc_conversations (conversation_id, origin, title, external_id, content_hash,"
            " occurred_at, captured_at, raw_path) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (conv.conversation_id, conv.origin.value, conv.title, conv.external_id, conv.content_hash(),
             conv.occurred_at, conv.captured_at, str(path)),
        )
    return path


def load_conversation(conversation_id: str) -> Conversation:
    with _db() as conn:
        row = conn.execute("SELECT raw_path FROM ckc_conversations WHERE conversation_id = ?",
                           (conversation_id,)).fetchone()
    if row is None:
        raise StoreError(f"会話が見つかりません: {conversation_id}")
    data = json.loads(Path(row["raw_path"]).read_text(encoding="utf-8"))
    data["origin"] = Origin(data["origin"])
    data["turns"] = [Turn(Speaker(t["speaker"]), t["text"], t.get("timestamp")) for t in data["turns"]]
    return Conversation(**data)


# --- 候補 ---

def _row_to_candidate(row) -> Candidate:
    return Candidate(
        candidate_id=row["candidate_id"], conversation_id=row["conversation_id"], kind=Kind(row["kind"]),
        title=row["title"], body=row["body"], projects=json.loads(row["projects"]), category=row["category"],
        confidence=Confidence(row["confidence"]),
        evidence=[Evidence(e["quote"], Speaker(e["speaker"])) for e in json.loads(row["evidence"])],
        status=CandidateStatus(row["status"]), resource_id=row["resource_id"], similar=json.loads(row["similar"]),
    )


def _candidate_values(c: Candidate) -> tuple:
    return (c.kind.value, c.title, c.body, json.dumps(c.projects, ensure_ascii=False), c.category,
            c.confidence.value, json.dumps([{"quote": e.quote, "speaker": e.speaker.value} for e in c.evidence],
                                           ensure_ascii=False),
            c.resource_id, json.dumps(c.similar, ensure_ascii=False))


def add_candidates(candidates: list[Candidate]) -> None:
    now = now_iso()
    with _db() as conn:
        conn.executemany(
            "INSERT INTO ckc_candidates (candidate_id, conversation_id, kind, title, body, projects, category,"
            " confidence, evidence, resource_id, similar, status, created_at, updated_at)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            [(c.candidate_id, c.conversation_id, *_candidate_values(c), c.status.value, now, now)
             for c in candidates],
        )


def get_candidate(candidate_id: str) -> Candidate:
    row = get_candidate_row(candidate_id)
    return _row_to_candidate(row)


def get_candidate_row(candidate_id: str):
    with _db() as conn:
        row = conn.execute("SELECT * FROM ckc_candidates WHERE candidate_id = ?", (candidate_id,)).fetchone()
    if row is None:
        raise StoreError(f"候補が見つかりません: {candidate_id}")
    return row


def list_candidates(status: CandidateStatus | None = None, conversation_id: str | None = None) -> list[Candidate]:
    query, params = "SELECT * FROM ckc_candidates WHERE 1=1", []
    if status is not None:
        query += " AND status = ?"
        params.append(status.value)
    if conversation_id is not None:
        query += " AND conversation_id = ?"
        params.append(conversation_id)
    with _db() as conn:
        rows = conn.execute(query + " ORDER BY created_at, rowid", params).fetchall()
    return [_row_to_candidate(r) for r in rows]


def update_candidate(c: Candidate) -> None:
    """題名・本文などの修正を保存する。保存済みの候補は変えない。"""
    if get_candidate(c.candidate_id).status == CandidateStatus.PUBLISHED:
        raise StoreError("保存済みの候補は修正できません")
    with _db() as conn:
        conn.execute(
            "UPDATE ckc_candidates SET kind = ?, title = ?, body = ?, projects = ?, category = ?, confidence = ?,"
            " evidence = ?, resource_id = ?, similar = ?, updated_at = ? WHERE candidate_id = ?",
            (*_candidate_values(c), now_iso(), c.candidate_id),
        )


def set_status(candidate_id: str, status: CandidateStatus, *, supersedes: str | None = None,
               ckc_id: str | None = None, note_relpath: str | None = None) -> None:
    current = get_candidate(candidate_id).status
    if status not in TRANSITIONS[current]:
        raise StoreError(f"状態を {current.value} から {status.value} に変えられません")
    with _db() as conn:
        conn.execute(
            "UPDATE ckc_candidates SET status = ?, supersedes = COALESCE(?, supersedes), ckc_id = COALESCE(?, ckc_id),"
            " note_relpath = COALESCE(?, note_relpath), updated_at = ? WHERE candidate_id = ?",
            (status.value, supersedes, ckc_id, note_relpath, now_iso(), candidate_id),
        )
