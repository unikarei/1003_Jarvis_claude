"""追記専用の原文 JSONL と、原文から復元できる SQLite 索引。"""

import atexit
import json
import os
import queue
import re
import sys
import threading
import time
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


# ---------------------------------------------------------------- 裏の書き込み（T-2203）
# 記録は裏の 1 本のスレッドで順に書く。会話の処理（とくにブラウザの会話のロックの中）でファイル・DB の書き込みを
# 待たない（2026-11-12 のブラウザの会話が固まった対策）。順番は保たれる（同じ会話の発言 → 終わり）。
_QUEUE: "queue.Queue" = queue.Queue()
_WRITER = None
_WRITER_LOCK = threading.Lock()
FLUSH_SECONDS = 5.0


def _writer_loop():
    while True:
        function, args = _QUEUE.get()
        try:
            safe_call(function, *args)
        finally:
            _QUEUE.task_done()


def submit(function, *args):
    """記録の処理を裏のスレッドに渡す（待たない）。記憶が無効なら何もしない。"""
    global _WRITER
    if not enabled():
        return
    with _WRITER_LOCK:
        if _WRITER is None or not _WRITER.is_alive():
            _WRITER = threading.Thread(target=_writer_loop, name="jarvis-memory-writer", daemon=True)
            _WRITER.start()
    _QUEUE.put((function, args))


def discard_pending() -> int:
    """まだ書いていない記録を捨てる（テストの後片付け用。次のテストの DB・フォルダに遅れて書かないように）。"""
    with _QUEUE.mutex:
        n = len(_QUEUE.queue)
        _QUEUE.queue.clear()
        _QUEUE.unfinished_tasks -= n
        if _QUEUE.unfinished_tasks <= 0:
            _QUEUE.all_tasks_done.notify_all()
    return n


def flush(timeout: float = FLUSH_SECONDS) -> bool:
    """渡した記録を書き終えるまで待つ（最大 timeout 秒）。書き終えたら True。"""
    deadline = time.monotonic() + timeout
    while _QUEUE.unfinished_tasks:
        if time.monotonic() >= deadline:
            return False
        time.sleep(0.01)
    return True


atexit.register(flush)   # プロセスの終わりに、残りの記録を書き終える（最大 5 秒）


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
    if not path.parent.is_dir():          # 作るのは最初の 1 回だけ（毎回の権限の付け直しをしない、T-2203）
        for folder in folders:
            folder.mkdir(mode=0o700, parents=True, exist_ok=True)
            folder.chmod(0o700)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
    with os.fdopen(fd, "a", encoding="utf-8") as stream:
        os.fchmod(stream.fileno(), 0o600)
        for record in records:
            stream.write(json.dumps(record, ensure_ascii=False) + "\n")


_INITIALIZED: set[str] = set()
_LAST_SEQ: dict[str, int] = {}   # このプロセスで最後に振った発言の番号（会話ごと）   # 表を作った DB（書くたびに作成のスクリプト全体を流さない、T-2203）


def _connection():
    key = str(Path(db.DB_PATH).resolve())
    if key not in _INITIALIZED:
        db.init_db()
        _INITIALIZED.add(key)
    return db.get_connection()


