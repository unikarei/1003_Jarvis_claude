"""CKC の記録（T-109）のテスト。一時 DB と一時データ領域だけを使う。"""

import stat

import pytest

import db
from ckc import store
from ckc.models import (Candidate, CandidateStatus, Confidence, Conversation, Evidence, Kind, Origin, Speaker, Turn,
                        new_id)


@pytest.fixture
def env(db_path, ckc_data_dir):
    return ckc_data_dir


def _conv(text="大洗の件は来月決める"):
    return Conversation(new_id("conv"), Origin.PASTE, "テスト", [Turn(Speaker.OWNER, text), Turn(Speaker.AI, "了解")],
                        "2026-10-12T10:00:00+09:00")


def _cand(conv, **kw):
    base = dict(candidate_id=new_id("cand"), conversation_id=conv.conversation_id, kind=Kind.DECISION, title="題",
                body="本文", projects=["oarai-minpaku"], category="minpaku", confidence=Confidence.CONFIRMED,
                evidence=[Evidence("大洗の件は来月決める", Speaker.OWNER)])
    base.update(kw)
    return Candidate(**base)


def test_tables_added_without_touching_graph(db_path):
    with db.get_connection() as conn:
        tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    assert {"nodes", "edges", "ckc_conversations", "ckc_candidates"} <= tables
    db.init_db()  # 2回目も壊れない（CREATE TABLE IF NOT EXISTS）


def test_save_and_load_conversation(env):
    conv = _conv()
    path = store.save_conversation(conv)
    assert path == env / "raw" / f"{conv.conversation_id}.json"
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert store.load_conversation(conv.conversation_id) == conv
    assert store.find_conversation_by_hash(conv.content_hash()) == conv.conversation_id


def test_same_conversation_is_not_registered_twice(env):
    store.save_conversation(_conv())
    with pytest.raises(store.StoreError, match="取り込み済み"):
        store.save_conversation(_conv(" 大洗の件は来月決める\r\n"))


def test_candidates_round_trip_and_update(env):
    conv = _conv()
    store.save_conversation(conv)
    c = _cand(conv, similar=["ckc_x"])
    store.add_candidates([c])
    assert store.list_candidates(CandidateStatus.PENDING) == [c]
    c.title = "直した題"
    c.projects = ["jarvis", "oarai-minpaku"]
    store.update_candidate(c)
    assert store.get_candidate(c.candidate_id).title == "直した題"
    assert store.list_candidates(conversation_id=conv.conversation_id)[0].projects == ["jarvis", "oarai-minpaku"]


def test_status_transitions(env):
    conv = _conv()
    store.save_conversation(conv)
    c = _cand(conv)
    store.add_candidates([c])
    with pytest.raises(store.StoreError):
        store.set_status(c.candidate_id, CandidateStatus.PUBLISHED)  # 承認前は保存できない
    store.set_status(c.candidate_id, CandidateStatus.APPROVED)
    store.set_status(c.candidate_id, CandidateStatus.PUBLISHED, ckc_id="ckc_1", note_relpath="_jarvis/x.md")
    row = store.get_candidate_row(c.candidate_id)
    assert (row["status"], row["ckc_id"], row["note_relpath"]) == ("published", "ckc_1", "_jarvis/x.md")
    with pytest.raises(store.StoreError):
        store.set_status(c.candidate_id, CandidateStatus.PENDING)
    with pytest.raises(store.StoreError, match="保存済み"):
        store.update_candidate(store.get_candidate(c.candidate_id))


def test_rejected_can_be_reconsidered(env):
    conv = _conv()
    store.save_conversation(conv)
    c = _cand(conv)
    store.add_candidates([c])
    store.set_status(c.candidate_id, CandidateStatus.REJECTED)
    store.set_status(c.candidate_id, CandidateStatus.PENDING)
    assert store.get_candidate(c.candidate_id).status == CandidateStatus.PENDING
