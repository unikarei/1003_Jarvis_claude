"""discover.py --acc（ACC の自動更新、T-704）のテスト。収集・判定・書き出し・コミット・ACC の生成はモック。"""

import json
import subprocess

import pytest

import discover
from acc import build
from discovery import notify as nt


@pytest.fixture
def env(tmp_path, monkeypatch):
    """Registry を読める状態にし、Discovery の各段階と ACC の生成の呼び出しを記録する。"""
    (tmp_path / "generated").mkdir()
    (tmp_path / "generated" / "registry.json").write_text(json.dumps({"resources": []}), encoding="utf-8")
    calls = []
    monkeypatch.setattr(discover, "collect", lambda registry: {})
    monkeypatch.setattr(discover, "evaluate", lambda registry, snapshot: None)
    monkeypatch.setattr(discover, "render_outputs", lambda *a: {"generated/guardian-report.md": "report"})
    monkeypatch.setattr(discover, "write_outputs", lambda d, texts: calls.append("write") or [])
    monkeypatch.setattr(discover, "notify_errors", lambda report: False)
    monkeypatch.setattr(discover.subprocess, "run",
                        lambda cmd, env=None: calls.append("commit") or subprocess.CompletedProcess(cmd, 0))
    monkeypatch.setattr(build, "main", lambda argv=None: calls.append(("acc", argv)) or 0)
    sent = []
    monkeypatch.setattr(nt, "run", lambda cmd: sent.append(cmd[-1]))
    return tmp_path, calls, sent


def test_acc_is_built_after_write_and_before_commit(env):
    reg, calls, _ = env
    assert discover.main(["--registry-dir", str(reg), "--commit", "--acc"]) == 0
    assert calls == ["write", ("acc", ["--registry-dir", str(reg)]), "commit"]


def test_no_acc_without_flag_or_on_dry_run(env, capsys):
    reg, calls, _ = env
    assert discover.main(["--registry-dir", str(reg)]) == 0
    assert discover.main(["--registry-dir", str(reg), "--dry-run", "--acc"]) == 0
    assert all(not (isinstance(c, tuple) and c[0] == "acc") for c in calls)


def test_acc_failure_keeps_commit_and_is_notified(env, monkeypatch):
    reg, calls, sent = env
    monkeypatch.setattr(build, "main", lambda argv=None: calls.append("acc") or 2)
    assert discover.main(["--registry-dir", str(reg), "--commit", "--acc", "--notify"]) == 2
    assert calls == ["write", "acc", "commit"]
    assert len(sent) == 1 and "Discovery" in sent[0] and "終了コード 2" in sent[0]


def test_acc_exception_does_not_stop_commit(env, monkeypatch):
    reg, calls, _ = env
    monkeypatch.setattr(build, "main", lambda argv=None: (_ for _ in ()).throw(OSError("書けない")))
    assert discover.main(["--registry-dir", str(reg), "--commit", "--acc"]) == 1
    assert calls == ["write", "commit"]


def test_commit_failure_takes_precedence(env, monkeypatch):
    reg, calls, _ = env
    monkeypatch.setattr(discover.subprocess, "run", lambda cmd, env=None: subprocess.CompletedProcess(cmd, 3))
    monkeypatch.setattr(build, "main", lambda argv=None: 2)
    assert discover.main(["--registry-dir", str(reg), "--commit", "--acc"]) == 3


def test_registered_plist_runs_with_acc():
    from pathlib import Path
    plist = (Path(discover.__file__).parent / "scripts" / "launchd" / "com.ohide.jarvis.discovery.plist").read_text()
    for arg in ("--commit", "--notify", "--acc"):
        assert f"<string>{arg}</string>" in plist