def _apply(conn, session_id, record):
    kind = record["type"]
    at = record["at"]
    if kind == "session":
        conn.execute("INSERT OR IGNORE INTO memory_sessions(session_id, channel, started_at, last_at) VALUES(?,?,?,?)",
                     (session_id, record["channel"], at, at))
    elif kind == "turn":
        conn.execute("INSERT OR REPLACE INTO memory_turns VALUES(?,?,?,?,?)",
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
        # 1 発言分だけ書く（発言のたびに JSONL を読み直して入れ直すと、会話が長いほど重くなるため、T-2203）。
        # JSONL と索引がずれたら `python -m memory rebuild` で原文から作り直す
        records = []
        if conn.execute("SELECT 1 FROM memory_sessions WHERE session_id=?", (session_id,)).fetchone() is None:
            records.append(dict(type="session", session_id=session_id, channel=channel, at=at))
        seq = max(conn.execute("SELECT COALESCE(MAX(seq),0) FROM memory_turns WHERE session_id=?",
                               (session_id,)).fetchone()[0], _LAST_SEQ.get(session_id, 0)) + 1
        records.append(dict(type="turn", seq=seq, speaker=speaker, text=text, at=at))
        _append(session_id, *records)
        _LAST_SEQ[session_id] = seq       # 索引だけ失敗しても、次の発言が同じ番号にならないように（T-2203）
        for record in records:
            _apply(conn, session_id, record)


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


def session_turns(session_id):
    """会話の発言（正本の索引）を順に返す: [{"speaker", "text"}]。引き継ぎメモの元（spec §3.4.12 G）。"""
    if not enabled():
        return []
    with _LOCK, closing(_connection()) as conn:
        return [dict(row) for row in conn.execute(
            "SELECT speaker, text FROM memory_turns WHERE session_id=? ORDER BY seq", (session_id,))]


# ---------------------------------------------------------------- 記憶のメモ（T-2406、spec §3.3.14 I）
# 会話の中でハーネスが Jarvis の MCP の道具 memory_note で書く。原文は月ごとの JSONL（追記だけ、600）、索引は memory_notes。
# MCP サーバーは別のプロセスで動くため、ここでは裏のスレッドを使わずにその場で書く。
NOTE_KINDS = ("fact", "decision", "preference")
NOTE_MAX_CHARS = 200
_NOTE_ID = re.compile(r"note_\d{8}_\d{6}_[0-9a-f]{4}\Z")


def _notes_root():
    return Path(config.MEMORY_DIR) / "notes"


def _note_path(at: datetime):
    return _notes_root() / f"{at:%Y}" / f"{at:%m}.jsonl"


def _apply_note(conn, record):
    conn.execute("INSERT OR REPLACE INTO memory_notes VALUES(?,?,?,?,?)",
                 (record["note_id"], record["session_id"], record["kind"], record["text"], record["at"]))


def add_note(session_id, kind, text):
    """記憶のメモを 1 件書いて返す。秘密情報は伏せてから書く。"""
    if not enabled():
        raise RuntimeError("会話の記憶が無効です")
    if kind not in NOTE_KINDS:
        raise ValueError("記憶のメモの種類が不正です")
    session_id = session_id or ""
    if session_id and not _ID.fullmatch(session_id):
        raise ValueError("会話 ID が不正です")
    text = mask_secrets(" ".join(str(text).split()))
    if not text or len(text) > NOTE_MAX_CHARS:
        raise ValueError(f"記憶のメモは 1〜{NOTE_MAX_CHARS} 字です")
    now = datetime.now()
    record = dict(type="note", note_id=f"note_{now:%Y%m%d_%H%M%S}_{os.urandom(2).hex()}", session_id=session_id,
                  kind=kind, text=text, at=now.isoformat())
    path = _note_path(now)
    with _LOCK:
        if not path.parent.is_dir():
            for folder in (Path(config.MEMORY_DIR), _notes_root(), path.parent):
                folder.mkdir(mode=0o700, parents=True, exist_ok=True)
                folder.chmod(0o700)
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
        with os.fdopen(fd, "a", encoding="utf-8") as stream:
            os.fchmod(stream.fileno(), 0o600)
            stream.write(json.dumps(record, ensure_ascii=False) + "\n")
        with closing(_connection()) as conn, conn:
            _apply_note(conn, record)
    return record


def list_notes(session_id=None):
    if not enabled():
        return []
    with _LOCK, closing(_connection()) as conn:
        query = "SELECT * FROM memory_notes" + (" WHERE session_id=?" if session_id is not None else "")
        return [dict(row) for row in conn.execute(query + " ORDER BY created_at, note_id",
                                                   (session_id,) if session_id is not None else ())]


def count_notes(session_id):
    return len(list_notes(session_id))


def _rewrite_notes(keep):
    """記憶のメモの原文から keep(record) が False のものを除く（一時ファイルに書いてから置き換える）。消した件数。"""
    removed = 0
    for path in sorted(_notes_root().glob("*/*.jsonl")):
        lines = path.read_text(encoding="utf-8").splitlines()
        kept = [line for line in lines if not line.strip() or keep(json.loads(line))]
        if len(kept) == len(lines):
            continue
        removed += len(lines) - len(kept)
        temp = path.with_name(path.name + ".tmp")
        fd = os.open(temp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            stream.write("".join(line + "\n" for line in kept))
        os.replace(temp, path)
    return removed


def forget_note(note_id):
    if not enabled() or not _NOTE_ID.fullmatch(note_id or ""):
        return False
    with _LOCK:
        removed = _rewrite_notes(lambda r: r.get("note_id") != note_id)
        with closing(_connection()) as conn, conn:
            deleted = conn.execute("DELETE FROM memory_notes WHERE note_id=?", (note_id,)).rowcount
    if removed or deleted:
        print(f"[記憶] メモを削除: {note_id}", file=sys.stderr)
    return bool(removed or deleted)


def stats():
    sessions = list_sessions()
    return {"sessions": len(sessions), "turns": sum(len(get_session(s["session_id"])["turns"]) for s in sessions),
            "summaries": sum(s["summary_status"] == "done" for s in sessions), "notes": len(list_notes())}


def rebuild():
    if not enabled():
        return {"sessions": 0, "turns": 0, "summaries": 0}
    with _LOCK:
        conn = _connection()
        try:
            with conn:
                conn.execute("DELETE FROM memory_turns")
                conn.execute("DELETE FROM memory_sessions")
                conn.execute("DELETE FROM memory_notes")
                for path in sorted((Path(config.MEMORY_DIR) / "sessions").glob("*/*/*.jsonl")):
                    _path(path.stem)  # 不正な ID は受け入れない
                    for line in path.read_text(encoding="utf-8").splitlines():
                        _apply(conn, path.stem, json.loads(line))
                for path in sorted(_notes_root().glob("*/*.jsonl")):
                    for line in path.read_text(encoding="utf-8").splitlines():
                        if line.strip():
                            _apply_note(conn, json.loads(line))
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
                conn.execute("DELETE FROM memory_notes WHERE session_id=?", (session_id,))
            _rewrite_notes(lambda r: r.get("session_id") != session_id)   # その会話で書いた記憶のメモも消す
        finally:
            conn.close()
        # 削除の監査には ID のみを残す（発言・要約は残さない）。
        print(f"[記憶] 削除: {session_id}", file=sys.stderr)
        return True
