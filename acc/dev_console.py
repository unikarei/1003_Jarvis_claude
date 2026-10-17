"""ACC の開発オーケストレーターの操作画面（../docs/dev-orchestrator-design.md §16、T-1305）。

統合ワークスペースの scripts/dev_orchestrator.py を、決まった引数（run --task <許可した指示書> [--pr]・stop）で
呼ぶだけで、実行エンジンは持たない。状態は .orchestrator/runs/<task>/<日時>/ の記録を読む。
127.0.0.1 に固定し、起動のたびに作るトークン（ターミナルにだけ表示）で認証する。常駐はしない。

使い方（VS Code のターミナルから）: python -m acc.dev_console [--port 8770]
"""

import argparse
import hmac
import json
import os
import re
import secrets
import subprocess
import sys
import threading
import time
from http.cookies import SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import config
from ckc.secrets import mask_secrets

HOST = "127.0.0.1"   # 固定。Tailscale などへの公開は別の承認
DEFAULT_PORT = 8770
COOKIE = "jarvis_dev_console"
CSRF_HEADER = "X-Jarvis-Console"
TASK_NAME = re.compile(r"^[A-Za-z0-9._-]+\.md$")
RUN_PART = re.compile(r"^[A-Za-z0-9._-]+$")
LOG_TAIL = 6000
START_WAIT_SECONDS = 3   # すぐ断られた（実行しない時間帯・実行中）ときの理由を拾う
WORKTREES_TTL = 60       # 作業フォルダの判定は git fetch を伴うため、この秒数だけ使い回す


def workspace() -> Path:
    return Path(os.environ.get("JARVIS_WORKSPACE", config.BASE_DIR.parent)).expanduser().resolve()


def orchestrator() -> Path:
    return workspace() / "scripts" / "dev_orchestrator.py"


def runs_dir() -> Path:
    return workspace() / ".orchestrator" / "runs"


def _git(*args: str) -> subprocess.CompletedProcess:
    return subprocess.run(["git", *args], cwd=workspace(), capture_output=True, text=True, timeout=30)


def _front_matter(text: str) -> dict[str, str]:
    m = re.match(r"\A---\n(.*?)\n---\n", text, re.S)
    meta = {}
    for line in (m.group(1).splitlines() if m else []):
        key, sep, value = line.partition(":")
        if sep and not line.startswith((" ", "#")):
            meta[key.strip()] = value.strip()
    return meta


def allowed_tasks() -> dict[str, dict]:
    """開始できる指示書: docs/codex/ 直下で、Git で管理され未変更、先頭に task_id・repo・test があるもの。
    指示書の setup・test はシェルで実行されるため、Owner がコミットしたものだけを許す。"""
    tracked = set(_git("ls-files", "docs/codex").stdout.split())
    changed = set(_git("status", "--porcelain", "--", "docs/codex").stdout.split())
    tasks = {}
    for path in sorted((workspace() / "docs" / "codex").glob("*.md")):
        rel = f"docs/codex/{path.name}"
        if not TASK_NAME.match(path.name) or rel not in tracked or rel in changed:
            continue
        meta = _front_matter(path.read_text(encoding="utf-8"))
        if all(meta.get(k) for k in ("task_id", "repo", "test")):
            tasks[path.name] = {"name": path.name, "task_id": meta["task_id"], "repo": meta["repo"]}
    return tasks


def completed_task_ids() -> set[str]:
    """完了済みの task_id: 一度でも PASS したもの、または後片付け（作業フォルダの削除）が済んだもの。
    後片付けは作業ブランチが main にマージ済みのときだけできるため、判定が PASS でなくても（Claude Code が
    残りを確かめてマージした場合など）完了として扱う。開始のリストから外し、開始も断る。"""
    done = set()
    for f in runs_dir().glob("*/*/state.json"):
        s = _read_json(f)
        if isinstance(s, dict) and s.get("status") == "PASS" and s.get("task_id"):
            done.add(s["task_id"])
    return done | set(cleanup_times([f.parent for f in runs_dir().glob("*/*/state.json")]))


def startable_tasks() -> tuple[dict[str, dict], list[dict]]:
    """（開始できる指示書, 完了済みで外した指示書）。"""
    done = completed_task_ids()
    tasks = allowed_tasks()
    return ({k: t for k, t in tasks.items() if t["task_id"] not in done},
            [t for t in tasks.values() if t["task_id"] in done])


