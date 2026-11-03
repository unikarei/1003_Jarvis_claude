"""Project OS の長い文書を節ごとに取り込む（T-1419）のテスト。DB は一時、GitHub は偽の取得、LLM は使わない。"""

import json
import re
import sqlite3
from collections import Counter

import pytest

import db
import ingest_oarai_project_os as ipo
from ingest_oarai_project_os import SECTION_MAX_CHARS, split_sections

BLOB = ipo.BLOB_PREFIX


def _nows(text: str) -> str:
    return re.sub(r"\s", "", text)


def _para(word: str, n: int) -> str:
    return " ".join([word] * n)


def _check_sections(path: str, raw: str, sections) -> None:
    """節の共通の性質: key が一意・1,000 字以下・本文を落とさない（空白を除いて元の文字がそのまま並ぶ）・行が合う。"""
    keys = [s.key for s in sections]
    assert len(keys) == len(set(keys))
    assert all(len(s.body) <= SECTION_MAX_CHARS for s in sections)
    assert _nows("".join(s.body for s in sections)) == _nows(raw)
    lines = raw.split("\n")
    for s in sections:
        assert s.start_line is not None and s.end_line is not None
        assert _nows(s.body) in _nows("\n".join(lines[s.start_line - 1:s.end_line]))


# ---------------------------------------------------------------- 分割（Markdown）

def test_short_file_is_one_section():
    raw = "# 題\n\n## A\n短い本文\n"
    sections = split_sections("docs/a.md", raw)
    assert [(s.key, s.heading, s.body) for s in sections] == [("all", "", raw)]


def test_split_by_h2_with_preamble():
    raw = "# 題\n\n前書き\n\n## A\n" + _para("あい", 300) + "\n\n## B\n" + _para("うえ", 200) + "\n"
    sections = split_sections("docs/a.md", raw)
    assert [(s.key, s.heading) for s in sections] == [("冒頭/0", "冒頭"), ("## A/0", "A"), ("## B/0", "B")]
    assert sections[1].body.startswith("## A\n")
    assert (sections[0].start_line, sections[1].start_line, sections[2].start_line) == (1, 5, 8)
    _check_sections("docs/a.md", raw, sections)


def test_fence_heading_does_not_split():
    raw = "## A\n```\n## 中\n### 中2\n```\n" + _para("あい", 200) + "\n## B\n" + _para("うえ", 300) + "\n"
    sections = split_sections("docs/a.md", raw)
    assert [s.key for s in sections] == ["## A/0", "## B/0"]
    assert "## 中" in sections[0].body
    _check_sections("docs/a.md", raw, sections)


def test_long_h2_split_by_h3_then_paragraphs_then_chars():
    paras = "\n\n".join(_para(f"p{i}", 100) for i in range(4))            # 段落 4 つ（各 約 300 字）
    raw = ("## 大\n導入\n### 小1\n" + _para("x", 300) + "\n### 小2\n" + paras + "\n### 小3\n"
           + "長" * 2500 + "\n## 次\n" + _para("y", 400) + "\n")
    sections = split_sections("docs/a.md", raw)
    got = [(s.key, s.heading) for s in sections]
    assert got[:3] == [("## 大/0", "大"), ("## 大/0/### 小1/0", "大 › 小1"), ("## 大/0/### 小2/0", "大 › 小2")]
    assert ("## 大/0/### 小2/0/続き1", "大 › 小2（続き 1）") in got
    long_parts = [s for s in sections if s.key.startswith("## 大/0/### 小3/0")]
    assert [s.heading for s in long_parts][1] == "大 › 小3（続き 1）"
    assert any(len(s.body) == SECTION_MAX_CHARS for s in long_parts)                # 1 段落が長いときだけ文字数で切る
    assert got[-1] == ("## 次/0", "次")
    _check_sections("docs/a.md", raw, sections)


def test_long_paragraph_on_one_line_has_unique_urls(db_path):
    raw = "## A\n" + "長" * 2500 + "\n## B\n" + _para("y", 400) + "\n"
    rows = ipo._section_rows("docs/a.md", raw)
    urls = [r["url"] for r in rows]
    assert len(urls) == len(set(urls))
    assert all("#part-" not in u for u in urls)                                    # 行がわかる節は行へのリンク
    assert [u.rsplit("#", 1)[1] for u in urls] == ["L1-L2", "L2-L2", "L2-L2", "L3-L4"]
    assert urls[1] == f"{BLOB}docs/a.md?plain=1#L2-L2"
    assert urls[2] == f"{BLOB}docs/a.md?plain=1&part=3#L2-L2"


