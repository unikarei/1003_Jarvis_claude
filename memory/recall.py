"""SQLite の索引だけから、回答に添える小さな記憶パックを作る。"""

import json
import math
import sqlite3
from contextlib import closing
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path

import config
import db
from memory import store


@dataclass
class MemoryPack:
    text: str = ""
    items: list[dict] = field(default_factory=list)


def _summary_item(row, body):
    channel = {"text": "文字の会話", "voice": "音声の会話", "web": "ブラウザの会話"}.get(row["channel"], "会話")
    return {"kind": "summary", "session_id": row["session_id"], "date": row["last_at"][:10],
            "text": f'- [会話の要約 {row["last_at"][:10]} {channel}]\n{body}'}


NOTE_KIND_LABELS = {"fact": "事実", "decision": "決定", "preference": "好み"}
NOTE_RECENT = 5      # 記憶パックに入れる新しい記憶のメモ
NOTE_MATCHED = 3     # 質問で BM25 に当たった記憶のメモ
SEARCH_TURN_DAYS = 90
SEARCH_TURN_LIMIT = 3000


def _note_item(row):
    return {"kind": "note", "note_id": row["note_id"], "date": row["created_at"][:10],
            "text": f'- [記憶のメモ {row["created_at"][:10]} {NOTE_KIND_LABELS.get(row["kind"], row["kind"])}] {row["text"]}'}


def _rank(rows, question, field="summary", min_score=None):
    # knowledge_engine.search と同じ名詞抽出・BM25 の式（要約には題名の重みなし）。
    terms = db.extract_keywords(question) or [question.strip()]
    terms = [t for t in terms if t]
    if not rows or not terms:
        return []
    docs = [(row, {t: row[field].count(t) for t in terms}, len(row[field]) or 1) for row in rows]
    avg_len = sum(d[2] for d in docs) / len(docs)
    df = {t: sum(d[1][t] > 0 for d in docs) for t in terms}
    hits = []
    for row, tf, length in docs:
        score = sum(math.log(1 + (len(docs) - df[t] + 0.5) / (df[t] + 0.5)) * tf[t] * 2.2 /
                    (tf[t] + 1.2 * (0.25 + 0.75 * length / avg_len)) for t in terms if tf[t])
        score = round(score, 3)
        if score >= (config.MEMORY_MIN_SCORE if min_score is None else min_score) and score > 0:
            hits.append((score, row))
    hits.sort(key=lambda pair: (pair[0], pair[1]["last_at"] if "last_at" in pair[1].keys() else ""), reverse=True)
    return hits


def _notes(conn):
    """記憶のメモ（新しい順）。表がない古い DB では空（T-2406 の前の DB でも想起を止めない）。"""
    try:
        return list(conn.execute("SELECT * FROM memory_notes ORDER BY created_at DESC, note_id DESC"))
    except sqlite3.OperationalError:
        return []


def recall(question: str, ctx) -> MemoryPack:
    if not config.MEMORY_ENABLED or not store.enabled():
        return MemoryPack()
    # 存在しない DB を新しく作らず、書き込みやスキーマ初期化も行わない。
    uri = Path(db.DB_PATH).resolve().as_uri() + "?mode=ro"
    try:
        return _recall(question, ctx, uri)
    except Exception as error:
        store.warn("想起", error)
        return MemoryPack()


