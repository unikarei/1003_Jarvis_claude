"""⑥民泊: 大洗Project OS（GitHub: unikarei/oarai-project-os-since2026）のdata/docsを取り込む。

対象リポジトリはprivateのため、`gh auth token`でGitHub APIに認証する。
JARVISはこのリポジトリを読み取るのみで、変更・コミット・importは一切行わない。

- `data/*.json`: 構造化データ（タスク・決定事項・リスク等）。要約せず整形JSONのまま保存する
- `docs/**/*.md`（`docs/templates/`配下の雛形は除外）、`README.md`、`AGENTS.md`: 全文を保存する
- 本文が SECTION_MAX_CHARS 字を超えるファイルは、見出し・項目ごとの節に分けて節ごとの Knowledge にする
  （T-1419、統合ワークスペースの docs/project-os-split-design.md）。ファイルの Source は全文のまま

    python ingest_oarai_project_os.py                                 # 取り込み（GitHub から）
    python ingest_oarai_project_os.py --migrate-sections              # DB のファイル単位の Knowledge を節に置き換える
    python ingest_oarai_project_os.py --rollback-sections <バックアップ>  # Project OS の Knowledge だけを戻す
"""

import argparse
import json
import sqlite3
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

import requests

import db
from config import OARAI_PROJECT_OS_REPO
from db import find_source_by_url
from github_auth import get_github_token

BRANCH = "main"
CATEGORY = "minpaku"
API_BASE = f"https://api.github.com/repos/{OARAI_PROJECT_OS_REPO}"
BLOB_PREFIX = f"https://github.com/{OARAI_PROJECT_OS_REPO}/blob/{BRANCH}/"

INCLUDE_TOP_LEVEL = ("data/", "docs/", "README.md", "AGENTS.md")
EXCLUDE_PREFIXES = ("docs/templates/",)

SECTION_MAX_CHARS = 1000
PREAMBLE = "冒頭"
WHOLE_KEY = "all"


def _is_target_path(path: str) -> bool:
    if path in ("README.md", "AGENTS.md"):
        return True
    if not (path.startswith("data/") or path.startswith("docs/")):
        return False
    if any(path.startswith(p) for p in EXCLUDE_PREFIXES):
        return False
    return path.endswith(".json") or path.endswith(".md")


def list_target_paths(token: str) -> list[str]:
    res = requests.get(
        f"{API_BASE}/git/trees/{BRANCH}",
        params={"recursive": "1"},
        headers={"Authorization": f"Bearer {token}"},
        timeout=15,
    )
    res.raise_for_status()
    tree = res.json()["tree"]
    return [item["path"] for item in tree if item["type"] == "blob" and _is_target_path(item["path"])]


def fetch_raw_content(path: str, token: str) -> str:
    res = requests.get(
        f"{API_BASE}/contents/{path}",
        params={"ref": BRANCH},
        headers={"Authorization": f"Bearer {token}", "Accept": "application/vnd.github.raw"},
        timeout=15,
    )
    res.raise_for_status()
    return res.text


def _to_knowledge_text(path: str, raw_content: str) -> str:
    """JSONは整形して保存（要約しない）。Markdownはそのまま保存する。"""
    if not path.endswith(".json"):
        return raw_content
    try:
        parsed = json.loads(raw_content)
    except json.JSONDecodeError:
        return raw_content
    return f"# {path}\n\n```json\n{json.dumps(parsed, ensure_ascii=False, indent=2)}\n```"


# ---------------------------------------------------------------- 分割

@dataclass
class Section:
    key: str                    # section_key（見出しの階層と同じ見出しの中の順番。行番号は使わない）
    heading: str                # 題名に使う見出し（key == "all" なら ""）
    body: str
    start_line: int | None      # 元のファイルの 1 始まりの行（わからなければ None）
    end_line: int | None


def split_sections(path: str, raw: str) -> list[Section]:
    """ファイルを節に分ける。本文が SECTION_MAX_CHARS 字以下・読めない JSON・最上位がオブジェクトでない JSON は 1 節。"""
    text = _to_knowledge_text(path, raw)
    whole = [Section(WHOLE_KEY, "", text, None, None)]
    if len(text) <= SECTION_MAX_CHARS:
        return whole
    if path.endswith(".json"):
        try:
            parsed = json.loads(raw)
        except json.JSONDecodeError:
            return whole
        if not isinstance(parsed, dict):
            return whole
        return _unique_keys(_split_json(path, parsed)) or whole
    return _unique_keys(_split_markdown(raw))


