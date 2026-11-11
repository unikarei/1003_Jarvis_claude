"""追記専用の原文 JSONL と、原文から復元できる SQLite 索引。"""

import json
import os
import re
import sys
import threading
from datetime import datetime, timedelta
from pathlib import Path
from contextlib import closing

import config
import db
from ckc.secrets import mask_secrets

_LOCK = threading.RLock()
_ID = re.compile(r"sess_(\d{4})(\d{2})\d{2}_\d{6}_[0-9a-f]{4}\Z")


def enabled():
    return os.environ.get("JARVIS_MEMORY", "1") != "0"


def warn(action, error):
    # 例外の本文には発言や秘密情報が含まれる可能性がある。
    print(f"[記憶] {action}に失敗しました（{type(error).__name__}）", file=sys.stderr)


def safe_call(function, *args, **kwargs):
    try:
        return function(*args, **kwargs)
    except Exception as error:
        warn("記録", error)
        return None


def _path(session_id):
    match = _ID.fullmatch(session_id)
    if not match:
        raise ValueError("会話 ID が不正です")
    return Path(config.MEMORY_DIR) / "sessions" / match[1] / match[2] / f"{session_id}.jsonl"


def _append(session_id, *records):
    path = _path(session_id)
    # memory 以下のフォルダだけを作る。既存の親フォルダの権限は変えない。
    folders = [Path(config.MEMORY_DIR)]
    folders += [Path(config.MEMORY_DIR) / part for part in ("sessions", f"sessions/{path.parent.parent.name}",
                                                          f"sessions/{path.parent.parent.name}/{path.parent.name}")]
    for folder in folders:
        folder.mkdir(mode=0o700, parents=True, exist_ok=True)
        folder.chmod(0o700)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
    with os.fdopen(fd, "a", encoding="utf-8") as stream:
        os.fchmod(stream.fileno(), 0o600)
        for record in records:
            stream.write(json.dumps(record, ensure_ascii=False) + "\n")


def _connection():
    db.init_db()
    return db.get_connection()


def _apply(conn, session_id, record):
    kind = record["type"]
    at = record["at"]
    if kind == "session":
        conn.execute("INSERT OR IGNORE INTO memory_sessions(session_id, channel, started_at, last_at) VALUES(?,?,?,?)",
                     (session_id, record["channel"], at, at))
    elif kind == "turn":
        conn.execute("INSERT INTO memory_turns VALUES(?,?,?,?,?)",
                     (session_id, record["seq"], record["speaker"], record["text"], at))
        conn.execute("UPDATE memory_sessions SET last_at=?, owner_turns=owner_turns+? WHERE session_id=?",
                     (at, int(record["speaker"] == "owner"), session_id))
    elif kind == "end":
        conn.execute("UPDATE memory_sessions SET ended_at=? WHERE session_id=?", (at, session_id))
    elif kind == "summary":
        conn.execute("UPDATE memory_sessions SET summary=?, summary_status='done', summarized_at=? WHERE session_id=?",
                     (record["text"], at, session_id))
    elif kind == "status":
        conn.execute("UPDATE memory_sessions SET summary_status=?, summary_tries=? WHERE session_id=?",
                     (record["status"], record["tries"], session_id))


def _write(conn, session_id, *records):
    _append(session_id, *records)
    for record in records:
        _apply(conn, session_id, record)


def log_turn(session_id, channel, speaker, text):
    if not enabled() or not text.strip():
        return
    speaker = getattr(speaker, "value", speaker)
    if speaker not in ("owner", "ai") or channel not in ("text", "voice", "web"):
        raise ValueError("話者または経路が不正です")
    text = mask_secrets(text)
    at = datetime.now().isoformat()
    with _LOCK, closing(_connection()) as conn, conn:
        # 前回の JSONL 追記後に DB 更新だけが失敗していても、原文を先に復元する。
        path = _path(session_id)
        if path.exists():
            conn.execute("DELETE FROM memory_turns WHERE session_id=?", (session_id,))
            conn.execute("DELETE FROM memory_sessions WHERE session_id=?", (session_id,))
            for line in path.read_text(encoding="utf-8").splitlines():
                _apply(conn, session_id, json.loads(line))
        records = []
        if conn.execute("SELECT 1 FROM memory_sessions WHERE session_id=?", (session_id,)).fetchone() is None:
            records.append(dict(type="session", session_id=session_id, channel=channel, at=at))
        seq = conn.execute("SELECT COALESCE(MAX(seq),0)+1 FROM memory_turns WHERE session_id=?",
                           (session_id,)).fetchone()[0]
        records.append(dict(type="turn", seq=seq, speaker=speaker, text=text, at=at))
        _write(conn, session_id, *records)


