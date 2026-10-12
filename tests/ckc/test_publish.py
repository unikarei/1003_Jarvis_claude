"""保存（T-113）のテスト。仮の Vault と一時 DB だけを使う。"""

import json
import subprocess

import pytest

import db
from ckc import publish, store
from ckc.models import (Candidate, CandidateStatus, Confidence, Evidence, Kind, NoteStatus, Speaker, new_id)
from ckc.capture import capture_text
from ckc.render import parse_note


@pytest.fixture
def env(db_path, ckc_data_dir, make_vault):
    return {"jk": make_vault("jarvis-knowledge"), "oarai": make_vault("oarai-obsidian")}


def _candidate(conv_id, title="汎用 Vault の採用", resource="vault-jarvis-knowledge", projects=("jarvis",)):
    c = Candidate(new_id("cand"), conv_id, Kind.DECISION, title, "大洗以外は jarvis-knowledge に保存する",
                  list(projects), None, Confidence.CONFIRMED,
                  [Evidence("jarvis-knowledge に保存することにする", Speaker.OWNER)], resource_id=resource)
    store.add_candidates([c])
    return c


@pytest.fixture
def conv_id(env):
    return capture_text("Owner: jarvis-knowledge に保存することにする\nAI: 了解", title="方針").conversation_id


def _approve(c, supersedes=None):
    store.set_status(c.candidate_id, CandidateStatus.APPROVED, supersedes=supersedes)


def test_publish_writes_note_and_graph(env, conv_id):
    c = _candidate(conv_id)
    _approve(c)
    result = publish.publish(c.candidate_id)
    assert result.resource_id == "vault-jarvis-knowledge" and result.warning is None
    assert result.note_relpath.startswith("_jarvis/knowledge/Decision/") and result.note_relpath.endswith("_汎用 Vault の採用.md")
    note = parse_note((env["jk"] / result.note_relpath).read_text(encoding="utf-8"))
    assert (note.ckc_id, note.status, note.source.conversation_id) == (result.ckc_id, NoteStatus.ACTIVE, conv_id)

    node = db.get_node(result.ckc_id)
    meta = json.loads(node["meta"])
    assert (node["type"], meta["note_relpath"], meta["resource_id"], meta["status"]) == (
        "Knowledge", result.note_relpath, "vault-jarvis-knowledge", "active")
    source = db.get_source_for_knowledge(result.ckc_id)
    assert source["url"] == f"ckc://conversation/{conv_id}"
    row = store.get_candidate_row(c.candidate_id)
    assert (row["status"], row["ckc_id"]) == ("published", result.ckc_id)
    assert not (env["oarai"] / "_jarvis").exists()


def test_unapproved_candidate_is_not_published(env, conv_id):
    c = _candidate(conv_id)
    with pytest.raises(publish.PublishError, match="承認されていない"):
        publish.publish(c.candidate_id)
    assert not (env["jk"] / "_jarvis").exists()


def test_secret_in_candidate_blocks_publish(env, conv_id):
    c = _candidate(conv_id)
    c.body = "キーは sk-ant-" + "x" * 40
    store.update_candidate(c)
    _approve(c)
    with pytest.raises(publish.PublishError, match="秘密情報"):
        publish.publish(c.candidate_id)
    assert store.get_candidate(c.candidate_id).status == CandidateStatus.APPROVED


def test_vault_failure_keeps_candidate_and_graph_untouched(env, conv_id):
    (env["jk"] / ".git" / "MERGE_HEAD").write_text("x")
    c = _candidate(conv_id)
    _approve(c)
    with pytest.raises(publish.PublishError, match="承認済みのまま"):
        publish.publish(c.candidate_id)
    assert store.get_candidate(c.candidate_id).status == CandidateStatus.APPROVED
    assert db.get_connection().execute("SELECT COUNT(*) FROM nodes WHERE type='Knowledge'").fetchone()[0] == 0


def test_same_title_gets_numbered_file(env, conv_id):
    paths = []
    for _ in range(2):
        c = _candidate(conv_id)
        _approve(c)
        paths.append(publish.publish(c.candidate_id).note_relpath)
    assert paths[1].endswith("_2.md") and paths[0] != paths[1]


def _commit_all(path):
    subprocess.run(["git", "add", "-A"], cwd=path, check=True)
    subprocess.run(["git", "commit", "-qm", "sync"], cwd=path, check=True)


def test_replace_existing_marks_old_superseded(env, conv_id):
    old = _candidate(conv_id, title="古い方針")
    _approve(old)
    old_result = publish.publish(old.candidate_id)
    _commit_all(env["jk"])  # Obsidian Git の同期を模擬
    old_text = (env["jk"] / old_result.note_relpath).read_text(encoding="utf-8")

    new = _candidate(conv_id, title="新しい方針")
    _approve(new, supersedes=old_result.ckc_id)
    new_result = publish.publish(new.candidate_id)
    assert new_result.warning is None
    old_note = parse_note((env["jk"] / old_result.note_relpath).read_text(encoding="utf-8"))
    assert (old_note.status, old_note.superseded_by) == (NoteStatus.SUPERSEDED, [new_result.ckc_id])
    assert old_note.body == parse_note(old_text).body  # 本文は変えない
    assert parse_note((env["jk"] / new_result.note_relpath).read_text(encoding="utf-8")).supersedes == [old_result.ckc_id]
    assert json.loads(db.get_node(old_result.ckc_id)["meta"])["status"] == "superseded"


def test_replace_with_unsynced_old_note_warns(env, conv_id):
    old = _candidate(conv_id, title="古い方針")
    _approve(old)
    old_result = publish.publish(old.candidate_id)  # 未コミットのまま（同期前）
    new = _candidate(conv_id, title="新しい方針")
    _approve(new, supersedes=old_result.ckc_id)
    result = publish.publish(new.candidate_id)
    assert "supersede" in result.warning
    assert parse_note((env["jk"] / old_result.note_relpath).read_text(encoding="utf-8")).status == NoteStatus.ACTIVE