def _unique_keys(sections: list[Section]) -> list[Section]:
    """同じファイルの中で key を一意にする（見出しに "/" などが入って偶然重なったときだけ番号を付ける）。"""
    seen: set[str] = set()
    for s in sections:
        key, n = s.key, 1
        while key in seen:
            key, n = f"{s.key}~{n}", n + 1
        s.key = key
        seen.add(key)
    return sections


def _is_fence(line: str) -> bool:
    return line.lstrip().startswith("```")


def _blocks(lines: list[tuple[int, str]], marker: str) -> list[tuple[str | None, list[tuple[int, str]]]]:
    """行頭の marker（"## " など）の見出しで分ける（フェンスの中では分けない）。最初の見出しより前は見出し None。"""
    blocks: list[tuple[str | None, list[tuple[int, str]]]] = [(None, [])]
    in_fence = False
    for no, line in lines:
        if _is_fence(line):
            in_fence = not in_fence
        elif not in_fence and line.startswith(marker):
            blocks.append((line[len(marker):].strip(), []))
        blocks[-1][1].append((no, line))
    if not "".join(line for _, line in blocks[0][1]).strip():
        blocks.pop(0)
    return blocks


def _join(lines: list[tuple[int, str]]) -> str:
    return "\n".join(line for _, line in lines)


def _paragraphs(lines: list[tuple[int, str]]) -> list[list[tuple[int, str]]]:
    """空行で区切った段落（フェンスの中の空行では区切らない）。"""
    paras: list[list[tuple[int, str]]] = []
    current: list[tuple[int, str]] = []
    in_fence = False
    for no, line in lines:
        if _is_fence(line):
            in_fence = not in_fence
        if not in_fence and not line.strip():
            if current:
                paras.append(current)
            current = []
            continue
        current.append((no, line))
    if current:
        paras.append(current)
    return paras


def _cut_chars(lines: list[tuple[int, str]]) -> list[tuple[str, int, int]]:
    """1 段落を SECTION_MAX_CHARS 字ずつに切る。(本文, 開始行, 終了行)。"""
    text, first = _join(lines), lines[0][0]
    out = []
    for i in range(0, len(text), SECTION_MAX_CHARS):
        piece = text[i:i + SECTION_MAX_CHARS]
        start = first + text[:i].count("\n")
        out.append((piece, start, start + piece.count("\n")))
    return out


def _pack_paragraphs(lines: list[tuple[int, str]]) -> list[tuple[str, int, int]]:
    """段落を順に SECTION_MAX_CHARS 字以内に詰める（元の行をそのまま使う）。超える 1 段落だけ文字数で切る。"""
    by_no = dict(lines)
    chunks: list[tuple[str, int, int]] = []
    start = end = None

    def text_of(a: int, b: int) -> str:
        return "\n".join(by_no[n] for n in range(a, b + 1) if n in by_no)

    def flush():
        nonlocal start, end
        if start is not None:
            chunks.append((text_of(start, end), start, end))
        start = end = None

    for para in _paragraphs(lines):
        p_start, p_end = para[0][0], para[-1][0]
        if len(_join(para)) > SECTION_MAX_CHARS:
            flush()
            chunks.extend(_cut_chars(para))
            continue
        if start is not None and len(text_of(start, p_end)) > SECTION_MAX_CHARS:
            flush()
        if start is None:
            start = p_start
        end = p_end
    flush()
    return chunks


def _fit(key: str, heading: str, lines: list[tuple[int, str]]) -> list[Section]:
    """1,000 字以内ならそのまま 1 節、超えれば段落で詰める（2 つ目から「（続き n）」）。末尾の空行は行の範囲に入れない。"""
    lines = list(lines)
    while len(lines) > 1 and not lines[-1][1].strip():
        lines.pop()
    text = _join(lines)
    if len(text) <= SECTION_MAX_CHARS:
        return [Section(key, heading, text, lines[0][0], lines[-1][0])]
    return [Section(key if i == 0 else f"{key}/続き{i}", heading if i == 0 else f"{heading}（続き {i}）", body, s, e)
            for i, (body, s, e) in enumerate(_pack_paragraphs(lines))]


