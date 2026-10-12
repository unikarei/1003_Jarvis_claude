"""CLI（T-114）のテスト。LLM はモック、仮の Vault と一時 DB を使う。"""

import json

import pytest

import ckc_cli
import llm
from ckc import review, store
from ckc.models import CandidateStatus, Kind

CONVERSATION = ("Owner: 大洗以外の知識は jarvis-knowledge に保存することにする\n"
                "AI: タグで分類する方法もあります\n"
                "Owner: 来週までに Windows の設定を確認しておく\n")


def _item(kind, title, quote, speaker="owner"):
    return {"kind": kind, "title": title, "body": f"{title}の本文", "projects": ["jarvis"], "category": None,
            "confidence": "confirmed", "evidence": [{"quote": quote, "speaker": speaker}]}


@pytest.fixture
def env(db_path, ckc_data_dir, make_vault, monkeypatch, tmp_path):
    jk = make_vault("jarvis-knowledge")
    response = {"items": [
        _item("Decision", "汎用 Vault の採用", "大洗以外の知識は jarvis-knowledge に保存することにする"),
        _item("Hypothesis", "タグで分類する案", "タグで分類する方法もあります", speaker="ai"),
        _item("Task", "Windows の設定確認", "来週までに Windows の設定を確認しておく"),
    ]}
    monkeypatch.setattr(llm, "complete", lambda prompt, provider=None: json.dumps(response, ensure_ascii=False))
    f = tmp_path / "会話.txt"
    f.write_text(CONVERSATION, encoding="utf-8")
    return {"jk": jk, "file": f}


def _answers(monkeypatch, answers):
    it = iter(answers)
    monkeypatch.setattr("builtins.input", lambda prompt="": next(it))


def _notes(jk):
    return sorted(p.relative_to(jk).as_posix() for p in (jk / "_jarvis").rglob("*.md")) if (jk / "_jarvis").exists() else []


def test_import_edit_reject_approve_publish(env, monkeypatch, capsys):
    # 1件目を修正して承認、2件目を却下、3件目を承認 → 保存
    _answers(monkeypatch, ["e", "汎用 Vault を採用する", "", "", "", "a", "r", "a", "y"])
    assert ckc_cli.main(["import", str(env["file"])]) == 0
    notes = _notes(env["jk"])
    assert len(notes) == 2
    assert any(n.startswith("_jarvis/knowledge/Decision/") and "汎用 Vault を採用する" in n for n in notes)
    assert any(n.startswith("_jarvis/knowledge/Task/") for n in notes)
    statuses = sorted(c.status.value for c in store.list_candidates())
    assert statuses == ["published", "published", "rejected"]


def test_import_twice_does_not_duplicate(env, monkeypatch, capsys):
    assert ckc_cli.main(["import", str(env["file"]), "--no-review"]) == 0
    assert ckc_cli.main(["import", str(env["file"]), "--no-review"]) == 0
    assert "取り込み済み" in capsys.readouterr().out
    assert len(store.list_candidates()) == 3


def test_decline_publish_keeps_approved(env, monkeypatch):
    _answers(monkeypatch, ["a", "s", "s", "n"])
    ckc_cli.main(["import", str(env["file"])])
    assert _notes(env["jk"]) == []
    assert [c.status for c in store.list_candidates()][0] == CandidateStatus.APPROVED
    assert ckc_cli.main(["publish"]) == 0
    assert len(_notes(env["jk"])) == 1


def test_non_interactive_commands(env, capsys):
    ckc_cli.main(["import", str(env["file"]), "--no-review"])
    first, second, third = store.list_candidates()
    sid = review.short_id(first.candidate_id)
    assert ckc_cli.main(["publish", sid]) == 1  # 未承認は保存しない
    assert ckc_cli.main(["edit", sid, "--kind", "Task", "--projects", "jarvis,piano"]) == 0
    edited = store.get_candidate(first.candidate_id)
    assert (edited.kind, edited.projects, edited.resource_id) == (Kind.TASK, ["jarvis", "piano"], "vault-jarvis-knowledge")
    assert ckc_cli.main(["approve", sid]) == 0
    assert ckc_cli.main(["reject", review.short_id(second.candidate_id)]) == 0
    assert ckc_cli.main(["publish"]) == 0
    assert len(_notes(env["jk"])) == 1
    capsys.readouterr()
    assert ckc_cli.main(["list", "--status", "pending"]) == 0
    assert review.short_id(third.candidate_id) in capsys.readouterr().out


def test_edit_that_breaks_rules_is_refused(env, capsys):
    ckc_cli.main(["import", str(env["file"]), "--no-review"])
    hypothesis = store.list_candidates()[1]  # AI の発言が根拠
    assert ckc_cli.main(["edit", review.short_id(hypothesis.candidate_id), "--kind", "Decision"]) == 1
    assert "検証に通りません" in capsys.readouterr().err
    assert store.get_candidate(hypothesis.candidate_id).kind == Kind.HYPOTHESIS


def test_secret_in_conversation_stops_import(env, tmp_path, capsys):
    f = tmp_path / "secret.txt"
    f.write_text("Owner: キーは sk-ant-" + "x" * 40 + " です\n", encoding="utf-8")
    assert ckc_cli.main(["import", str(f)]) == 1
    assert "秘密情報" in capsys.readouterr().err
    assert store.list_candidates() == []
