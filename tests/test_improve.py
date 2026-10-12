"""自律改善（MAP Phase 10）のテスト。"""

import subprocess

import governance
import improve
from acc import build as acc_build
from discovery.guardian import Finding, Report

TASKS = """## 1. X

| ID | タスク | リポジトリ | 担当 | 依存 | 状態 |
|---|---|---|---|---|---|
| T-1 | a | 1003 | Claude | — | 完了 |
| T-2 | b | 1003 | Claude | T-1 | 未着手 |
| T-3 | c | 1003 | Claude | — | 保留（承認待ち） |
"""


def _report():
    r = Report(checked_at="2026-10-13T03:00:00+09:00")
    r.findings = [Finding("error", "unreachable", "api-x", "HTTP 500"),
                  Finding("warning", "unregistered", None, "Registry に未登録の launchd ジョブ: com.x")]
    return r


def test_proposals_are_prioritized_with_evidence():
    groups = governance.classify(governance.parse_tasks(TASKS))
    items = improve.proposals(
        _report(), {"knowledge": 10, "duplicate_titles": [["a", "b", "c"]], "stale": {"ai-news": 2}}, groups,
        ["u/a#1 と #2 が同じファイル"], {"repo-a": {"python_files": 3, "todo": 2, "long_files": ["big.py"],
                                                "merged_remote_branches": ["origin/old"]}},
        {"no_backup": ["data-db"], "manual_backups": ["2026-10-12_x"], "latest_manual": "2026-10-12_x"})
    assert [p.priority for p in items] == sorted((p.priority for p in items), key=improve.PRIORITY_ORDER.get)
    titles = " ".join(p.title for p in items)
    assert "api-x: HTTP 500" in titles and "バックアップのないデータが 1 件" in titles
    assert "未登録の資産が 1 件" in titles and "承認待ちのタスクが 1 件" in titles and "3 件）" in titles
    assert "TODO / FIXME が 2 件" in titles and "着手できるタスクが 1 件" in titles
    assert all(p.evidence and p.action for p in items)
    assert items[0].priority == "高"


def _git(path, *args):
    subprocess.run(["git", "-c", "user.name=t", "-c", "user.email=t@e", *args], cwd=path, check=True, capture_output=True)


def test_tech_debt_and_backup(tmp_path, monkeypatch):
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q", "-b", "main")
    (repo / "a.py").write_text("# TODO: 直す\nx = 1  # FIXME\n" + "y = 2\n" * 450)
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "init")
    registry = {"resources": {"repo-a": {"resource_type": "repository", "locator": str(repo)},
                              "data-d": {"resource_type": "data_store", "backup": "none"},
                              "data-e": {"resource_type": "data_store", "backup": "Time Machine"}}}
    debt = improve.tech_debt(registry)
    assert debt["repo-a"]["todo"] == 2 and debt["repo-a"]["long_files"] == ["a.py"]
    (tmp_path / "Backups" / "2026-10-12_a").mkdir(parents=True)
    monkeypatch.setattr(improve, "BACKUP_DIR", tmp_path / "Backups")
    assert improve.backup_status(registry) == {"no_backup": ["data-d"], "manual_backups": ["2026-10-12_a"],
                                               "latest_manual": "2026-10-12_a"}


def test_resume_summary(tmp_path, monkeypatch):
    (tmp_path / "docs").mkdir()
    (tmp_path / "docs" / "progress.md").write_text("# p\n\n## 現在の状態\n\n- 作業中: X\n\n## 2026-10-12\n\n- 古い\n")
    monkeypatch.setattr(governance, "WORKSPACE_DIR", tmp_path)
    assert improve.resume_summary() == "- 作業中: X"


def test_markdown_and_acc_section():
    data = {"generated_at": "2026-10-13T03:00", "resume": "- 作業中: X",
            "proposals": [{"priority": "高", "area": "構成", "title": "api-x <down>", "evidence": "Guardian",
                           "action": "直す"}], "ready_tasks": []}
    md = improve.to_markdown(data)
    assert "## 開発の再開" in md and "**[高・構成] api-x <down>**" in md and "根拠: Guardian" in md
    html = acc_build.improvement_section(data)
    assert "api-x &lt;down&gt;" in html and "開発の再開" in html
    assert "python improve.py" in acc_build.improvement_section(None)