def _occurrence(counter: dict[str, int], name: str) -> int:
    n = counter.get(name, 0)
    counter[name] = n + 1
    return n


def _split_markdown(raw: str) -> list[Section]:
    lines = list(enumerate(raw.split("\n"), 1))
    sections: list[Section] = []
    seen2: dict[str, int] = {}
    for h2, block in _blocks(lines, "## "):
        name2 = PREAMBLE if h2 is None else h2
        key2 = f"{name2}/0" if h2 is None else f"## {h2}/{_occurrence(seen2, h2)}"
        if len(_join(block)) <= SECTION_MAX_CHARS:
            sections.extend(_fit(key2, name2, block))
            continue
        seen3: dict[str, int] = {}
        for h3, sub in _blocks(block, "### "):
            if h3 is None:
                sections.extend(_fit(key2, name2, sub))
            else:
                key3 = f"{key2}/### {h3}/{_occurrence(seen3, h3)}"
                sections.extend(_fit(key3, f"{name2} › {h3}", sub))
    return sections


def _label(elem, number: int) -> str:
    if isinstance(elem, dict):
        for field in ("id", "title", "name"):
            if elem.get(field) not in (None, ""):
                return str(elem[field])
    return str(number)


def _pack_lines(text: str) -> list[str]:
    """行を順に SECTION_MAX_CHARS 字以内に詰める。超える 1 行だけ文字数で切る。"""
    chunks: list[str] = []
    current: str | None = None
    for line in text.split("\n"):
        for piece in [line[i:i + SECTION_MAX_CHARS] for i in range(0, len(line), SECTION_MAX_CHARS)] or [""]:
            if current is not None and len(current) + 1 + len(piece) <= SECTION_MAX_CHARS:
                current += "\n" + piece
            else:
                if current:
                    chunks.append(current)
                current = piece
    if current:
        chunks.append(current)
    return chunks


def _dumps(value) -> str:
    return json.dumps(value, ensure_ascii=False, indent=2)


def _indent(text: str, n: int) -> str:
    return "\n".join(" " * n + line for line in text.split("\n"))


def _split_json(path: str, parsed: dict) -> list[Section]:
    """値が配列の項目の要素ごとに整形して順に詰める。配列以外の項目（空の配列を含む）は最初の節の先頭にまとめる。
    配列以外の項目が SECTION_MAX_CHARS 字を超える（または空でない配列がない）ときは、それだけを行で詰めた
    「冒頭」の節にする（1 行が超えるときだけ文字数で切る）。

    本文は _to_knowledge_text の整形（`# <path>`・```json・外側の { }・配列の名前の行 `"tasks": [`・`]`・
    区切りの ,）をそのまま切り分けたもので、すべての節をつなぐと元の本文の文字（空白を除く）がすべてそろう
    （配列以外の項目を先頭に寄せるので、並びだけが変わる）。"""
    items = list(parsed.items())
    arrays: list[tuple[str, list[str]]] = []          # (配列の名前, [名前の行, 要素..., 閉じの行])
    ids: dict[tuple[str, int], str] = {}
    others: list[str] = []
    for name, value in items:
        key = json.dumps(name, ensure_ascii=False)
        if isinstance(value, list) and value:
            elems = [_indent(_dumps(elem), 4) + ("," if j < len(value) - 1 else "") for j, elem in enumerate(value)]
            arrays.append((name, [f"  {key}: ["] + elems + ["  ]"]))
            for j, elem in enumerate(value):
                ids[(name, j + 1)] = _label(elem, j + 1)
        else:
            others.append(_indent(f"{key}: {_dumps(value)}", 2))
    # 区切りの , は並べ替えた後の順（配列以外の項目 → 配列）で付ける。最後の項目だけ付けない
    others = [text + ("," if i < len(others) - 1 or arrays else "") for i, text in enumerate(others)]
    for a, (name, lines) in enumerate(arrays):
        if a < len(arrays) - 1:
            lines[-1] += ","
    header, footer = f"# {path}\n\n```json\n{{", "}\n```"
    head: str | None = "\n".join([header] + others)
    sections: list[Section] = []
    if not arrays or len(head) > SECTION_MAX_CHARS:
        tail = "\n" + footer if not arrays else ""
        for n, body in enumerate(_pack_lines(head + tail)):
            sections.append(Section(f"{PREAMBLE}/{n}", PREAMBLE if n == 0 else f"{PREAMBLE}（続き {n}）",
                                    body, None, None))
        head = None
    for a, (name, lines) in enumerate(arrays):
        # 要素には題名の id を付け、名前の行・閉じの行・配列以外の項目・外枠には付けない
        pieces = [(text, ids[(name, j)] if 0 < j < len(lines) - 1 else None) for j, text in enumerate(lines)]
        if head is not None:
            pieces.insert(0, (head, None))
            head = None
        if a == len(arrays) - 1:
            pieces.append((footer, None))
        chunks: list[list[tuple[str, str | None]]] = []
        for piece in pieces:
            if chunks and len("\n".join(t for t, _ in chunks[-1] + [piece])) <= SECTION_MAX_CHARS:
                chunks[-1].append(piece)
            else:
                chunks.append([piece])
        for n, chunk in enumerate(chunks):
            labels = [lb for _, lb in chunk if lb is not None]
            heading = (name if not labels else f"{name} {labels[0]}" if len(labels) == 1
                       else f"{name} {labels[0]}〜{labels[-1]}")
            sections.append(Section(f"{name}/{n}", heading, "\n".join(t for t, _ in chunk), None, None))
    return sections


