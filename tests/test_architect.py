"""Knowledge Architect（MAP Phase 8）のテスト。LLM・gh はモック、Git は一時フォルダ。"""

import json
import subprocess

import pytest

import architect
import config
import db
import llm


@pytest.fixture
def env(db_path, ckc_data_dir, make_vault, monkeypatch):
    monkeypatch.setattr(architect, "PROPOSAL_DIR", ckc_data_dir.parent / "architect")
    db.insert_node("Knowledge", category="manufacturing", title="タービン翼の冷却", content="冷却設計の論文メモ")
    return make_vault("jarvis-knowledge")


def _llm(monkeypatch, data):
    monkeypatch.setattr(llm, "complete", lambda prompt, provider=None: "提案です\n" + json.dumps(data, ensure_ascii=False))


GOOD = {"reuse": [{"id": "turbine-research", "reason": "既存の研究テーマ"}], "new_project_needed": True,
        "project": {"project_id": "turbine-cooling", "name": "タービン冷却", "description": "冷却設計",
                    "jarvis_category": "manufacturing", "default_vault": "vault-jarvis-knowledge"},
        "notes": [{"title": "冷却方式の比較", "purpose": "方式を比べる"}, {"title": "参考文献", "purpose": "論文"}],
        "rationale": "研究テーマを分ける"}


def test_propose_validates_and_saves(env, monkeypatch):
    _llm(monkeypatch, GOOD)
    p = architect.propose("タービンの冷却設計を調べたい")
    assert p.new_project_needed and p.project["project_id"] == "turbine-cooling"
    assert architect.load(p.proposal_id) == p
    text = architect.format_proposal(p)
    assert "turbine-research" in text and "冷却方式の比較" in text


@pytest.mark.parametrize("change, message", [
    ({"reuse": [{"id": "nope", "reason": "x"}]}, "一覧にない ID"),
    ({"project": {**GOOD["project"], "project_id": "Bad ID"}}, "形式が不正"),
    ({"project": {**GOOD["project"], "project_id": "jarvis"}}, "既存と重複"),
    ({"project": {**GOOD["project"], "default_vault": "vault-nope"}}, "Vault ではない"),
    ({"notes": []}, "notes"),
])
def test_invalid_proposals_are_rejected(env, monkeypatch, change, message):
    _llm(monkeypatch, {**GOOD, **change})
    with pytest.raises(architect.ArchitectError, match=message):
        architect.propose("タービンの冷却")


def test_reuse_only_proposal(env, monkeypatch):
    _llm(monkeypatch, {"reuse": [{"id": "turbine-research", "reason": "既存で足りる"}], "new_project_needed": False,
                       "project": None, "notes": [{"title": "冷却メモ", "purpose": "x"}]})
    p = architect.propose("タービンの冷却")
    assert p.project is None and "新しい Project は不要" in architect.format_proposal(p)
    assert architect.note_target(p) == ("turbine-research", "vault-jarvis-knowledge")
    applied = architect.apply(p)
    assert applied.applied == {"notes": ["_jarvis/themes/turbine-research/冷却メモ.md"]}   # PR は作らない


def _git(path, *args):
    return subprocess.run(["git", "-c", "user.name=t", "-c", "user.email=t@e", *args], cwd=path, check=True,
                          capture_output=True, text=True)


@pytest.fixture
def registry_repo(tmp_path):
    remote = tmp_path / "remote.git"
    _git(tmp_path, "init", "-q", "--bare", "-b", "main", str(remote))
    repo = tmp_path / "registry"
    _git(tmp_path, "clone", "-q", str(remote), str(repo))
    (repo / "registry").mkdir()
    (repo / "registry" / "projects.yaml").write_text("projects:\n  - project_id: jarvis\n    name: Jarvis\n    status: Registered\n")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "init")
    _git(repo, "push", "-q", "origin", "main")
    return repo, remote


def test_apply_creates_notes_and_registry_pr(env, monkeypatch, registry_repo):
    repo, remote = registry_repo
    _llm(monkeypatch, GOOD)
    p = architect.propose("タービンの冷却設計を調べたい")
    real = architect._run
    gh_calls = []

    def fake(cmd, cwd=None):
        if cmd[0] == "gh":
            gh_calls.append(cmd)
            return subprocess.CompletedProcess(cmd, 0, "https://github.com/x/pull/9\n", "")
        if "scripts/validate.py" in cmd:
            return subprocess.CompletedProcess(cmd, 0, "OK", "")
        if cmd[:2] == ["git", "commit"]:
            cmd = ["git", "-c", "user.name=t", "-c", "user.email=t@e", *cmd[1:]]
        return real(cmd, cwd=cwd)
    monkeypatch.setattr(architect, "_run", fake)

    applied = architect.apply(p, registry_dir=repo)
    assert applied.status == "applied" and applied.applied["registry_pr"] == "https://github.com/x/pull/9"
    assert sorted(applied.applied["notes"]) == sorted(["_jarvis/themes/turbine-cooling/参考文献.md",
                                                      "_jarvis/themes/turbine-cooling/冷却方式の比較.md"])
    assert (env / "_jarvis/themes/turbine-cooling/冷却方式の比較.md").read_text(encoding="utf-8").startswith("# 冷却方式の比較")
    pushed = _git(remote, "show", "architect/turbine-cooling:registry/projects.yaml").stdout
    assert "project_id: turbine-cooling" in pushed and 'name: "タービン冷却"' in pushed
    assert _git(repo, "rev-parse", "--abbrev-ref", "HEAD").stdout.strip() == "main"   # main の作業フォルダは変えない
    assert "turbine-cooling" not in (repo / "registry" / "projects.yaml").read_text()
    assert _git(repo, "worktree", "list").stdout.count("\n") == 1                      # 一時 worktree は消えている
    with pytest.raises(architect.ArchitectError, match="適用済み"):
        architect.apply(applied, registry_dir=repo)