def test_keys_stable_when_section_added_before():
    body = {h: _para(h.lower(), 300) for h in ("A", "B", "C")}
    raw = "".join(f"## {h}\n{b}\n" for h, b in body.items())
    raw2 = "## 新\n" + _para("n", 300) + "\n" + raw
    before = {s.key: s.body for s in split_sections("docs/a.md", raw)}
    after = {s.key: s.body for s in split_sections("docs/a.md", raw2)}
    assert all(after[k] == v for k, v in before.items())
    assert set(after) - set(before) == {"## 新/0"}


def test_same_heading_twice_gets_order():
    raw = "## A\n" + _para("a", 300) + "\n## A\n" + _para("b", 300) + "\n## C\n" + _para("c", 300)
    assert [s.key for s in split_sections("docs/a.md", raw)] == ["## A/0", "## A/1", "## C/0"]


# ---------------------------------------------------------------- 分割（JSON）

def _tasks(n: int, size: int = 100, key: str = "id") -> list[dict]:
    return [{key: f"T-{i:03}", "body": "あ" * size} for i in range(1, n + 1)]


def _elem(t: dict) -> str:
    """配列の要素の整形（_to_knowledge_text の中と同じく 4 字下げ）。"""
    return "\n".join("    " + line for line in json.dumps(t, ensure_ascii=False, indent=2).split("\n"))


def _check_json(path: str, raw: str, sections) -> None:
    """JSON の節: key が一意・本文を落とさない（つなぐと _to_knowledge_text の空白以外の文字がすべてそろう）。"""
    assert len({s.key for s in sections}) == len(sections)
    assert Counter(_nows("".join(s.body for s in sections))) == Counter(_nows(ipo._to_knowledge_text(path, raw)))


def test_json_packs_array_items_with_ids():
    raw = json.dumps({"schema_version": 2, "tasks": _tasks(12), "risks": _tasks(2, key="title")}, ensure_ascii=False)
    sections = split_sections("data/tasks.json", raw)
    assert all(len(s.body) <= SECTION_MAX_CHARS for s in sections)
    # 外枠と配列以外の項目は最初の節の先頭、続いて配列の名前の行
    assert sections[0].body.startswith('# data/tasks.json\n\n```json\n{\n  "schema_version": 2,\n  "tasks": [\n')
    assert sections[0].heading.startswith("tasks T-001〜")
    assert [s.key for s in sections if s.key.startswith("tasks/")] == [f"tasks/{i}" for i in range(len(sections) - 1)]
    assert sections[-1].key == "risks/0" and sections[-1].heading == "risks T-001〜T-002"
    assert sections[-1].body.startswith('  "risks": [\n') and sections[-1].body.endswith("  ]\n}\n```")
    for t in _tasks(12):
        assert any(_elem(t) in s.body for s in sections)
    assert all(s.start_line is None for s in sections)
    _check_json("data/tasks.json", raw, sections)


def test_json_keeps_array_name_and_frame_in_order():
    """配列以外の項目が最初にあるとき、節をつなぐと _to_knowledge_text の本文そのもの（空白を除く）になる。"""
    raw = json.dumps({"schema_version": 2, "tasks": _tasks(12), "risks": _tasks(9, key="title")}, ensure_ascii=False)
    sections = split_sections("data/tasks.json", raw)
    assert _nows("".join(s.body for s in sections)) == _nows(ipo._to_knowledge_text("data/tasks.json", raw))
    assert sum('"tasks": [' in s.body for s in sections) == 1 and sum('"risks": [' in s.body for s in sections) == 1


def test_json_other_items_after_arrays_are_kept():
    parsed = {"tasks": _tasks(12), "updated_at": "2026-11-03", "notes": {"a": "あ" * 50}}
    raw = json.dumps(parsed, ensure_ascii=False)
    sections = split_sections("data/tasks.json", raw)
    assert '"updated_at": "2026-11-03"' in sections[0].body and '"notes": {' in sections[0].body
    assert all(len(s.body) <= SECTION_MAX_CHARS for s in sections)
    _check_json("data/tasks.json", raw, sections)


def _joined_json(sections):
    text = "\n".join(s.body for s in sections)
    return json.loads(text.split("```json\n", 1)[1].rsplit("\n```", 1)[0])


