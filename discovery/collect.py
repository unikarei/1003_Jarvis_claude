"""Discovery: Registry の Resource ごとに実際の状態を集める（../docs/architecture.md §11.1）。

すべて読み取りだけ。git は --no-optional-locks で実行し、fetch もしない（Obsidian Git の操作を妨げない）。
1つの Resource の収集で例外が起きても、その Resource に error を記録して全体は続ける。
"""

import json
import os
import platform
import shutil
import subprocess
from datetime import datetime
from pathlib import Path

import requests

import config
from discovery import settings


def run(cmd: list[str], cwd: Path | None = None, timeout: int = 30) -> subprocess.CompletedProcess:
    """外部コマンドを実行する（テストではこの関数を差し替える）。"""
    return subprocess.run(cmd, cwd=cwd, capture_output=True, text=True, timeout=timeout)


def now_iso() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


def expand(path: str) -> Path:
    return Path(os.path.expanduser(path))


def locator_for_host(resource: dict) -> str | None:
    loc = resource.get("locator")
    if isinstance(loc, dict):
        value = loc.get(config.HOST_ID)
        return None if value in (None, "unknown") else value
    return loc


def _git(path: Path, *args: str) -> str | None:
    out = run(["git", "--no-optional-locks", *args], cwd=path)
    return out.stdout.strip() if out.returncode == 0 else None


# ---------------------------------------------------------------- 種類ごとの収集

def collect_repository(resource: dict) -> dict:
    loc = locator_for_host(resource)
    if not loc:
        return {"skipped": "この端末の場所が Registry にない"}
    path = expand(loc)
    if not (path / ".git").exists():
        return {"exists": False, "path": str(path)}
    git_dir = Path(_git(path, "rev-parse", "--absolute-git-dir") or path / ".git")
    facts = {
        "exists": True,
        "path": str(path),
        "branch": _git(path, "rev-parse", "--abbrev-ref", "HEAD"),
        "uncommitted": len([line for line in (_git(path, "status", "--porcelain") or "").splitlines() if line]),
        "last_commit_at": _git(path, "log", "-1", "--format=%cI"),
        "git_busy": sorted(m for m in ("MERGE_HEAD", "rebase-merge", "rebase-apply", "index.lock")
                           if (git_dir / m).exists()),
    }
    remote = resource.get("git_remote")
    if remote and remote != "local-only":
        local = _git(path, "rev-parse", "HEAD")
        out = run(["git", "ls-remote", remote, f"refs/heads/{resource.get('branch', 'main')}"], cwd=path)
        remote_sha = out.stdout.split()[0] if out.returncode == 0 and out.stdout.strip() else None
        facts["remote_head"] = remote_sha
        facts["in_sync_with_remote"] = (remote_sha == local) if remote_sha else None
        if "github.com/" in remote:
            repo = remote.split("github.com/", 1)[1].removesuffix(".git")
            prs = run(["gh", "pr", "list", "--repo", repo, "--state", "open", "--json", "number", "-q", "length"])
            facts["open_prs"] = int(prs.stdout.strip()) if prs.returncode == 0 and prs.stdout.strip().isdigit() else None
    return facts


