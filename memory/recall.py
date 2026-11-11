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


def _rank(rows, question):
    # knowledge_engine.search と同じ名詞抽出・BM25 の式（要約には題名の重みなし）。
    terms = db.extract_keywords(question) or [question.strip()]
    terms = [t for t in terms if t]
    if not rows or not terms:
        return []
    docs = [(row, {t: row["summary"].count(t) for t in terms}, len(row["summary"])) for row in rows]
    avg_len = sum(d[2] for d in docs) / len(docs)
    df = {t: sum(d[1][t] > 0 for d in docs) for t in terms}
    hits = []
    for row, tf, length in docs:
        score = sum(math.log(1 + (len(docs) - df[t] + 0.5) / (df[t] + 0.5)) * tf[t] * 2.2 /
                    (tf[t] + 1.2 * (0.25 + 0.75 * length / avg_len)) for t in terms if tf[t])
        score = round(score, 3)
        if score >= config.MEMORY_MIN_SCORE and score > 0:
            hits.append((score, row))
    hits.sort(key=lambda pair: (pair[0], pair[1]["last_at"]), reverse=True)
    return hits


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