@pytest.mark.parametrize("parsed", [
    {"tasks": _tasks(12), "updated_at": "2026-11-03"},                                  # 配列以外が後ろ
    {"schema_version": 2, "tasks": _tasks(12), "risks": _tasks(9, key="title"), "note": "x"},
    {"a": 1, "b": "2", "tasks": _tasks(3)},                                             # 分割しない長さでも
])
def test_json_sections_join_to_same_value(parsed):
    """節をつなぐと、元と同じ値の JSON として読める（区切りの , を並べ替えた後の順で付ける）。"""
    raw = json.dumps(parsed, ensure_ascii=False)
    sections = split_sections("data/tasks.json", raw)
    if len(sections) > 1:
        assert _joined_json(sections) == parsed


def test_json_single_long_item_alone_and_fallback_labels():
    items = [{"name": "小"}, {"name": "大", "body": "x" * 1500}, {"v": 1}]
    sections = split_sections("data/x.json", json.dumps({"items": items}))
    assert [s.heading for s in sections] == ["items 小", "items 大", "items 3"]
    assert len(sections[1].body) > SECTION_MAX_CHARS
    _check_json("data/x.json", json.dumps({"items": items}), sections)


@pytest.mark.parametrize("raw", ["{" + "x" * 1500, json.dumps([{"id": i, "b": "x" * 100} for i in range(20)])])
def test_json_unreadable_or_not_object_is_one_section(raw):
    sections = split_sections("data/x.json", raw)
    assert [s.key for s in sections] == ["all"]
    assert sections[0].body == ipo._to_knowledge_text("data/x.json", raw)


def test_json_without_arrays_is_split_by_lines():
    parsed = {f"k{i}": "あ" * 150 for i in range(20)} | {"long": "x" * 2500, "empty": []}
    raw = json.dumps(parsed, ensure_ascii=False)
    sections = split_sections("data/x.json", raw)
    assert len(sections) > 1 and all(len(s.body) <= SECTION_MAX_CHARS for s in sections)
    assert [s.heading for s in sections][:2] == ["冒頭", "冒頭（続き 1）"]
    assert len({s.key for s in sections}) == len(sections)
    assert _nows("".join(s.body for s in sections)) == _nows(ipo._to_knowledge_text("data/x.json", raw))


def test_json_long_head_is_split_before_arrays():
    parsed = {"meta": {f"k{i}": "あ" * 150 for i in range(10)}, "tasks": _tasks(12)}
    sections = split_sections("data/tasks.json", json.dumps(parsed, ensure_ascii=False))
    assert all(len(s.body) <= SECTION_MAX_CHARS for s in sections)
    head = [s for s in sections if s.key.startswith("冒頭/")]
    assert len(head) > 1 and sections[:len(head)] == head
    assert sections[len(head)].heading.startswith("tasks T-001〜")
    assert sections[len(head)].body.startswith('  "tasks": [\n')
    for t in _tasks(12):
        assert any(_elem(t) in s.body for s in sections)
    _check_json("data/tasks.json", json.dumps(parsed, ensure_ascii=False), sections)


def test_json_section_urls_use_part(db_path):
    raw = json.dumps({"tasks": _tasks(12)}, ensure_ascii=False)
    rows = ipo._section_rows("data/tasks.json", raw)
    assert [r["url"] for r in rows] == [f"{BLOB}data/tasks.json?plain=1#part-{i}" for i in range(1, len(rows) + 1)]


# ---------------------------------------------------------------- 取り込み

@pytest.fixture
def fake_repo(db_path, monkeypatch):
    files: dict[str, str] = {}
    monkeypatch.setattr(ipo, "get_github_token", lambda: "token")
    monkeypatch.setattr(ipo, "list_target_paths", lambda token: list(files))
    monkeypatch.setattr(ipo, "fetch_raw_content", lambda path, token: files[path])
    return files


def _knowledge():
    return {r["id"]: r for r in db._fetchall("SELECT * FROM nodes WHERE type = 'Knowledge'")}


def _edges():
    return sorted((r["src_id"], r["dst_id"], r["type"]) for r in db._fetchall("SELECT * FROM edges"))


def _by_key():
    return {json.loads(r["meta"])["section_key"]: r for r in _knowledge().values()}