def obsidian_vault_paths() -> set[str]:
    try:
        data = json.loads(settings.OBSIDIAN_CONFIG.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return set()
    return {str(Path(v["path"]).resolve()) for v in data.get("vaults", {}).values() if "path" in v}


def collect_vault(resource: dict) -> dict:
    facts = collect_repository(resource)
    if facts.get("exists"):
        facts["registered_in_obsidian"] = str(Path(facts["path"]).resolve()) in obsidian_vault_paths()
    return facts


def launchd_jobs() -> dict[str, dict]:
    """`launchctl list` の結果: ラベル → {pid, last_exit}。"""
    out = run(["launchctl", "list"])
    jobs = {}
    for line in out.stdout.splitlines()[1:]:
        parts = line.split("\t")
        if len(parts) != 3:
            continue
        pid, status, label = parts
        jobs[label] = {"pid": None if pid == "-" else int(pid),
                       "last_exit": int(status) if status.lstrip("-").isdigit() else None}
    return jobs


def collect_launchd(resource: dict, jobs: dict[str, dict]) -> dict:
    label = resource.get("launchd_label")
    job = jobs.get(label)
    if job is None:
        return {"loaded": False, "label": label}
    return {"loaded": True, "label": label, "running": job["pid"] is not None, "last_exit": job["last_exit"]}


def collect_http(resource: dict) -> dict:
    hc = resource.get("healthcheck") or {}
    url = hc.get("target") if hc.get("type") == "http" else (resource.get("url") or resource.get("base_url"))
    if not url:
        return {"skipped": "検査する URL がない"}
    try:
        res = requests.get(url, timeout=settings.HTTP_TIMEOUT_SECONDS, allow_redirects=True)
    except requests.RequestException as e:
        return {"url": url, "error": type(e).__name__}
    return {"url": url, "status_code": res.status_code, "elapsed_ms": int(res.elapsed.total_seconds() * 1000)}


def collect_data_store(resource: dict) -> dict:
    loc = locator_for_host(resource)
    if not loc:
        return {"skipped": "この端末の場所が Registry にない"}
    path = expand(loc)
    if not path.exists():
        return {"exists": False, "path": str(path)}
    stat = path.stat()
    size = stat.st_size if path.is_file() else sum(p.stat().st_size for p in path.rglob("*") if p.is_file())
    return {"exists": True, "path": str(path), "size_bytes": size,
            "modified_at": datetime.fromtimestamp(stat.st_mtime).astimezone().isoformat(timespec="seconds")}


def collect_host(resource: dict) -> dict:
    if resource["resource_id"] != config.HOST_ID:
        return {"skipped": "この端末からは調べられない"}
    usage = shutil.disk_usage(Path.home())
    return {"this_host": True, "platform": platform.platform(), "disk_free_gb": round(usage.free / 1e9, 1)}


# ---------------------------------------------------------------- 未登録の探索

def find_unregistered(resources: dict[str, dict], jobs: dict[str, dict]) -> tuple[list[dict], dict[str, list[str]]]:
    """Registry にない資産と、対象外として数える資産。"""
    registered_paths = set()
    for r in resources.values():
        loc = locator_for_host(r)
        if loc:
            registered_paths.add(str(expand(loc).resolve()))
    labels = {r.get("launchd_label") for r in resources.values() if r.get("launchd_label")}
    unregistered, out_of_scope = [], {"launchd": [], "docker": []}

    for parent, kind in ((settings.WORKSPACE_DIR, "repository"), (settings.VAULT_PARENT, "vault")):
        if not parent.is_dir():
            continue
        for child in sorted(parent.iterdir()):
            if child.name in settings.IGNORED_WORKSPACE_DIRS or not (child / ".git").exists():
                continue
            if str(child.resolve()) not in registered_paths:
                unregistered.append({"kind": kind, "path": str(child)})

    for label in sorted(jobs):
        if not label.startswith(settings.LAUNCHD_PREFIX) or label in labels:
            continue
        if label in settings.OUT_OF_SCOPE_LAUNCHD:
            out_of_scope["launchd"].append(label)
        else:
            unregistered.append({"kind": "launchd", "label": label})

    docker = run(["docker", "ps", "--format", "{{.Names}}"])
    if docker.returncode == 0:
        services = {r.get("docker_service") for r in resources.values() if r.get("docker_service")}
        for name in sorted(docker.stdout.split()):
            if name in services:
                continue
            if name.startswith(settings.OUT_OF_SCOPE_DOCKER_PREFIXES):
                out_of_scope["docker"].append(name)
            else:
                unregistered.append({"kind": "docker", "name": name})
    return unregistered, out_of_scope


# ---------------------------------------------------------------- 全体

def collect(registry: dict) -> dict:
    """Registry（registry.json の内容）の全 Resource を調べたスナップショットを返す。"""
    resources = registry["resources"]
    jobs = launchd_jobs()
    results = {}
    for rid, r in sorted(resources.items()):
        kind = r.get("resource_type")
        try:
            if kind == "repository":
                facts = collect_repository(r)
            elif kind == "vault":
                facts = collect_vault(r)
            elif kind in ("service", "scheduled_job"):
                facts = collect_launchd(r, jobs)
            elif kind in ("website", "api"):
                facts = collect_http(r)
            elif kind == "data_store":
                facts = collect_data_store(r)
            elif kind == "host":
                facts = collect_host(r)
            else:
                facts = {"skipped": "この種類は Phase 3 では調べない"}
        except Exception as e:  # noqa: BLE001 1件の失敗で全体を止めない
            facts = {"error": f"{type(e).__name__}: {e}"[:200]}
        results[rid] = {"resource_type": kind, "checked_at": now_iso(), "facts": facts}
    unregistered, out_of_scope = find_unregistered(resources, jobs)
    return {"checked_at": now_iso(), "host": config.HOST_ID, "resources": results,
            "unregistered": unregistered, "out_of_scope": out_of_scope}