# ---------------------------------------------------------------- DB の内容

def _file_name(path: str) -> str:
    return path.rsplit("/", 1)[-1]


def _section_rows(path: str, raw: str) -> list[dict]:
    """節ごとの Knowledge の title・content・url・meta（url はファイルの中で一意）。"""
    blob_url = f"{BLOB_PREFIX}{path}"
    name = _file_name(path)
    rows, urls = [], set()
    for i, s in enumerate(split_sections(path, raw)):
        meta = {"repo_path": path, "section_key": s.key, "section_index": i}
        if s.key == WHOLE_KEY:
            rows.append({"key": s.key, "title": f"{name}（大洗Project OS）", "content": s.body, "url": blob_url,
                         "meta": meta})
            continue
        if s.start_line is None:
            url = f"{blob_url}?plain=1#part-{i + 1}"
        else:
            url = f"{blob_url}?plain=1#L{s.start_line}-L{s.end_line}"
            if url in urls:                           # 1 行の長い段落を文字数で切ると同じ行の範囲になる
                url = f"{blob_url}?plain=1&part={i + 1}#L{s.start_line}-L{s.end_line}"
        urls.add(url)
        rows.append({"key": s.key, "title": f"{name} › {s.heading}（大洗Project OS）",
                     "content": f"# {name} › {s.heading}\n\n{s.body}", "url": url, "meta": meta})
    return rows


def _new_id(conn: sqlite3.Connection, node_type: str) -> str:
    while True:                                       # 同じ秒に多数作るため、既存の ID と重ならないものを選ぶ
        node_id = db.generate_id(node_type, CATEGORY)
        if conn.execute("SELECT 1 FROM nodes WHERE id = ?", (node_id,)).fetchone() is None:
            return node_id


def _insert_node(conn: sqlite3.Connection, node_type: str, title: str, content: str, url: str, meta: dict) -> str:
    node_id = _new_id(conn, node_type)
    conn.execute("INSERT INTO nodes (id, type, category, title, content, url, created_at, meta) "
                 "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                 (node_id, node_type, CATEGORY, title, content, url, datetime.now().isoformat(),
                  json.dumps(meta, ensure_ascii=False)))
    return node_id


def _insert_edge(conn: sqlite3.Connection, src_id: str, dst_id: str) -> None:
    conn.execute("INSERT INTO edges (src_id, dst_id, type, created_at) VALUES (?, ?, 'derived_from', ?)",
                 (src_id, dst_id, datetime.now().isoformat()))


def _delete_knowledge(conn: sqlite3.Connection, knowledge_id: str) -> None:
    conn.execute("DELETE FROM edges WHERE src_id = ? OR dst_id = ?", (knowledge_id, knowledge_id))
    conn.execute("DELETE FROM nodes WHERE id = ?", (knowledge_id,))


