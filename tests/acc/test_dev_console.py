"""acc/dev_console.py（ACC の開発オーケストレーターの操作画面、T-1305）のテスト。

本物のオーケストレーター・Claude・Codex は使わない。一時フォルダのワークスペースに偽の dev_orchestrator.py を置く。
"""

import http.client
import json
import os
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest

from acc import dev_console

TOKEN = "test-token-0123456789"

FAKE_ORCH = r'''
import json, os, signal, sys, time
from pathlib import Path
runs = Path.cwd() / ".orchestrator" / "runs"
lock = runs / ".lock"
if sys.argv[1] == "run":
    if os.environ.get("FAKE_REFUSE"):
        print("開始しません: 05:30〜07:30（Asia/Tokyo）は新しい run を始めない", file=sys.stderr)
        sys.exit(3)
    d = runs / "T-1" / "20261017T120000"
    d.mkdir(parents=True, exist_ok=True)
    lock.write_text(str(os.getpid()))
    Path(os.environ["FAKE_ARGS"]).write_text(json.dumps(sys.argv[1:]))
    state = {"task_id": "T-1", "status": "running", "stage": "claude", "pid": os.getpid(), "cycles": [{"cycle": 1}],
             "max_cycles": 3}
    (d / "state.json").write_text(json.dumps(state))
    def stop(*_):
        state.update(status="STOPPED", reason="stopped_by_owner", stage="finished")
        (d / "state.json").write_text(json.dumps(state))
        lock.unlink()
        sys.exit(4)
    signal.signal(signal.SIGTERM, stop)
    time.sleep(30)
elif sys.argv[1] == "worktrees":
    Path(os.environ["FAKE_ARGS"] + ".wt").write_text("called")
    print(os.environ.get("FAKE_WORKTREES", "[]"))
elif sys.argv[1] == "cleanup":
    Path(os.environ["FAKE_ARGS"] + ".cleanup").write_text(json.dumps(sys.argv[1:]))
    if os.environ.get("FAKE_CLEANUP_REFUSE"):
        print("消しません: origin/main にマージされていない", file=sys.stderr)
        sys.exit(1)
    print("消しました: /ws/.worktrees/demo-orch-t-9")
elif sys.argv[1] == "stop":
    pid = int(lock.read_text())
    os.kill(pid, signal.SIGTERM)
    for _ in range(100):
        if not lock.exists():
            break
        time.sleep(0.1)
    print(f"止めました（pid {pid}）。記録は STOPPED。")
'''


def _git(ws: Path, *args: str):
    subprocess.run(["git", *args], cwd=ws, check=True, capture_output=True)


@pytest.fixture
def ws(tmp_path, monkeypatch):
    ws = tmp_path / "ws"
    (ws / "scripts").mkdir(parents=True)
    (ws / "docs" / "codex").mkdir(parents=True)
    (ws / "scripts" / "dev_orchestrator.py").write_text(FAKE_ORCH, encoding="utf-8")
    (ws / "docs" / "codex" / "T-1_ok.md").write_text("---\ntask_id: T-1\nrepo: demo\ntest: true\n---\n# ok\n")
    (ws / "docs" / "codex" / "T-2_no_meta.md").write_text("# 先頭の YAML なし\n")
    (ws / "docs" / "codex" / "T-3_edited.md").write_text("---\ntask_id: T-3\nrepo: demo\ntest: true\n---\n")
    _git(ws, "init", "-q")
    _git(ws, "add", ".")
    _git(ws, "-c", "user.name=t", "-c", "user.email=t@example.com", "commit", "-q", "-m", "init")
    (ws / "docs" / "codex" / "T-3_edited.md").write_text("---\ntask_id: T-3\nrepo: demo\ntest: rm -rf /\n---\n")
    (ws / "docs" / "codex" / "T-4_untracked.md").write_text("---\ntask_id: T-4\nrepo: demo\ntest: true\n---\n")
    monkeypatch.setenv("JARVIS_WORKSPACE", str(ws))
    monkeypatch.setenv("FAKE_ARGS", str(tmp_path / "args.json"))
    return ws


@pytest.fixture
def server(ws):
    srv, token = dev_console.make_server(port=0, token=TOKEN)
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    yield srv
    srv.shutdown()
    srv.server_close()
    try:   # 残った偽の run を止める
        os.kill(int((ws / ".orchestrator" / "runs" / ".lock").read_text()), 15)
    except (OSError, ValueError):
        pass