def _doc(sections: dict[str, str]) -> str:
    return "# 文書\n\n" + "".join(f"## {h}\n{b}\n\n" for h, b in sections.items())


DOC = {"A": _para("a", 300), "B": _para("b", 300), "C": _para("c", 300)}


def test_ingest_new_file_creates_source_sections_and_edges(fake_repo):
    fake_repo["docs/spec.md"] = _doc(DOC)
    fake_repo["data/small.json"] = '{"a": 1}'
    result = ipo.ingest_all()
    assert result["updated"] == [] and result["deleted"] == []
    sources = db._fetchall("SELECT * FROM nodes WHERE type = 'Source'")
    assert len(sources) == 2
    by_key = _by_key()
    assert set(by_key) == {"冒頭/0", "## A/0", "## B/0", "## C/0", "all"}
    assert sorted(result["created"]) == sorted(r["id"] for r in by_key.values())
    a = by_key["## A/0"]
    assert a["title"] == "spec.md › A（大洗Project OS）"
    assert a["content"].startswith("# spec.md › A\n\n## A\n")
    assert a["url"] == f"{BLOB}docs/spec.md?plain=1#L3-L4"
    assert json.loads(a["meta"]) == {"repo_path": "docs/spec.md", "section_key": "## A/0", "section_index": 1}
    small = by_key["all"]
    assert (small["title"], small["url"]) == ("small.json（大洗Project OS）", f"{BLOB}data/small.json")
    assert small["content"] == ipo._to_knowledge_text("data/small.json", '{"a": 1}')
    src_of = {s["url"]: s["id"] for s in sources}
    assert all(db.get_source_for_knowledge(k["id"])["id"] == src_of[f"{BLOB}{json.loads(k['meta'])['repo_path']}"]
               for k in by_key.values())
    assert len(_edges()) == 5


def test_reingest_updates_adds_deletes_sections(fake_repo):
    fake_repo["docs/spec.md"] = _doc(DOC)
    ipo.ingest_all()
    before = _by_key()
    edges_before = _edges()
    fake_repo["docs/spec.md"] = _doc({"A": DOC["A"], "B": _para("bb", 300), "D": _para("d", 300)})
    result = ipo.ingest_all()
    after = _by_key()
    assert set(after) == {"冒頭/0", "## A/0", "## B/0", "## D/0"}
    assert after["## A/0"]["id"] == before["## A/0"]["id"]
    assert dict(after["## A/0"]) == dict(before["## A/0"])                          # 変わらない節はそのまま
    assert after["## B/0"]["id"] == before["## B/0"]["id"] and "bb bb" in after["## B/0"]["content"]
    assert result["updated"] == [before["## B/0"]["id"]]
    assert result["created"] == [after["## D/0"]["id"]]
    assert result["deleted"] == [before["## C/0"]["id"]]
    assert db.get_node(before["## C/0"]["id"]) is None
    assert all(before["## C/0"]["id"] not in e[:2] for e in _edges())
    assert len(_edges()) == len(edges_before)
    source = db._fetchall("SELECT * FROM nodes WHERE type = 'Source'")[0]
    assert source["content"] == fake_repo["docs/spec.md"]


def test_reingest_same_content_does_nothing(fake_repo):
    fake_repo["docs/spec.md"] = _doc(DOC)
    ipo.ingest_all()
    snapshot = ({k: dict(v) for k, v in _knowledge().items()}, _edges())
    assert ipo.ingest_all() == {"created": [], "updated": [], "deleted": []}
    assert ({k: dict(v) for k, v in _knowledge().items()}, _edges()) == snapshot


def test_reingest_file_is_one_transaction(fake_repo, monkeypatch):
    fake_repo["docs/spec.md"] = _doc(DOC)
    ipo.ingest_all()
    snapshot = ({k: dict(v) for k, v in _knowledge().items()}, _edges())
    source_before = db._fetchall("SELECT content FROM nodes WHERE type = 'Source'")[0]["content"]
    fake_repo["docs/spec.md"] = _doc({"A": _para("z", 300)})
    real = ipo._sync_sections

    def boom(*a, **k):
        real(*a, **k)                                     # 節を書き換えてから失敗する
        raise RuntimeError("偽の失敗")
    monkeypatch.setattr(ipo, "_sync_sections", boom)
    with pytest.raises(RuntimeError):
        ipo.ingest_all()
    assert ({k: dict(v) for k, v in _knowledge().items()}, _edges()) == snapshot
    assert db._fetchall("SELECT content FROM nodes WHERE type = 'Source'")[0]["content"] == source_before