def _meta(row) -> dict:
    try:
        meta = json.loads(row["meta"] or "{}")
    except (TypeError, ValueError):
        return {}
    return meta if isinstance(meta, dict) else {}


def _derived_knowledge(conn: sqlite3.Connection, source_id: str) -> list[sqlite3.Row]:
    return conn.execute("SELECT k.* FROM nodes k JOIN edges e ON e.src_id = k.id "
                        "WHERE e.dst_id = ? AND e.type = 'derived_from' AND k.type = 'Knowledge' "
                        "ORDER BY k.created_at, k.id", (source_id,)).fetchall()


def _create_sections(conn: sqlite3.Connection, source_id: str, path: str, raw: str,
                     replaces: str | None = None) -> list[str]:
    created = []
    for row in _section_rows(path, raw):
        meta = dict(row["meta"], **({"replaces": replaces} if replaces else {}))
        kid = _insert_node(conn, "Knowledge", row["title"], row["content"], row["url"], meta)
        _insert_edge(conn, kid, source_id)
        created.append(kid)
    return created


def _sync_sections(conn: sqlite3.Connection, source_id: str, path: str, raw: str) -> tuple[list, list, list]:
    """Source の節を作り直し、既存の節と section_key で対応づける。(作った, 更新した, 消した) の ID。

    section_key のない既存の Knowledge（ファイル単位、移行の前）は消し、新しい節の meta.replaces に入れる。"""
    created, updated, deleted = [], [], []
    existing, legacy = {}, []
    for row in _derived_knowledge(conn, source_id):
        key = _meta(row).get("section_key")
        if key is None:
            legacy.append(row)
        else:
            existing[key] = row
    replaces = legacy[0]["id"] if legacy else None
    for row in legacy:
        _delete_knowledge(conn, row["id"])
        deleted.append(row["id"])
    wanted = _section_rows(path, raw)
    keys = {row["key"] for row in wanted}
    for key, old in existing.items():
        if key not in keys:
            _delete_knowledge(conn, old["id"])
            deleted.append(old["id"])
    for row in wanted:
        old = existing.get(row["key"])
        if old is None:
            meta = dict(row["meta"], **({"replaces": replaces} if replaces else {}))
            kid = _insert_node(conn, "Knowledge", row["title"], row["content"], row["url"], meta)
            _insert_edge(conn, kid, source_id)
            created.append(kid)
            continue
        old_meta = _meta(old)
        meta = dict(row["meta"], **({"replaces": old_meta["replaces"]} if "replaces" in old_meta else {}))
        if (old["title"], old["content"], old["url"], old_meta) != (row["title"], row["content"], row["url"], meta):
            conn.execute("UPDATE nodes SET title = ?, content = ?, url = ?, meta = ? WHERE id = ?",
                         (row["title"], row["content"], row["url"], json.dumps(meta, ensure_ascii=False), old["id"]))
            updated.append(old["id"])
    return created, updated, deleted


# ---------------------------------------------------------------- 取り込み

def ingest_all() -> dict[str, list[str]]:
    """data/*.json・docs/**/*.mdをSource/節ごとのKnowledgeとして取り込む。
    内容が変わったファイルは節を作り直し、変わった節を更新・新しい節を追加・なくなった節を削除する
    （1 ファイルを 1 つのトランザクションで）。内容が同じファイルは何もしない。
    作った・更新した・消したKnowledge IDの一覧を返す。
    """
    token = get_github_token()
    created, updated, deleted = [], [], []

    for path in list_target_paths(token):
        blob_url = f"{BLOB_PREFIX}{path}"
        raw_content = fetch_raw_content(path, token)

        existing_source = find_source_by_url(blob_url)
        if existing_source is not None and existing_source["content"] == raw_content:
            continue
        conn = db.get_connection()
        try:
            with conn:
                if existing_source is None:
                    source_id = _insert_node(conn, "Source", _file_name(path), raw_content, blob_url,
                                             {"repo_path": path})
                    created += _create_sections(conn, source_id, path, raw_content)
                else:
                    conn.execute("UPDATE nodes SET content = ? WHERE id = ?", (raw_content, existing_source["id"]))
                    c, u, d = _sync_sections(conn, existing_source["id"], path, raw_content)
                    created, updated, deleted = created + c, updated + u, deleted + d
        finally:
            conn.close()

    return {"created": created, "updated": updated, "deleted": deleted}