def _pid_alive(pid) -> bool:
    try:
        os.kill(int(pid), 0)
        return True
    except (TypeError, ValueError, ProcessLookupError):
        return False
    except PermissionError:
        return True


def _read_json(path: Path):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def run_summary(d: Path) -> dict | None:
    s = _read_json(d / "state.json")
    if not isinstance(s, dict):
        return None
    status = s.get("status")
    if status == "running" and not _pid_alive(s.get("pid")):
        status = "interrupted"   # 強制終了・再起動などで記録が閉じていない
    cycles = s.get("cycles") or []
    return {"task_id": s.get("task_id"), "run": d.name, "status": status, "stage": s.get("stage"),
            "cycles": len(cycles), "max_cycles": s.get("max_cycles"),
            "verdict": cycles[-1].get("verdict") if cycles else None,
            "findings": [c.get("findings") for c in cycles], "reason": s.get("reason"),
            "pr_url": s.get("pr_url"), "started_at": s.get("started_at"), "finished_at": s.get("finished_at"),
            "updated_at": s.get("updated_at")}


def cleanup_times(dirs: list[Path]) -> dict[str, str]:
    """タスクごとの最後の後片付け（events.jsonl の stage=cleanup）の時刻。"""
    times: dict[str, str] = {}
    for d in dirs:
        try:
            lines = (d / "events.jsonl").read_text(encoding="utf-8").splitlines()
        except OSError:
            continue
        for line in lines:
            if '"cleanup"' not in line:
                continue
            try:
                e = json.loads(line)
            except ValueError:
                continue
            if e.get("stage") == "cleanup" and e.get("at"):
                task = d.parent.name
                times[task] = max(times.get(task, ""), e["at"])
    return times


def list_runs(limit: int = 100) -> list[dict]:
    """新しい順。後片付けより前に始まった run は archived（一覧から外し、履歴に出す。記録は消さない）。"""
    base = runs_dir()
    dirs = sorted((d for d in base.glob("*/*") if (d / "state.json").exists()), key=lambda d: d.name, reverse=True)
    cleaned = cleanup_times(dirs)
    runs = []
    for r in (run_summary(d) for d in dirs[:limit]):
        if r:
            at = cleaned.get(r["task_id"])
            r["archived"] = bool(at and (r.get("started_at") or "") < at and r["status"] != "running")
            runs.append(r)
    return runs


def _tail(path: Path, n: int = LOG_TAIL) -> str | None:
    try:
        return mask_secrets(path.read_text(encoding="utf-8", errors="replace")[-n:])
    except OSError:
        return None


def run_detail(task_id: str, run: str) -> dict | None:
    """1 件の記録。プロンプトと Claude の生の結果は返さない。テキストは秘密情報を伏せる。"""
    if not (RUN_PART.match(task_id) and RUN_PART.match(run)):
        return None
    base = runs_dir().resolve()
    d = (base / task_id / run).resolve()
    if d.parent.parent != base or not (d / "state.json").exists():
        return None
    state = _read_json(d / "state.json") or {}
    events = (_tail(d / "events.jsonl", 20000) or "").splitlines()[-100:]
    cycles = []
    for c in state.get("cycles") or []:
        n = c.get("cycle")
        cdir = d / f"cycle-{n}"
        cycles.append({"cycle": n, "claude": {k: (c.get("claude") or {}).get(k) for k in ("models", "seconds",
                                                                                         "num_turns")},
                       "test": c.get("test"), "codex": c.get("codex"), "verdict": c.get("verdict"),
                       "review": _read_json(cdir / "review.json"), "test_log": _tail(cdir / "test.log"),
                       "diffstat": _tail(cdir / "diffstat.txt")})
    out = {"summary": run_summary(d), "cycles": cycles, "events": [mask_secrets(e) for e in events],
           "summary_md": _tail(d / "summary.md", 20000),
           "auth": {k: {"ok": v.get("ok"), "detail": v.get("detail")} for k, v in (state.get("auth") or {}).items()}}
    return json.loads(mask_secrets(json.dumps(out, ensure_ascii=False)))