def request(srv, method, path, body=None, cookie=True, host=None, csrf=True, origin=None):
    port = srv.server_address[1]
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=30)
    headers = {"Host": host or f"127.0.0.1:{port}"}
    if cookie:
        headers["Cookie"] = f"{dev_console.COOKIE}={TOKEN}"
    if csrf and method == "POST":
        headers[dev_console.CSRF_HEADER] = "1"
    if origin:
        headers["Origin"] = origin
    data = json.dumps(body).encode() if body is not None else None
    if data is not None:
        headers["Content-Type"] = "application/json"
    conn.request(method, path, body=data, headers=headers)
    res = conn.getresponse()
    raw = res.read()
    conn.close()
    try:
        return res.status, json.loads(raw), res
    except ValueError:
        return res.status, raw.decode(), res


def test_binds_loopback_only(server):
    assert server.server_address[0] == "127.0.0.1"


def test_requires_token(server):
    assert request(server, "GET", "/api/runs", cookie=False)[0] == 401
    assert request(server, "GET", "/", cookie=False)[0] == 401
    assert request(server, "GET", "/?token=wrong", cookie=False)[0] == 401


def test_token_url_sets_cookie_and_redirects(server):
    code, _, res = request(server, "GET", f"/?token={TOKEN}", cookie=False)
    assert code == 303 and res.getheader("Location") == "/"
    cookie = res.getheader("Set-Cookie")
    assert f"{dev_console.COOKIE}={TOKEN}" in cookie and "HttpOnly" in cookie and "SameSite=Strict" in cookie


def test_rejects_other_host(server):
    assert request(server, "GET", "/api/runs", host="evil.example:80")[0] == 403
    assert request(server, "GET", f"/?token={TOKEN}", cookie=False, host="evil.example")[0] == 403


def test_page_is_served_with_security_headers(server):
    code, body, res = request(server, "GET", "/")
    assert code == 200 and "開発オーケストレーター" in body
    assert res.getheader("X-Frame-Options") == "DENY"
    assert "frame-ancestors 'none'" in res.getheader("Content-Security-Policy")


def test_post_requires_csrf_header_and_same_origin(server):
    assert request(server, "POST", "/api/start", {"task": "T-1_ok.md"}, csrf=False)[0] == 403
    assert request(server, "POST", "/api/start", {"task": "T-1_ok.md"}, origin="https://evil.example")[0] == 403
    assert request(server, "POST", "/api/stop", {}, cookie=False)[0] == 401


def test_only_committed_unmodified_task_files(ws, server):
    code, data, _ = request(server, "GET", "/api/tasks")
    assert code == 200
    assert [t["name"] for t in data["tasks"]] == ["T-1_ok.md"]
    for name in ("T-2_no_meta.md", "T-3_edited.md", "T-4_untracked.md", "../AGENTS.md", "x; rm -rf /.md"):
        assert request(server, "POST", "/api/start", {"task": name})[0] == 400, name


def test_start_status_and_stop(ws, server, tmp_path):
    code, data, _ = request(server, "POST", "/api/start", {"task": "T-1_ok.md", "pr": True})
    assert code == 202 and data["started"] == "T-1"
    assert json.loads((tmp_path / "args.json").read_text()) == ["run", "--task", "docs/codex/T-1_ok.md", "--pr"]
    # 二重の開始は断る
    assert request(server, "POST", "/api/start", {"task": "T-1_ok.md"})[0] == 409
    code, data, _ = request(server, "GET", "/api/runs")
    assert data["running"] is True
    assert data["runs"][0]["status"] == "running" and data["runs"][0]["stage"] == "claude"
    code, data, _ = request(server, "POST", "/api/stop", {})
    assert code == 200 and "止めました" in data["message"]
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        _, data, _ = request(server, "GET", "/api/runs")
        if not data["running"]:
            break
        time.sleep(0.2)
    assert data["running"] is False
    assert (data["runs"][0]["status"], data["runs"][0]["reason"]) == ("STOPPED", "stopped_by_owner")


def test_refusal_reason_is_shown(server, monkeypatch):
    monkeypatch.setenv("FAKE_REFUSE", "1")
    code, data, _ = request(server, "POST", "/api/start", {"task": "T-1_ok.md"})
    assert code == 409 and "05:30" in data["error"]


def _write_run(ws: Path, status="PASS", pid=None) -> Path:
    d = ws / ".orchestrator" / "runs" / "T-9" / "20261017T100000"
    (d / "cycle-1").mkdir(parents=True)
    state = {"task_id": "T-9", "status": status, "stage": "finished", "pid": pid, "max_cycles": 3,
             "pr_url": "https://github.com/unikarei/demo/pull/1",
             "auth": {"claude": {"ok": True, "detail": "claude.ai（pro）", "authMethod": "claude.ai"}},
             "cycles": [{"cycle": 1, "verdict": "CHANGES_REQUESTED", "findings": 1, "test": {"returncode": 0},
                         "claude": {"models": ["m"], "seconds": 1.0, "usage": {"secret": 1}},
                         "codex": {"model": "gpt-6-sol"}}]}
    (d / "state.json").write_text(json.dumps(state), encoding="utf-8")
    (d / "cycle-1" / "review.json").write_text(json.dumps({"verdict": "CHANGES_REQUESTED", "summary": "直す",
                                                           "findings": [{"severity": "major", "file": "a.py",
                                                                         "line": 1, "message": "m"}]}))
    (d / "cycle-1" / "test.log").write_text("token sk-ant-" + "a" * 40 + "\n1 passed\n")
    (d / "cycle-1" / "claude.prompt.md").write_text("プロンプトは返さない")
    return d