# ---------------------------------------------------------------- 移行と戻し方

def backup_dir() -> Path:
    return Path(db.DB_PATH).parent / "data" / "backups"


def backup_db(directory: Path | None = None) -> Path:
    """DB を sqlite3 の backup API で data/backups/jarvis-<日時>-before-sections.db に写す。
    既存のバックアップは上書きしない（同じ名前があれば番号を付けた新しい名前にする）。"""
    directory = Path(directory or backup_dir())
    directory.mkdir(parents=True, exist_ok=True)
    stamp, n = datetime.now().strftime('%Y%m%d-%H%M%S-%f'), 0
    while True:
        dst_path = directory / f"jarvis-{stamp}{f'-{n}' if n else ''}-before-sections.db"
        try:
            dst_path.open("xb").close()               # 排他的に作る（あれば FileExistsError）
            break
        except FileExistsError:
            n += 1
    src, dst = sqlite3.connect(db.DB_PATH), sqlite3.connect(dst_path)
    try:
        src.backup(dst)
    finally:
        dst.close()
        src.close()
    return dst_path


def _project_os_sources(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    return conn.execute("SELECT * FROM nodes WHERE type = 'Source' AND substr(url, 1, ?) = ? ORDER BY created_at, id",
                        (len(BLOB_PREFIX), BLOB_PREFIX)).fetchall()


def migrate_sections(backup_to: Path | None = None, on_backup=None) -> dict:
    """DB の Project OS のファイル単位の Knowledge を節の Knowledge に置き換える（GitHub は呼ばない）。
    先に DB をバックアップする（on_backup にそのパスを渡してから移行する。移行が失敗してもパスがわかる）。
    全体を 1 つのトランザクションで行い、すでに節になっているファイルは飛ばす。"""
    backup = backup_db(backup_to)
    if on_backup is not None:
        on_backup(backup)
    files, deleted, created = 0, [], []
    conn = db.get_connection()
    try:
        with conn:
            for source in _project_os_sources(conn):
                knowledge = _derived_knowledge(conn, source["id"])
                if any("section_key" in _meta(k) for k in knowledge):
                    continue
                path = source["url"][len(BLOB_PREFIX):]
                for k in knowledge:
                    _delete_knowledge(conn, k["id"])
                    deleted.append(k["id"])
                created += _create_sections(conn, source["id"], path, source["content"] or "",
                                            replaces=knowledge[0]["id"] if knowledge else None)
                files += 1
    finally:
        conn.close()
    return {"backup": str(backup), "files": files, "deleted": deleted, "created": created}


_PROJECT_OS_KNOWLEDGE = (
    "SELECT k.* FROM nodes k WHERE k.type = 'Knowledge' AND (substr(k.url, 1, ?) = ? OR EXISTS ("
    " SELECT 1 FROM edges e JOIN nodes s ON s.id = e.dst_id WHERE e.src_id = k.id AND e.type = 'derived_from'"
    " AND s.type = 'Source' AND substr(s.url, 1, ?) = ?)) ORDER BY k.created_at, k.id")


def _project_os_knowledge(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    return conn.execute(_PROJECT_OS_KNOWLEDGE, (len(BLOB_PREFIX), BLOB_PREFIX) * 2).fetchall()


def rollback_sections(backup_path: Path) -> dict:
    """Project OS の Knowledge をバックアップの時点の状態にそろえる: 今の Project OS の Knowledge（節も
    ファイル単位も）とその辺を消し、バックアップにある Project OS の Knowledge（移行の前から節だったものを含む）と
    その derived_from の辺を同じ ID のまま戻す。ほかの知識には触れない。1 つのトランザクション。"""
    backup_path = Path(backup_path)
    if not backup_path.is_file():
        raise FileNotFoundError(f"バックアップがありません: {backup_path}")
    src = sqlite3.connect(backup_path.resolve().as_uri() + "?mode=ro", uri=True)
    src.row_factory = sqlite3.Row
    try:
        restore = _project_os_knowledge(src)
        edges = {k["id"]: src.execute("SELECT * FROM edges WHERE src_id = ? AND type = 'derived_from' ORDER BY id",
                                      (k["id"],)).fetchall() for k in restore}
        ids = [k["id"] for k in restore]
        marks = ",".join("?" * len(ids))
        # derived_from 以外で Project OS の Knowledge につながる辺（消すときに一緒に消えるもの）も戻す
        other_edges = src.execute(f"SELECT * FROM edges WHERE (src_id IN ({marks}) OR dst_id IN ({marks})) "
                                  f"AND NOT (type = 'derived_from' AND src_id IN ({marks})) ORDER BY id",
                                  ids * 3).fetchall() if ids else []
    finally:
        src.close()
    if not any("section_key" not in _meta(k) for k in restore):
        # 移行を 2 回目以降に実行したときのバックアップなど。節を消すと Project OS の知識がなくなるので、何も変えない
        raise ValueError(f"このバックアップには戻すファイル単位の Project OS の Knowledge がありません: {backup_path}。"
                         "最初に移行したとき（ファイル数が 1 件以上）に表示されたバックアップを指定してください")

    deleted = []
    conn = db.get_connection()
    try:
        with conn:
            for k in _project_os_knowledge(conn):
                _delete_knowledge(conn, k["id"])
                deleted.append(k["id"])
            for k in restore:
                conn.execute("INSERT INTO nodes (id, type, category, title, content, url, created_at, meta) "
                             "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                             (k["id"], k["type"], k["category"], k["title"], k["content"], k["url"],
                              k["created_at"], k["meta"]))
                for e in edges[k["id"]]:
                    conn.execute("INSERT INTO edges (src_id, dst_id, type, created_at) VALUES (?, ?, ?, ?)",
                                 (e["src_id"], e["dst_id"], e["type"], e["created_at"]))
            for e in other_edges:
                ends = conn.execute("SELECT COUNT(*) FROM nodes WHERE id IN (?, ?)",
                                    (e["src_id"], e["dst_id"])).fetchone()[0]
                if ends == len({e["src_id"], e["dst_id"]}):      # 相手のノードが今もあるときだけ
                    conn.execute("INSERT INTO edges (src_id, dst_id, type, created_at) VALUES (?, ?, ?, ?)",
                                 (e["src_id"], e["dst_id"], e["type"], e["created_at"]))
    finally:
        conn.close()
    files = len({e["dst_id"] for es in edges.values() for e in es})
    return {"files": files, "deleted": deleted, "created": [k["id"] for k in restore]}


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description="大洗Project OS の data/docs を取り込む")
    group = parser.add_mutually_exclusive_group()
    group.add_argument("--migrate-sections", action="store_true",
                       help="DB のファイル単位の Knowledge を節の Knowledge に置き換える（GitHub は呼ばない）")
    group.add_argument("--rollback-sections", metavar="BACKUP",
                       help="バックアップの DB から Project OS の Knowledge だけを戻す")
    args = parser.parse_args(argv)
    if args.migrate_sections:
        result = migrate_sections(on_backup=lambda path: print(f"バックアップ: {path}", flush=True))
        print(f"ファイル: {result['files']}件 / 消したKnowledge: {len(result['deleted'])}件 / "
              f"作ったKnowledge: {len(result['created'])}件")
        if result["files"] == 0:
            print("移行するファイルはありませんでした（すでに節になっています）。このバックアップは移行の前の状態では"
                  "ないため --rollback-sections には使えません。戻すときは最初の移行のときに表示されたバックアップを"
                  "指定してください")
    elif args.rollback_sections:
        try:
            result = rollback_sections(Path(args.rollback_sections))
        except (FileNotFoundError, ValueError) as e:
            raise SystemExit(f"戻せません: {e}")
        print(f"ファイル: {result['files']}件 / 消したKnowledge: {len(result['deleted'])}件 / "
              f"戻したKnowledge: {len(result['created'])}件")
    else:
        result = ingest_all()
        print(f"新規Knowledge: {len(result['created'])}件 / 更新: {len(result['updated'])}件 / "
              f"削除: {len(result['deleted'])}件")


if __name__ == "__main__":
    main()