def _recall(question, ctx, uri):
    candidates = []
    with closing(sqlite3.connect(uri, uri=True, timeout=0.02)) as conn:
        conn.row_factory = sqlite3.Row
        for row in conn.execute("SELECT id, title, content, created_at, meta FROM nodes "
                                "WHERE type='Knowledge' AND meta LIKE '%Preference%' ORDER BY created_at DESC, id DESC"):
            try:
                meta = json.loads(row["meta"] or "{}")
            except (TypeError, ValueError):
                continue
            if not isinstance(meta, dict) or meta.get("kind") != "Preference" or meta.get("status") != "active":
                continue
            candidates.append({"kind": "preference", "knowledge_id": row["id"], "date": row["created_at"][:10],
                               "text": f'- [承認済み・好み {row["created_at"][:10]}] {row["title"] or ""}: {(row["content"] or "")[:120]}'})
            if len(candidates) == 5:
                break
        notes = _notes(conn)
        chosen = notes[:NOTE_RECENT]
        for _, row in _rank(notes, question, field="text"):
            if len(chosen) >= NOTE_RECENT + NOTE_MATCHED:
                break
            if row["note_id"] not in {r["note_id"] for r in chosen}:
                chosen.append(row)
        candidates.extend(_note_item(row) for row in sorted(chosen, key=lambda r: r["created_at"], reverse=True))
        sessions = list(conn.execute("SELECT * FROM memory_sessions WHERE session_id != ? ORDER BY last_at DESC, session_id DESC",
                                     (ctx.session_id,)))
        now = datetime.now().astimezone()
        recent = None
        for row in sessions:
            # 終わった会話: 正常に終えたもの、または最後の発言から 30 分たったもの（Ctrl+C・切断・タブを閉じた会話も含む。
            # 要約の対象と同じ基準、spec §3.3.14 B）。日付は終えた時刻、なければ最後の発言の時刻
            last = datetime.fromisoformat(row["last_at"]).astimezone()
            if not row["ended_at"] and now - last < timedelta(minutes=30):
                continue
            ended = datetime.fromisoformat(row["ended_at"]).astimezone() if row["ended_at"] else last
            if timedelta(0) <= now - ended <= timedelta(days=3):
                recent = row
                break
        if recent is not None:
            body = recent["summary"]
            if not body:
                turns = list(conn.execute("SELECT speaker, text FROM memory_turns WHERE session_id=? ORDER BY seq DESC LIMIT 6",
                                          (recent["session_id"],)))
                body = "\n".join(f'{"Owner" if t["speaker"] == "owner" else "JARVIS"}: {t["text"][:200]}' for t in reversed(turns))
            if body:
                candidates.append(_summary_item(recent, body))
        summaries = [r for r in sessions if r["summary_status"] == "done" and r["summary"]
                     and (recent is None or r["session_id"] != recent["session_id"])]
        for score, row in _rank(summaries, question)[:3]:
            item = _summary_item(row, row["summary"])
            item["score"] = score
            candidates.append(item)
    pack = MemoryPack()
    for item in candidates:
        text = ("\n" if pack.text else "") + item["text"]
        if len(pack.text) + len(text) <= config.MEMORY_PACK_CHARS:
            pack.text += text
            pack.items.append(item)
    return pack


def search(query: str, session_id: str | None, limit: int = 5) -> list[dict]:
    """記憶の検索（MCP の道具 memory_recall、spec §3.3.14 I）。記憶のメモ・会話の要約・会話の発言（今の会話と
    過去 90 日の会話）を BM25 で探し、上位 limit 件を返す。読み取りだけ。"""
    if not config.MEMORY_ENABLED or not store.enabled():
        return []
    uri = Path(db.DB_PATH).resolve().as_uri() + "?mode=ro"
    since = (datetime.now() - timedelta(days=SEARCH_TURN_DAYS)).isoformat()
    docs = []
    with closing(sqlite3.connect(uri, uri=True, timeout=0.5)) as conn:
        conn.row_factory = sqlite3.Row
        for row in _notes(conn):
            docs.append({"body": row["text"], "text": _note_item(row)["text"], "at": row["created_at"]})
        for row in conn.execute("SELECT session_id, last_at, summary FROM memory_sessions "
                                "WHERE summary_status='done' AND summary IS NOT NULL"):
            docs.append({"body": row["summary"], "at": row["last_at"],
                         "text": f'- [会話の要約 {row["last_at"][:10]}] {row["summary"]}'})
        for row in conn.execute("SELECT session_id, speaker, text, created_at FROM memory_turns "
                                "WHERE session_id=? OR created_at>=? ORDER BY created_at DESC LIMIT ?",
                                (session_id or "", since, SEARCH_TURN_LIMIT)):
            who = "Owner" if row["speaker"] == "owner" else "JARVIS"
            where = "今の会話" if row["session_id"] == session_id else "会話"
            docs.append({"body": row["text"], "at": row["created_at"],
                         "text": f'- [{where}の発言 {row["created_at"][:16].replace("T", " ")} {who}] {row["text"][:300]}'})
    ranked = _rank(docs, query, field="body", min_score=0)
    ranked.sort(key=lambda pair: (pair[0], pair[1]["at"]), reverse=True)
    return [dict(row, score=score) for score, row in ranked[:limit]]