# ---------------------------------------------------------------- 移行と戻し方

def _old_ingest(path: str, raw: str) -> str:
    """T-1419 より前の取り込み（ファイル単位の Knowledge 1 件）を再現する。"""
    url = f"{BLOB}{path}"
    sid = db.insert_node("Source", category="minpaku", title=path.rsplit("/", 1)[-1], content=raw, url=url,
                         meta={"repo_path": path})
    kid = db.insert_node("Knowledge", category="minpaku", title=f"{path.rsplit('/', 1)[-1]}（大洗Project OS）",
                         content=ipo._to_knowledge_text(path, raw), url=url)
    db.insert_edge(kid, sid, "derived_from")
    return kid


@pytest.fixture
def legacy(db_path):
    files = {"docs/spec.md": _doc(DOC), "data/tasks.json": json.dumps({"tasks": _tasks(12)}, ensure_ascii=False),
             "README.md": "# 短い\n"}
    kids = {path: _old_ingest(path, raw) for path, raw in files.items()}
    other_s = db.insert_node("Source", category="news", title="記事", content="本文", url="https://example.com/a")
    other_k = db.insert_node("Knowledge", category="news", title="記事の要約", content="要約", url="https://example.com/a")
    db.insert_edge(other_k, other_s, "derived_from")
    return {"files": files, "kids": kids, "other": other_k}


def _project_os_state():
    rows = db._fetchall("SELECT * FROM nodes WHERE type = 'Knowledge' AND category = 'minpaku' ORDER BY id")
    ids = {r["id"] for r in rows}
    edges = sorted((r["src_id"], r["dst_id"], r["type"]) for r in db._fetchall("SELECT * FROM edges")
                   if r["src_id"] in ids)
    return [dict(r) for r in rows], edges


def test_migrate_replaces_file_knowledge_with_sections(legacy, tmp_path):
    result = ipo.migrate_sections(tmp_path / "bk")
    assert result["files"] == 3 and sorted(result["deleted"]) == sorted(legacy["kids"].values())
    assert sqlite3.connect(result["backup"]).execute("SELECT COUNT(*) FROM nodes").fetchone()[0] > 0
    assert re.search(r"jarvis-\d{8}-\d{6}-\d{6}(-\d+)?-before-sections\.db$", result["backup"])
    for kid in legacy["kids"].values():
        assert db.get_node(kid) is None
    knowledge = [r for r in _knowledge().values() if r["category"] == "minpaku"]
    assert sorted(r["id"] for r in knowledge) == sorted(result["created"])
    for r in knowledge:
        meta = json.loads(r["meta"])
        assert meta["replaces"] == legacy["kids"][meta["repo_path"]]
        assert db.get_source_for_knowledge(r["id"])["url"] == f"{BLOB}{meta['repo_path']}"
    assert sum(json.loads(r["meta"])["repo_path"] == "docs/spec.md" for r in knowledge) == 4
    assert db.get_node(legacy["other"]) is not None


def test_backup_does_not_overwrite_in_same_instant(legacy, tmp_path, monkeypatch):
    class Frozen(ipo.datetime):
        @classmethod
        def now(cls, tz=None):
            return ipo.datetime(2026, 11, 3, 12, 0, 0)
    monkeypatch.setattr(ipo, "datetime", Frozen)
    first = ipo.backup_db(tmp_path / "bk")
    db.insert_node("Knowledge", category="news", title="後から", content="x")
    second = ipo.backup_db(tmp_path / "bk")
    assert first != second
    count = "SELECT COUNT(*) FROM nodes"
    assert (sqlite3.connect(first).execute(count).fetchone()[0] + 1
            == sqlite3.connect(second).execute(count).fetchone()[0])


def test_migrate_twice_keeps_first_backup_for_rollback(legacy, tmp_path, monkeypatch):
    class Frozen(ipo.datetime):
        @classmethod
        def now(cls, tz=None):
            return ipo.datetime(2026, 11, 3, 12, 0, 0)
    before = _project_os_state()
    monkeypatch.setattr(ipo, "datetime", Frozen)            # 同じ時刻に 2 回移行しても最初のバックアップが残る
    first = ipo.migrate_sections(tmp_path / "bk")["backup"]
    second = ipo.migrate_sections(tmp_path / "bk")["backup"]
    assert first != second
    ipo.rollback_sections(first)
    assert _project_os_state() == before