def safe_log_turn(*args):
    return safe_call(log_turn, *args)


def _change(session_id, record):
    if not enabled():
        return
    with _LOCK:
        conn = _connection()
        try:
            with conn:
                if conn.execute("SELECT 1 FROM memory_sessions WHERE session_id=?", (session_id,)).fetchone():
                    _write(conn, session_id, record)
        finally:
            conn.close()


def end_session(session_id):
    _change(session_id, dict(type="end", at=datetime.now().isoformat()))


def save_summary(session_id, text):
    _change(session_id, dict(type="summary", text=mask_secrets(text), at=datetime.now().isoformat()))


def _status(session_id, status, tries):
    _change(session_id, dict(type="status", status=status, tries=tries, at=datetime.now().isoformat()))


def mark_summary_failed(session_id):
    with _LOCK:
        session = get_session(session_id)
        if session:
            _status(session_id, "failed", session["summary_tries"] + 1)


def list_sessions():
    if not enabled():
        return []
    with _LOCK:
        conn = _connection()
        try:
            return [dict(row) for row in conn.execute("SELECT * FROM memory_sessions ORDER BY started_at, session_id")]
        finally:
            conn.close()


def get_session(session_id):
    if not enabled():
        return None
    with _LOCK:
        conn = _connection()
        try:
            row = conn.execute("SELECT * FROM memory_sessions WHERE session_id=?", (session_id,)).fetchone()
            if row is None:
                return None
            result = dict(row)
            result["turns"] = [dict(turn) for turn in conn.execute(
                "SELECT * FROM memory_turns WHERE session_id=? ORDER BY seq", (session_id,))]
            return result
        finally:
            conn.close()


def sessions_to_summarize(now=None, exclude_session_id=None):
    if not enabled():
        return []
    now = now or datetime.now()
    result = []
    with _LOCK:
        for row in list_sessions():
            if row["session_id"] == exclude_session_id:
                continue
            if row["owner_turns"] < 2:
                if row["ended_at"] and row["summary_status"] == "none":
                    _status(row["session_id"], "skipped", row["summary_tries"])
                continue
            if (row["summary_status"] in ("none", "failed") and row["summary_tries"] < 3
                    and (row["ended_at"] or now - datetime.fromisoformat(row["last_at"]) >= timedelta(minutes=30))):
                result.append(row)
    return result


def stats():
    sessions = list_sessions()
    return {"sessions": len(sessions), "turns": sum(len(get_session(s["session_id"])["turns"]) for s in sessions),
            "summaries": sum(s["summary_status"] == "done" for s in sessions)}


def rebuild():
    if not enabled():
        return {"sessions": 0, "turns": 0, "summaries": 0}
    with _LOCK:
        conn = _connection()
        try:
            with conn:
                conn.execute("DELETE FROM memory_turns")
                conn.execute("DELETE FROM memory_sessions")
                for path in sorted((Path(config.MEMORY_DIR) / "sessions").glob("*/*/*.jsonl")):
                    _path(path.stem)  # 不正な ID は受け入れない
                    for line in path.read_text(encoding="utf-8").splitlines():
                        _apply(conn, path.stem, json.loads(line))
        finally:
            conn.close()
        return stats()


def forget(session_id):
    if not enabled():
        return False
    with _LOCK:
        path = _path(session_id)
        conn = _connection()
        try:
            exists = path.exists() or conn.execute("SELECT 1 FROM memory_sessions WHERE session_id=?", (session_id,)).fetchone()
            if not exists:
                return False
            with conn:
                path.unlink(missing_ok=True)
                conn.execute("DELETE FROM memory_turns WHERE session_id=?", (session_id,))
                conn.execute("DELETE FROM memory_sessions WHERE session_id=?", (session_id,))
        finally:
            conn.close()
        # 削除の監査には ID のみを残す（発言・要約は残さない）。
        print(f"[記憶] 削除: {session_id}", file=sys.stderr)
        return True
