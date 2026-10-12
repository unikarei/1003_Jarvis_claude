"""scripts/commit_registry_outputs.sh（T-404）のテスト。一時フォルダの bare リポジトリを origin にする。"""

import os
import subprocess
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "commit_registry_outputs.sh"
TARGETS = ["discovered/latest.json", "generated/guardian-report.md", "generated/architecture.mmd", "generated/STATUS.md"]


def _git(path, *args, check=True):
    return subprocess.run(["git", "-c", "user.name=t", "-c", "user.email=t@e", *args], cwd=path, check=check,
                          capture_output=True, text=True)


@pytest.fixture
def repos(tmp_path):
    remote = tmp_path / "remote.git"
    _git(tmp_path, "init", "-q", "--bare", "-b", "main", str(remote))
    local = tmp_path / "registry"
    _git(tmp_path, "clone", "-q", str(remote), str(local))
    (local / "registry").mkdir()
    (local / "registry" / "resources.yaml").write_text("resources: []\n")
    _git(local, "add", "-A")
    _git(local, "commit", "-q", "-m", "init")
    _git(local, "push", "-q", "origin", "main")
    return local, remote


def _write_targets(local, text="v1"):
    for t in TARGETS:
        (local / t).parent.mkdir(parents=True, exist_ok=True)
        (local / t).write_text(text)


def _run(local):
    env = {**os.environ, "REGISTRY_REPO": str(local), "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@e",
           "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@e"}
    return subprocess.run(["/bin/bash", str(SCRIPT)], env=env, capture_output=True, text=True)


def test_commits_only_targets_and_pushes(repos):
    local, remote = repos
    _write_targets(local)
    (local / "registry" / "resources.yaml").write_text("resources: [changed]\n")  # 人の編集中の変更
    result = _run(local)
    assert result.returncode == 0, result.stdout
    files = _git(remote, "show", "--name-only", "--format=", "main").stdout.split()
    assert sorted(files) == sorted(TARGETS)
    assert "resources: [changed]" in (local / "registry" / "resources.yaml").read_text()  # 触れていない
    assert _git(local, "status", "--porcelain").stdout.strip() == "M registry/resources.yaml"


def test_no_changes_is_noop(repos):
    local, _ = repos
    _write_targets(local)
    assert _run(local).returncode == 0
    second = _run(local)
    assert second.returncode == 0 and "nothing to commit" in second.stdout


@pytest.mark.parametrize("setup, message", [
    (lambda l: _git(l, "switch", "-q", "-c", "feature"), "not main"),
    (lambda l: (l / ".git" / "MERGE_HEAD").write_text("x"), "MERGE_HEAD"),
    (lambda l: _git(l, "add", "registry/resources.yaml"), "already staged"),
])
def test_aborts(repos, setup, message):
    local, remote = repos
    (local / "registry" / "resources.yaml").write_text("resources: [x]\n")
    setup(local)
    _write_targets(local)
    result = _run(local)
    assert result.returncode == 1 and message in result.stdout
    assert _git(remote, "log", "--oneline", "main").stdout.count("\n") == 1  # push されていない


def test_aborts_when_diverged(repos, tmp_path):
    local, remote = repos
    other = tmp_path / "other"
    _git(tmp_path, "clone", "-q", str(remote), str(other))
    (other / "x").write_text("x")
    _git(other, "add", "x")
    _git(other, "commit", "-q", "-m", "other")
    _git(other, "push", "-q", "origin", "main")
    (local / "y").write_text("y")
    _git(local, "add", "y")
    _git(local, "commit", "-q", "-m", "local")
    _write_targets(local)
    result = _run(local)
    assert result.returncode == 1 and "diverged" in result.stdout