class Controller:
    """開始・停止。オーケストレーターの CLI を決まった引数で呼ぶ（シェルは使わない）。"""

    def __init__(self):
        self._lock = threading.Lock()
        self.proc: subprocess.Popen | None = None
        self._worktrees: tuple[float, list] | None = None

    def running(self) -> bool:
        if self.proc is not None and self.proc.poll() is None:
            return True
        try:
            return _pid_alive((runs_dir() / ".lock").read_text().strip())
        except OSError:
            return False

    def start(self, name: str, pr: bool) -> tuple[int, dict]:
        tasks, done = startable_tasks()
        if any(t["name"] == name for t in done):
            return 400, {"error": "完了済み（PASS 済み、またはマージして後片付け済み）の指示書です。やり直すときは新しい task_id の指示書を作ってください"}
        if name not in tasks:
            return 400, {"error": "開始できる指示書ではありません（docs/codex/ の、コミット済みで未変更のものだけ）"}
        with self._lock:
            if self.running():
                return 409, {"error": "別の run が実行中です（1 件ずつ）"}
            log_dir = workspace() / ".orchestrator" / "console"
            log_dir.mkdir(parents=True, exist_ok=True)
            log = log_dir / f"{time.strftime('%Y%m%dT%H%M%S')}-{tasks[name]['task_id']}.log"
            cmd = [sys.executable, str(orchestrator()), "run", "--task", f"docs/codex/{name}"] + (["--pr"] if pr else [])
            with log.open("w", encoding="utf-8") as f:
                self.proc = subprocess.Popen(cmd, cwd=workspace(), stdin=subprocess.DEVNULL, stdout=f,
                                             stderr=subprocess.STDOUT, start_new_session=True)
            deadline = time.monotonic() + START_WAIT_SECONDS
            while time.monotonic() < deadline and self.proc.poll() is None:
                time.sleep(0.1)
            rc = self.proc.poll()
        if rc == 3:   # 実行しない時間帯・実行中・指示書の誤り（オーケストレーターが断った）
            return 409, {"error": mask_secrets(log.read_text(encoding="utf-8").strip()) or "開始を断られました"}
        return 202, {"started": tasks[name]["task_id"], "pid": self.proc.pid, "exited": rc}

    def _cli(self, *args: str, timeout: int = 90) -> subprocess.CompletedProcess:
        return subprocess.run([sys.executable, str(orchestrator()), *args], cwd=workspace(), capture_output=True,
                              text=True, timeout=timeout, stdin=subprocess.DEVNULL)

    def stop(self) -> tuple[int, dict]:
        res = self._cli("stop")
        return (200 if res.returncode == 0 else 500), {"message": mask_secrets((res.stdout + res.stderr).strip())}

    def worktrees(self, refresh: bool = False) -> tuple[int, dict]:
        """残っている作業フォルダと、消してよいか（マージ済み・変更なし・実行中でない）。判定はオーケストレーター。"""
        if not refresh and self._worktrees and time.monotonic() - self._worktrees[0] < WORKTREES_TTL:
            return 200, {"worktrees": self._worktrees[1]}
        res = self._cli("worktrees", "--json", timeout=300)
        try:
            items = json.loads(res.stdout)
        except ValueError:
            return 500, {"error": mask_secrets((res.stderr or res.stdout).strip()[:500]) or "判定できません"}
        self._worktrees = (time.monotonic(), items)
        return 200, {"worktrees": items}

    def cleanup(self, task_id: str) -> tuple[int, dict]:
        if not RUN_PART.match(task_id):
            return 400, {"error": "タスクの ID が違います"}
        with self._lock:
            res = self._cli("cleanup", task_id, timeout=300)
            self._worktrees = None
        return (200 if res.returncode == 0 else 409), {"message": mask_secrets((res.stdout + res.stderr).strip())}