def test_run_detail_masks_secrets_and_hides_prompts(ws, server):
    _write_run(ws)
    code, data, _ = request(server, "GET", "/api/runs/T-9/20261017T100000")
    assert code == 200
    text = json.dumps(data, ensure_ascii=False)
    assert "sk-ant-" + "a" * 40 not in text and "1 passed" in text
    assert "プロンプトは返さない" not in text and "usage" not in text
    c = data["cycles"][0]
    assert c["review"]["findings"][0]["message"] == "m" and c["codex"]["model"] == "gpt-6-sol"
    assert data["summary"]["pr_url"].endswith("/pull/1")
    assert data["auth"]["claude"] == {"ok": True, "detail": "claude.ai（pro）"}


def test_run_detail_rejects_paths_outside_runs(ws, server):
    _write_run(ws)
    for path in ("/api/runs/T-9/..", "/api/runs/../.orchestrator", "/api/runs/T-9/%2e%2e", "/api/runs/T-9/nope"):
        assert request(server, "GET", path)[0] == 404, path


def test_interrupted_when_pid_is_gone(ws):
    _write_run(ws, status="running", pid=999999999)
    assert dev_console.list_runs()[0]["status"] == "interrupted"


def test_main_refuses_without_orchestrator(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("JARVIS_WORKSPACE", str(tmp_path))
    assert dev_console.main(["--port", "0"]) == 1
    assert "オーケストレーターがありません" in capsys.readouterr().err


def test_buttons_show_disabled_state(server):
    _, page, _ = request(server, "GET", "/")
    assert "button:disabled" in page
    assert 'getElementById("stop").disabled = !d.running' in page
    assert 'getElementById("start").disabled = d.running' in page


def test_help_dialog(server):
    _, page, _ = request(server, "GET", "/")
    assert 'id="help-open"' in page and '<dialog id="help"' in page
    for text in ("1. 指示書を書く", "2. 指示書をコミットする", "3. 画面で開始する", "4. 進捗を見る",
                 "5. 結果を見て判断する", "同じ ID は使い回せません"):
        assert text in page, text
    assert "help.showModal()" in page


WT = [{"task_id": "T-9", "run": "20261017T100000", "removable": True, "reason": None},
      {"task_id": "T-8", "run": "20261017T090000", "removable": False, "reason": "origin/main にマージされていない"}]


def test_worktrees_are_listed_and_cached(server, monkeypatch, tmp_path):
    monkeypatch.setenv("FAKE_WORKTREES", json.dumps(WT))
    code, data, _ = request(server, "GET", "/api/worktrees")
    assert code == 200 and data["worktrees"] == WT
    (tmp_path / "args.json.wt").unlink()
    request(server, "GET", "/api/worktrees")
    assert not (tmp_path / "args.json.wt").exists()        # 60 秒は使い回す（git fetch を減らす）
    request(server, "GET", "/api/worktrees?refresh=1")
    assert (tmp_path / "args.json.wt").exists()
    assert request(server, "GET", "/api/worktrees", cookie=False)[0] == 401


def test_cleanup_calls_orchestrator_with_fixed_args(server, tmp_path):
    code, data, _ = request(server, "POST", "/api/cleanup", {"task_id": "T-9"})
    assert code == 200 and "消しました" in data["message"]
    assert json.loads((tmp_path / "args.json.cleanup").read_text()) == ["cleanup", "T-9"]


def test_cleanup_refusal_and_bad_input(server, monkeypatch, tmp_path):
    monkeypatch.setenv("FAKE_CLEANUP_REFUSE", "1")
    code, data, _ = request(server, "POST", "/api/cleanup", {"task_id": "T-8"})
    assert code == 409 and "マージされていない" in data["message"]
    for bad in ("../x", "T-9; rm -rf /", ""):
        assert request(server, "POST", "/api/cleanup", {"task_id": bad})[0] == 400, bad
    assert request(server, "POST", "/api/cleanup", {"task_id": "T-9"}, csrf=False)[0] == 403


def test_page_has_cleanup_button_and_help(server):
    _, page, _ = request(server, "GET", "/")
    assert "作業フォルダを削除" in page and "6. 後片付け（マージの後）" in page
