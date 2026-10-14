"""Discovery の収集（T-401）と Guardian の判定（T-402）のテスト。外部コマンドと HTTP はモック、Git は一時フォルダ。"""

import subprocess
from types import SimpleNamespace

import pytest
import requests

from discovery import collect, guardian, settings


def _git(path, *args):
    subprocess.run(["git", *args], cwd=path, check=True, capture_output=True)


@pytest.fixture
def repo(tmp_path):
    path = tmp_path / "repo"
    path.mkdir()
    _git(path, "init", "-q", "-b", "main")
    _git(path, "-c", "user.name=t", "-c", "user.email=t@e", "commit", "-q", "--allow-empty", "-m", "init")
    return path


def _resource(rid, kind, **kw):
    return {"resource_id": rid, "resource_type": kind, "project_id": "jarvis", **kw}


@pytest.fixture
def no_network(monkeypatch):
    """git ls-remote・gh・launchctl・docker は外に出さない。"""
    real = collect.run

    def fake(cmd, cwd=None, timeout=30):
        if cmd[:2] == ["git", "ls-remote"]:
            return subprocess.CompletedProcess(cmd, 0, "deadbeef\trefs/heads/main\n", "")
        if cmd[0] in ("gh",):
            return subprocess.CompletedProcess(cmd, 0, "2\n", "")
        if cmd[0] == "launchctl":
            return subprocess.CompletedProcess(cmd, 0, "PID\tStatus\tLabel\n123\t0\tcom.ohide.svc\n-\t0\tcom.ohide.job\n"
                                                        "-\t1\tcom.ohide.bad\n-\t0\tcom.ohide.extra\n", "")
        if cmd[0] == "docker":
            return subprocess.CompletedProcess(cmd, 0, "pocs0108_x\nnew-container\n", "")
        return real(cmd, cwd=cwd, timeout=timeout)
    monkeypatch.setattr(collect, "run", fake)


def test_repository_facts(repo, no_network):
    (repo / "dirty.txt").write_text("x")
    facts = collect.collect_repository(_resource("repo-x", "repository", locator=str(repo),
                                                 git_remote="https://github.com/u/x.git", branch="main"))
    assert facts["exists"] and facts["branch"] == "main" and facts["uncommitted"] == 1
    assert facts["in_sync_with_remote"] is False and facts["open_prs"] == 2 and facts["git_busy"] == []


def test_collection_uses_only_read_only_commands(repo, monkeypatch, no_network):
    calls = []
    inner = collect.run
    monkeypatch.setattr(collect, "run", lambda cmd, cwd=None, timeout=30: calls.append(cmd) or inner(cmd, cwd, timeout))
    collect.collect_repository(_resource("repo-x", "repository", locator=str(repo),
                                         git_remote="https://github.com/u/x.git", branch="main"))
    git_subcommands = {c[2] if c[1] == "--no-optional-locks" else c[1] for c in calls if c[0] == "git"}
    assert git_subcommands <= {"rev-parse", "status", "log", "ls-remote"}
    assert not any(c[0] == "git" and "fetch" in c for c in calls)


def test_launchd_and_http(no_network, monkeypatch):
    jobs = collect.launchd_jobs()
    assert collect.collect_launchd({"launchd_label": "com.ohide.svc"}, jobs) == {
        "loaded": True, "label": "com.ohide.svc", "running": True, "last_exit": 0}
    assert collect.collect_launchd({"launchd_label": "com.ohide.none"}, jobs)["loaded"] is False
    monkeypatch.setattr(requests, "get", lambda url, **k: SimpleNamespace(
        status_code=500, elapsed=SimpleNamespace(total_seconds=lambda: 0.2)))
    assert collect.collect_http({"healthcheck": {"type": "http", "target": "https://x/health"}})["status_code"] == 500

    def boom(url, **k):
        raise requests.ConnectionError()
    monkeypatch.setattr(requests, "get", boom)
    assert collect.collect_http({"url": "https://x/"})["error"] == "ConnectionError"