def test_rollback_refuses_backup_without_file_knowledge(legacy, tmp_path, capsys):
    """2 回目の移行のバックアップ（節だけ）で戻そうとすると、何も変えずに断る。"""
    ipo.migrate_sections(tmp_path / "bk")
    state = _project_os_state()
    second = ipo.migrate_sections(tmp_path / "bk")["backup"]
    with pytest.raises(ValueError, match="最初に移行したとき"):
        ipo.rollback_sections(second)
    assert _project_os_state() == state
    with pytest.raises(SystemExit):
        ipo.main(["--rollback-sections", second])
    assert _project_os_state() == state


def test_cli_second_migration_warns_backup_not_for_rollback(legacy, tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(ipo, "backup_dir", lambda: tmp_path / "bk")
    ipo.main(["--migrate-sections"])
    assert "--rollback-sections には使えません" not in capsys.readouterr().out
    ipo.main(["--migrate-sections"])
    assert "--rollback-sections には使えません" in capsys.readouterr().out


def test_migrate_twice_is_same(legacy, tmp_path):
    ipo.migrate_sections(tmp_path / "bk")
    state = _project_os_state()
    second = ipo.migrate_sections(tmp_path / "bk")
    assert (second["files"], second["deleted"], second["created"]) == (0, [], [])
    assert _project_os_state() == state


def test_migrate_failure_changes_nothing(legacy, tmp_path, monkeypatch):
    state = _project_os_state()
    calls = []
    real = ipo.split_sections

    def flaky(path, raw):
        calls.append(path)
        if len(calls) == 2:
            raise RuntimeError("偽の失敗")
        return real(path, raw)
    monkeypatch.setattr(ipo, "split_sections", flaky)
    shown = []
    with pytest.raises(RuntimeError):
        ipo.migrate_sections(tmp_path / "bk", on_backup=shown.append)
    assert _project_os_state() == state
    assert len(shown) == 1 and shown[0].exists()      # 移行が失敗しても、バックアップのパスは先に渡っている
    assert list((tmp_path / "bk").glob("jarvis-*-before-sections.db"))


def test_migrate_default_backup_dir_is_next_to_db(legacy, db_path):
    result = ipo.migrate_sections()
    assert str(db_path.parent / "data" / "backups") in result["backup"]


def test_reingest_before_migration_replaces_file_knowledge(legacy, monkeypatch):
    """移行の前のファイル（section_key のない Knowledge）が変わったら、消して節にし、replaces を持たせる。"""
    files = dict(legacy["files"], **{"docs/spec.md": _doc(dict(DOC, D=_para("d", 300)))})
    monkeypatch.setattr(ipo, "get_github_token", lambda: "token")
    monkeypatch.setattr(ipo, "list_target_paths", lambda token: list(files))
    monkeypatch.setattr(ipo, "fetch_raw_content", lambda path, token: files[path])
    result = ipo.ingest_all()
    old = legacy["kids"]["docs/spec.md"]
    assert result["deleted"] == [old] and result["updated"] == [] and len(result["created"]) == 5
    assert all(json.loads(db.get_node(k)["meta"])["replaces"] == old for k in result["created"])
    assert db.get_node(legacy["kids"]["README.md"]) is not None             # 変わっていないファイルはそのまま


def test_rollback_restores_project_os_and_keeps_other_knowledge(legacy, tmp_path):
    before = _project_os_state()
    backup = ipo.migrate_sections(tmp_path / "bk")["backup"]
    added = db.insert_node("Knowledge", category="news", title="移行の後の知識", content="新しい")
    result = ipo.rollback_sections(backup)
    assert sorted(result["created"]) == sorted(legacy["kids"].values()) and result["files"] == 3
    assert _project_os_state() == before
    assert db.get_node(added) is not None and db.get_node(legacy["other"]) is not None


def test_cli_prints_counts(legacy, tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(ipo, "backup_dir", lambda: tmp_path / "bk")
    ipo.main(["--migrate-sections"])
    out = capsys.readouterr().out
    assert "バックアップ:" in out and "ファイル: 3件" in out and "消したKnowledge: 3件" in out
    backup = next((tmp_path / "bk").glob("*.db"))
    ipo.main(["--rollback-sections", str(backup)])
    assert "戻したKnowledge: 3件" in capsys.readouterr().out