def make_handler(token: str, port: int, controller: Controller):
    allowed_hosts = {f"127.0.0.1:{port}", f"localhost:{port}"}
    allowed_origins = {f"http://{h}" for h in allowed_hosts}

    class Handler(BaseHTTPRequestHandler):
        server_version = "JarvisDevConsole"

        def log_message(self, fmt, *args):   # URL のトークンを端末のログに出さない
            sys.stderr.write(f"{self.command} {urlparse(self.path).path} -> {args[1] if len(args) > 1 else ''}\n")

        def _send(self, code: int, body: bytes, ctype: str, headers: dict | None = None):
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("X-Frame-Options", "DENY")
            self.send_header("Referrer-Policy", "no-referrer")
            self.send_header("Content-Security-Policy",
                             "default-src 'self'; script-src 'unsafe-inline'; style-src 'unsafe-inline'; "
                             "connect-src 'self'; frame-ancestors 'none'")
            for k, v in (headers or {}).items():
                self.send_header(k, v)
            self.end_headers()
            self.wfile.write(body)

        def _json(self, code: int, data):
            self._send(code, json.dumps(data, ensure_ascii=False).encode("utf-8"), "application/json; charset=utf-8")

        def _host_ok(self) -> bool:
            return self.headers.get("Host", "") in allowed_hosts

        def _authed(self) -> bool:
            cookie = SimpleCookie(self.headers.get("Cookie", ""))
            value = cookie[COOKIE].value if COOKIE in cookie else ""
            return hmac.compare_digest(value.encode(), token.encode())

        def _guard(self, write: bool = False) -> bool:
            if not self._host_ok():
                self._json(403, {"error": "Host が違います"})
                return False
            if not self._authed():
                self._json(401, {"error": "認証が必要です（起動時にターミナルに表示した URL を開いてください）"})
                return False
            if write:
                origin = self.headers.get("Origin")
                if self.headers.get(CSRF_HEADER) != "1" or (origin and origin not in allowed_origins):
                    self._json(403, {"error": "操作の要求の形が違います"})
                    return False
            return True

        def do_GET(self):
            url = urlparse(self.path)
            if url.path == "/" and "token" in parse_qs(url.query):
                if not self._host_ok():
                    return self._json(403, {"error": "Host が違います"})
                given = parse_qs(url.query)["token"][0]
                if not hmac.compare_digest(given.encode(), token.encode()):
                    return self._json(401, {"error": "トークンが違います"})
                return self._send(303, b"", "text/plain", {
                    "Location": "/", "Set-Cookie": f"{COOKIE}={token}; HttpOnly; SameSite=Strict; Path=/"})
            if not self._guard():
                return
            if url.path == "/":
                return self._send(200, PAGE.encode("utf-8"), "text/html; charset=utf-8")
            if url.path == "/api/worktrees":
                return self._json(*controller.worktrees("refresh" in parse_qs(url.query)))
            if url.path == "/api/tasks":
                tasks, done = startable_tasks()
                return self._json(200, {"tasks": list(tasks.values()), "completed": done})
            if url.path == "/api/runs":
                return self._json(200, {"runs": list_runs(), "running": controller.running()})
            m = re.fullmatch(r"/api/runs/([^/]+)/([^/]+)", url.path)
            if m:
                detail = run_detail(m.group(1), m.group(2))
                return self._json(200, detail) if detail else self._json(404, {"error": "記録がありません"})
            self._json(404, {"error": "ありません"})

        def do_POST(self):
            if not self._guard(write=True):
                return
            length = min(int(self.headers.get("Content-Length") or 0), 10_000)
            try:
                body = json.loads(self.rfile.read(length) or b"{}")
            except ValueError:
                return self._json(400, {"error": "JSON ではありません"})
            if self.path == "/api/start":
                return self._json(*controller.start(str(body.get("task", "")), bool(body.get("pr"))))
            if self.path == "/api/stop":
                return self._json(*controller.stop())
            if self.path == "/api/cleanup":
                return self._json(*controller.cleanup(str(body.get("task_id", ""))))
            self._json(404, {"error": "ありません"})

    return Handler


