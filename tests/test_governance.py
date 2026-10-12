"""Multi-Agent Governance（MAP Phase 9）のテスト。gh・LLM はモック。"""

import json
import subprocess
from datetime import datetime, timezone

import governance as gv
import llm

TASKS = """# tasks

## 1. CKC Phase 1

| ID | タスク | リポジトリ | 担当 | 依存 | 状態 |
|---|---|---|---|---|---|
| T-101 | 文書 | 1003 | Claude | — | 完了 |
| T-102 | 基盤 | 1003 | Claude | T-101 | 完了（abc） |
| T-103 | 型 | 1003 | **Codex** | T-102 | 未着手 |
| T-104 | 検出 | 1003 | Codex | T-103 | 未着手 |
| T-105 | 公開 | 1003 | Claude | T-104、Owner の承認 | 保留（承認待ち） |
| T-106 | 作業 | 1003 | Claude | — | 作業中（branch） |

## 2. そのほか

| ID | タスク | リポジトリ | 依存 | 状態 |
|---|---|---|---|---|
| T-201 | 片付け | oarai | Owner | レビュー待ち（PR #1） |
"""


def test_parse_and_classify():
    tasks = gv.parse_tasks(TASKS)
    assert [t.task_id for t in tasks] == ["T-101", "T-102", "T-103", "T-104", "T-105", "T-106", "T-201"]
    t105 = next(t for t in tasks if t.task_id == "T-105")
    assert t105.deps == ["T-104"] and t105.status == "保留" and t105.section == "CKC Phase 1"
    g = gv.classify(tasks)
    assert [t.task_id for t in g["ready"]] == ["T-103"]
    assert [t.task_id for t in g["blocked"]] == ["T-104"]
    assert [t.task_id for t in g["waiting_approval"]] == ["T-105", "T-201"]
    assert [t.task_id for t in g["in_progress"]] == ["T-106"]


def _pr(n, base="main", head=None, files=(), created="2026-10-12T00:00:00Z", repo="u/a"):
    return gv.PullRequest(repo, n, f"PR {n}", base, head or f"feat/{n}", created, list(files))


def test_pr_findings():
    now = datetime(2026, 10, 20, tzinfo=timezone.utc)
    prs = [_pr(1, files=["a.py", "b.py"]), _pr(2, files=["b.py"]),
           _pr(3, base="feat/4", files=["c.py"]), _pr(4, head="feat/4", files=["c.py"], created="2026-10-19T00:00:00Z"),
           _pr(5, files=["b.py"], repo="u/other")]
    notes = gv.pr_findings(prs, now=now)
    assert any("u/a#1 と #2" in n and "b.py" in n for n in notes)          # 同じリポジトリで同じファイル
    assert not any("#3 と #4" in n for n in notes)                         # 積み重ねの親子は除く
    assert not any("u/other" in n and "競合" in n for n in notes)          # 別のリポジトリは関係ない
    assert any("u/a#3 は main ではなく `feat/4`" in n for n in notes)
    assert any("u/a#1 は 8 日間" in n for n in notes) and not any("#4 は" in n and "日間" in n for n in notes)


def test_repositories_from_registry(tmp_path, monkeypatch):
    import config
    path = tmp_path / "registry.json"
    path.write_text(json.dumps({"projects": {}, "resources": {
        "repo-a": {"resource_type": "repository", "git_remote": "https://github.com/u/a.git"},
        "repo-local": {"resource_type": "repository", "git_remote": "local-only"},
        "vault-v": {"resource_type": "vault", "git_remote": "https://github.com/u/v.git"}}}))
    monkeypatch.setattr(config, "REGISTRY_JSON", path)
    assert gv.repositories() == ["u/a"]


def test_board_and_collect(monkeypatch, tmp_path):
    monkeypatch.setattr(gv, "repositories", lambda: ["u/a", "u/broken"])

    def fake_run(cmd, timeout=60):
        if "u/broken" in cmd:
            return subprocess.CompletedProcess(cmd, 1, "", "not found")
        return subprocess.CompletedProcess(cmd, 0, json.dumps([{"number": 7, "title": "x", "baseRefName": "main",
                                                                 "headRefName": "feat/x", "createdAt": "2026-10-12T00:00:00Z",
                                                                 "files": [{"path": "a.py"}]}]), "")
    monkeypatch.setattr(gv, "run", fake_run)
    prs, errors = gv.collect_prs()
    assert [p.number for p in prs] == [7] and "u/broken" in errors[0]
    text = gv.board(gv.parse_tasks(TASKS), prs, gv.pr_findings(prs), errors)
    assert "着手できる 1" in text and "`T-103` 型" in text and "u/a#7" in text and "取得できなかったもの" in text


def test_codex_prompt_contains_rules():
    t = next(t for t in gv.parse_tasks(TASKS) if t.task_id == "T-103")
    text = gv.codex_prompt(t)
    assert "unikarei/1003_Jarvis_claude" in text and "worktree" in text and "feat/t-103" in text
    assert "main へのマージは Owner" in text


def test_review_uses_llm_and_does_not_post(monkeypatch):
    calls = []

    def fake_run(cmd, timeout=60):
        calls.append(cmd)
        return subprocess.CompletedProcess(cmd, 0, "タイトル" if "view" in cmd else "+ added line", "")
    monkeypatch.setattr(gv, "run", fake_run)
    monkeypatch.setattr(llm, "complete", lambda prompt, provider=None: "指摘なし" if "+ added line" in prompt else "x")
    assert gv.review("u/a", 7) == "指摘なし"
    assert all(c[:3] in (["gh", "pr", "view"], ["gh", "pr", "diff"]) for c in calls)   # 投稿・マージはしない