def test_full_collect_and_guardian(repo, tmp_path, monkeypatch, no_network):
    monkeypatch.setattr(settings, "WORKSPACE_DIR", tmp_path)       # repo（登録済み）と other（未登録）
    monkeypatch.setattr(settings, "VAULT_PARENT", tmp_path / "none")
    other = tmp_path / "other"
    other.mkdir()
    _git(other, "init", "-q")
    monkeypatch.setattr(requests, "get", lambda url, **k: SimpleNamespace(
        status_code=200 if "ok" in url else 503, elapsed=SimpleNamespace(total_seconds=lambda: 0.1)))
    registry = {"resources": {r["resource_id"]: r for r in [
        _resource("repo-x", "repository", locator=str(repo), git_remote="local-only", branch="main"),
        _resource("repo-gone", "repository", locator=str(tmp_path / "gone"), git_remote="local-only", branch="main"),
        _resource("svc-a", "service", launchd_label="com.ohide.svc"),
        _resource("job-ok", "scheduled_job", launchd_label="com.ohide.job", depends_on=["api-down"]),
        _resource("job-bad", "scheduled_job", launchd_label="com.ohide.bad"),
        _resource("web-ok", "website", url="https://ok/"),
        _resource("api-down", "api", base_url="https://down/api"),
        _resource("data-d", "data_store", locator=str(tmp_path / "nope.db"), format="sqlite", backup="none"),
        _resource("ext-z", "external_service"),
    ]}}
    snapshot = collect.collect(registry)
    report = guardian.evaluate(registry, snapshot)
    states = {rid: j.state for rid, j in report.judgements.items()}
    assert states == {"repo-x": "Verified", "repo-gone": "Failed", "svc-a": "Verified", "job-ok": "Verified",
                      "job-bad": "Failed", "web-ok": "Verified", "api-down": "Failed", "data-d": "Failed",
                      "ext-z": "Unknown"}
    assert all(j.checked_at for j in report.judgements.values() if j.state == "Verified")
    kinds = {(f.kind, f.resource_id) for f in report.findings}
    assert ("missing", "repo-gone") in kinds and ("stopped", "job-bad") in kinds
    assert ("unreachable", "api-down") in kinds and ("backup", "data-d") in kinds
    assert ("dependency", "job-ok") in kinds
    messages = " ".join(f.message for f in report.findings if f.kind == "unregistered")
    assert str(other) in messages and "com.ohide.extra" in messages and "new-container" in messages
    assert snapshot["out_of_scope"]["docker"] == ["pocs0108_x"]
    assert [f.severity for f in report.findings] == sorted((f.severity for f in report.findings),
                                                          key=guardian.SEVERITY_ORDER.get)


def test_one_failing_collector_does_not_stop_the_rest(no_network, monkeypatch, tmp_path):
    monkeypatch.setattr(settings, "WORKSPACE_DIR", tmp_path)
    monkeypatch.setattr(settings, "VAULT_PARENT", tmp_path)

    def broken(resource):
        raise RuntimeError("壊れた")
    monkeypatch.setattr(collect, "collect_data_store", broken)
    registry = {"resources": {"data-a": _resource("data-a", "data_store", locator="/x", format="sqlite", backup="b"),
                              "svc-a": _resource("svc-a", "service", launchd_label="com.ohide.svc")}}
    report = guardian.evaluate(registry, collect.collect(registry))
    assert report.judgements["data-a"].state == "Unknown" and report.judgements["svc-a"].state == "Verified"


def test_vault_branch_mismatch_and_merge_in_progress(repo, no_network):
    (repo / ".git" / "MERGE_HEAD").write_text("x")
    res = _resource("vault-v", "vault", locator=str(repo), git_remote="local-only", branch="develop")
    findings = []
    state, _ = guardian._judge_repository("vault-v", res, collect.collect_vault(res), findings)
    assert state == "Failed" and findings[0].kind == "sync"


def test_run_tolerates_missing_command_and_timeout(monkeypatch):
    """launchd の PATH にコマンドがない・時間切れでも例外にしない（2026-10-14 の初回の定期実行で docker が見つからず停止した）。"""
    out = collect.run(["jarvis-no-such-command-xyz", "ps"])
    assert out.returncode == 127 and "見つかりません" in out.stderr

    def slow(*a, **kw):
        raise subprocess.TimeoutExpired(cmd=a[0], timeout=1)
    monkeypatch.setattr(subprocess, "run", slow)
    out = collect.run(["sleep", "10"], timeout=1)
    assert out.returncode == 124 and "時間切れ" in out.stderr