def make_server(port: int = DEFAULT_PORT, token: str | None = None) -> tuple[ThreadingHTTPServer, str]:
    token = token or secrets.token_urlsafe(32)
    server = ThreadingHTTPServer((HOST, port), None)
    server.RequestHandlerClass = make_handler(token, server.server_address[1], Controller())
    return server, token


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="ACC の開発オーケストレーターの操作画面（127.0.0.1 だけ）")
    parser.add_argument("--port", type=int, default=DEFAULT_PORT)
    args = parser.parse_args(argv)
    if not orchestrator().exists():
        print(f"オーケストレーターがありません: {orchestrator()}", file=sys.stderr)
        return 1
    server, token = make_server(args.port)
    port = server.server_address[1]
    print(f"開発オーケストレーターの操作画面: http://{HOST}:{port}/?token={token}", flush=True)
    print("（この URL は他人に渡さない。Ctrl-C で終了。実行中の run は止まらない。止めるときは画面の「停止」）", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
    return 0


PAGE = """<!doctype html>
<html lang="ja"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>開発オーケストレーター</title>
<style>
:root { --bg:#f8fafc; --fg:#0f172a; --muted:#64748b; --card:#fff; --line:#e2e8f0;
        --ok:#16a34a; --ng:#dc2626; --warn:#d97706; --run:#2563eb; }
@media (prefers-color-scheme: dark) { :root { --bg:#0b1120; --fg:#e2e8f0; --muted:#94a3b8; --card:#111827; --line:#1f2937; } }
* { box-sizing:border-box; }
body { margin:0; font-family:-apple-system, "Hiragino Sans", "Noto Sans JP", sans-serif; background:var(--bg); color:var(--fg); }
header { padding:16px 24px; border-bottom:1px solid var(--line); }
header h1 { margin:0; font-size:19px; display:flex; align-items:center; gap:10px; }
#help-open { width:28px; height:28px; padding:0; border-radius:50%; font-weight:bold; }
dialog { max-width:760px; width:calc(100% - 32px); max-height:85vh; overflow:auto; border:1px solid var(--line);
         border-radius:12px; background:var(--card); color:var(--fg); padding:16px 20px; }
dialog::backdrop { background:rgba(0,0,0,.45); }
dialog h2 { margin-top:0; } dialog h3 { font-size:15px; margin:18px 0 6px; }
dialog p, dialog li, dialog td, dialog th { font-size:14px; line-height:1.6; }
.dialog-head { display:flex; justify-content:space-between; align-items:center; position:sticky; top:-16px; background:var(--card); padding-top:4px; }
main { padding:16px 24px 48px; max-width:1100px; margin:0 auto; }
section { background:var(--card); border:1px solid var(--line); border-radius:10px; padding:12px 14px; margin-bottom:16px; }
h2 { font-size:16px; margin:0 0 10px; }
.muted { color:var(--muted); font-size:13px; }
table { border-collapse:collapse; width:100%; font-size:13px; }
td, th { border-bottom:1px solid var(--line); padding:6px 8px; text-align:left; vertical-align:top; }
tr.sel { background:rgba(37,99,235,.08); }
.st { font-size:11px; padding:1px 7px; border-radius:999px; border:1px solid currentColor; white-space:nowrap; }
.PASS { color:var(--ok); } .BLOCKED, .interrupted { color:var(--ng); } .CHANGES_REQUESTED, .STOPPED { color:var(--warn); }
.running { color:var(--run); }
button { font:inherit; padding:4px 12px; border-radius:6px; border:1px solid var(--line); background:var(--card); color:var(--fg); cursor:pointer; }
button.danger { color:var(--ng); border-color:var(--ng); }
button:disabled { opacity:.35; cursor:not-allowed; }
select { font:inherit; padding:4px; max-width:100%; }
pre { background:var(--bg); border:1px solid var(--line); border-radius:6px; padding:8px; overflow:auto; font-size:12px; max-height:320px; white-space:pre-wrap; }
#msg { min-height:1.4em; }
@media (max-width:600px) { main, header { padding-left:16px; padding-right:16px; } }
</style></head><body>
<header><h1>開発オーケストレーター <button id="help-open" title="使い方" aria-label="使い方を表示">?</button></h1>
<div class="muted">Claude Code が実装し、Codex が読み取り専用でレビューする（最大 3 回）。main へのマージは Owner が GitHub で行う。</div></header>
<dialog id="help" aria-labelledby="help-title">
<div class="dialog-head"><h2 id="help-title">使い方</h2><button id="help-close" aria-label="閉じる">閉じる</button></div>
<p>指示書を選んで開始し、終わったら PR を見てマージを判断する画面です。指示書を書くところだけは、画面の外で行います。</p>
<h3>1. 指示書を書く（画面の外）</h3>
<p>統合ワークスペースの <code>docs/codex/</code> に Markdown のファイルを作り、先頭に次の 5 行を書きます。</p>
<pre>---
task_id: T-1400
repo: 1003_Jarvis_claude
base: main
setup: uv venv -q -p 3.13 .venv &amp;&amp; uv pip install -q -p .venv/bin/python -r requirements.txt -r requirements-dev.txt
test: .venv/bin/python -m pytest tests -q
---
# T-1400 （やりたいことの題名）

## 目的
## 仕様（何をどう変えるか。できるだけ具体的に）
## テスト（何を確かめるテストを書くか）
## 制約（変更してよいファイル、変えてはいけないこと）</pre>
<p>見本は <code>docs/codex/T-1304_knowledge_audit_json.md</code>。Codex は「仕様と制約を満たしているか」でレビューするため、具体的に書くほど判定の精度が上がります。Claude Code に「〜の指示書を作って」と頼むこともできます。</p>
<h3>2. 指示書をコミットする</h3>
<p>一度 PASS した指示書と、マージして後片付けした指示書（完了済み）は開始のリストに出ません。やり直すときは新しい task_id の指示書を作ります。</p>
<p>指示書の <code>setup</code>・<code>test</code> はシェルで実行されるため、画面に出るのは<strong>コミット済みで、その後に変更していない</strong>指示書だけです。</p>
<h3>3. 画面で開始する</h3>
<p>指示書を選ぶ → 必要なら「PASS なら PR を作る」にチェック → 「開始」→ 確認で OK。Claude・Codex の利用枠を使います。05:30〜07:30 は開始できません（理由が出ます）。</p>
<h3>4. 進捗を見る</h3>
<ul>
<li>一覧は 5 秒ごとに更新されます。</li>
<li><strong>段階</strong>: claude（実装中）→ test → codex（レビュー中）→ finished。</li>
<li><strong>回（指摘の数）</strong>: 何回目のレビューか、各回の指摘が何件か。</li>
<li>タスク名をクリックすると、各回の Codex の指摘・テストのログ・差分の統計が見られます。</li>
<li>途中でやめるときは「実行中の run を停止」。Claude・Codex も止まり、記録は STOPPED で閉じます。</li>
</ul>
<h3>5. 結果を見て判断する</h3>
<table>
<tr><th>状態</th><th>意味</th><th>次にすること</th></tr>
<tr><td><span class="st PASS">PASS</span></td><td>レビューに合格</td><td>PR のリンクから差分を確認し、問題なければ GitHub でマージ</td></tr>
<tr><td><span class="st CHANGES_REQUESTED">CHANGES_REQUESTED</span></td><td>3 回直しても合格しなかった</td><td>詳細の指摘を読み、指示書を直して新しい ID で開始し直す</td></tr>
<tr><td><span class="st BLOCKED">BLOCKED</span></td><td>利用枠の上限・認証・エラーなどで停止</td><td>詳細の「理由」を確認する</td></tr>
<tr><td><span class="st STOPPED">STOPPED</span></td><td>自分で止めた</td><td>—</td></tr>
</table>
<p>マージは自動では行われません。マージ後の本番への反映は Claude Code に依頼します。</p>
<h3>6. 後片付け（マージの後）</h3>
<p>PR を GitHub でマージしたら、一覧の「作業フォルダを削除」を押します。押せるのは、作業ブランチが main にマージ済みで、作業フォルダに未コミットの変更がなく、実行中でないときだけです（押せない理由はボタンの下に出ます）。マージの判定は 60 秒ごとに更新されます。消えるのは作業フォルダとローカルの作業ブランチで、実行の記録・GitHub のブランチと PR は残ります。後片付けしたタスクの run は一覧から外れ、下の「履歴（後片付け済み）」に移ります（同じ ID で新しく開始した run は一覧に出ます）。</p>
<h3>注意</h3>
<ul>
<li><strong>同じ ID は使い回せません</strong>。一度実行した ID で再び開始すると、前回の作業フォルダが残っているため BLOCKED になります。やり直すときは ID を変えます（例: T-1400 → T-1400b）。</li>
<li>同時に動かせるのは <strong>1 件だけ</strong>です。</li>
<li>ブラウザやターミナルを閉じても実行中の run は止まりません。止めるときは画面の「停止」を使います。</li>
</ul>
</dialog>
<main>
<section><h2>開始</h2>
<select id="task"></select> <label><input type="checkbox" id="pr"> PASS なら PR を作る（マージはしない）</label>
<button id="start">開始</button> <button id="stop" class="danger">実行中の run を停止</button>
<div id="done" class="muted"></div>
<div id="msg" class="muted"></div></section>
<section><h2>実行の一覧</h2><table><thead><tr><th>タスク</th><th>状態</th><th>段階</th><th>回（指摘の数）</th><th>開始</th><th>PR</th><th>作業フォルダ</th></tr></thead>
<tbody id="runs"></tbody></table>
<details id="history-box"><summary>履歴（後片付け済み） <span id="history-count">0</span> 件</summary>
<p class="muted">作業フォルダを削除したタスクの run。実行の記録は残っており、クリックで詳細を見られる。</p>
<table><thead><tr><th>タスク</th><th>状態</th><th>段階</th><th>回（指摘の数）</th><th>開始</th><th>PR</th></tr></thead>
<tbody id="history"></tbody></table></details></section>
<section id="detail" hidden><h2 id="dtitle"></h2><div id="dbody"></div></section>
</main>
<script>
const H = {"Content-Type": "application/json", "X-Jarvis-Console": "1"};
let selected = null;
const esc = s => String(s ?? "").replace(/[&<>"']/g, c => ({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"}[c]));
const badge = s => `<span class="st ${esc(s)}">${esc(s)}</span>`;
const link = u => u && /^https:\\/\\/github\\.com\\//.test(u) ? `<a href="${esc(u)}" target="_blank" rel="noopener">${esc(u.split("/").slice(-3).join("/"))}</a>` : "";
async function api(path, opts) { const r = await fetch(path, opts); return [r.status, await r.json()]; }
let worktrees = [];
function wtCell(r) {
  const w = worktrees.find(w => w.task_id === r.task_id && w.run === r.run);
  if (!w) return "";
  return `<button class="cleanup" data-task="${esc(w.task_id)}" ${w.removable ? "" : "disabled"} title="${esc(w.removable ? "マージ済み。作業フォルダとローカルのブランチを消す" : w.reason)}">作業フォルダを削除</button>` +
    (w.removable ? "" : `<div class="muted">${esc(w.reason)}</div>`);
}
async function loadWorktrees(refresh) {
  const [code, d] = await api("/api/worktrees" + (refresh ? "?refresh=1" : ""));
  if (code === 200) { worktrees = d.worktrees; loadRuns(); }
}
async function loadTasks() {
  const [, d] = await api("/api/tasks");
  document.getElementById("task").innerHTML = d.tasks.map(t => `<option value="${esc(t.name)}">${esc(t.task_id)}（${esc(t.repo)}）${esc(t.name)}</option>`).join("") ||
    '<option value="">開始できる指示書はありません（新しい指示書を docs/codex/ にコミットする）</option>';
  document.getElementById("done").textContent = d.completed.length ?
    `完了済み（PASS 済み・マージして後片付け済み）で外した指示書: ${d.completed.length} 件（${d.completed.map(t => t.task_id).join("、")}）` : "";
}
async function loadRuns() {
  const [code, d] = await api("/api/runs");
  if (code !== 200) { document.getElementById("msg").textContent = d.error; return; }
  const row = (r, wt) => `<tr data-k="${esc(r.task_id)}/${esc(r.run)}" class="${selected === r.task_id + "/" + r.run ? "sel" : ""}">
    <td><a href="#" data-k="${esc(r.task_id)}/${esc(r.run)}">${esc(r.task_id)}</a><div class="muted">${esc(r.run)}</div></td>
    <td>${badge(r.status)}${r.reason ? `<div class="muted">${esc(r.reason)}</div>` : ""}</td><td>${esc(r.stage)}</td>
    <td>${esc(r.cycles)} / ${esc(r.max_cycles)}（${esc(r.findings.join("・"))}）</td><td>${esc(r.started_at)}</td><td>${link(r.pr_url)}</td>${wt ? `<td>${wtCell(r)}</td>` : ""}</tr>`;
  const active = d.runs.filter(r => !r.archived), archived = d.runs.filter(r => r.archived);
  document.getElementById("runs").innerHTML = active.map(r => row(r, true)).join("") ||
    '<tr><td colspan="7" class="muted">実行中・後片付け前の run はありません。</td></tr>';
  document.getElementById("history").innerHTML = archived.map(r => row(r, false)).join("");
  document.getElementById("history-count").textContent = archived.length;
  document.getElementById("stop").disabled = !d.running;
  document.getElementById("start").disabled = d.running;
  if (selected) loadDetail(selected);
}
async function loadDetail(k) {
  const [code, d] = await api("/api/runs/" + k);
  if (code !== 200) return;
  document.getElementById("detail").hidden = false;
  document.getElementById("dtitle").innerHTML = `${esc(d.summary.task_id)} ${badge(d.summary.status)} ${link(d.summary.pr_url)}`;
  const auth = Object.entries(d.auth).map(([k, v]) => `${esc(k)}: ${v.ok ? "OK" : "NG"} ${esc(v.detail)}`).join(" ／ ");
  document.getElementById("dbody").innerHTML = `<div class="muted">認証: ${auth || "-"}</div>` + d.cycles.map(c => `
    <h3>${esc(c.cycle)} 回目 ${badge(c.verdict || "-")}</h3>
    <div class="muted">Claude: ${esc((c.claude.models || []).join(", "))}・${esc(c.claude.seconds)} 秒 ／ テスト: 終了コード ${esc(c.test?.returncode)} ／ Codex: ${esc(c.codex?.model)}</div>
    ${c.review ? `<p>${esc(c.review.summary)}</p><ul>${c.review.findings.map(f => `<li>[${esc(f.severity)}] ${esc(f.file)}${f.line ? ":" + esc(f.line) : ""} ${esc(f.message)}</li>`).join("")}</ul>` : ""}
    <details><summary>差分の統計</summary><pre>${esc(c.diffstat)}</pre></details>
    <details><summary>テストのログ（末尾）</summary><pre>${esc(c.test_log)}</pre></details>`).join("") +
    `<details><summary>進捗の記録（events）</summary><pre>${esc(d.events.join("\\n"))}</pre></details>`;
}
document.getElementById("history").addEventListener("click", e => {
  const k = e.target.closest("[data-k]")?.dataset.k; if (!k) return; e.preventDefault(); selected = k; loadRuns();
});
document.getElementById("runs").addEventListener("click", async e => {
  const b = e.target.closest("button.cleanup");
  if (b) {
    if (b.disabled || !confirm(b.dataset.task + " の作業フォルダを削除しますか（マージ済み。ローカルの作業ブランチも消します。実行の記録・GitHub のブランチと PR は残ります）")) return;
    document.getElementById("msg").textContent = "削除しています…";
    const [, d] = await api("/api/cleanup", {method: "POST", headers: H, body: JSON.stringify({task_id: b.dataset.task})});
    document.getElementById("msg").textContent = d.message || d.error;
    loadWorktrees(true); return;
  }
  const k = e.target.closest("[data-k]")?.dataset.k; if (!k) return; e.preventDefault(); selected = k; loadRuns();
});
document.getElementById("start").onclick = async () => {
  const task = document.getElementById("task").value;
  if (!task || !confirm(task + " を開始しますか（Claude・Codex の利用枠を使います）")) return;
  const [code, d] = await api("/api/start", {method: "POST", headers: H, body: JSON.stringify({task, pr: document.getElementById("pr").checked})});
  document.getElementById("msg").textContent = code === 202 ? `開始しました: ${d.started}` : d.error;
  loadRuns();
};
document.getElementById("stop").onclick = async () => {
  if (!confirm("実行中の run を停止しますか（Claude・Codex・テストのプロセスも止めます）")) return;
  document.getElementById("msg").textContent = "停止しています…";
  const [, d] = await api("/api/stop", {method: "POST", headers: H, body: "{}"});
  document.getElementById("msg").textContent = d.message || d.error; loadRuns();
};
const help = document.getElementById("help");
document.getElementById("help-open").onclick = () => help.showModal();
document.getElementById("help-close").onclick = () => help.close();
help.addEventListener("click", e => { if (e.target === help) help.close(); });   // 外側をクリックで閉じる
loadTasks(); loadRuns(); loadWorktrees(false); setInterval(loadRuns, 5000); setInterval(() => loadWorktrees(true), 60000);
</script></body></html>
"""

if __name__ == "__main__":
    sys.exit(main())
