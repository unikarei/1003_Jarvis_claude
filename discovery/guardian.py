"""Architecture Guardian: Registry（設計上の意図）と Discovery の結果（実態）を比べる（../docs/architecture.md §11.2）。

各 Resource を Verified（検査日時付き）/ Failed（理由付き）/ Unknown に判定し、問題（finding）を重要度順に返す。
Registry の YAML は書き換えない（判定結果は discovered/・generated/ にだけ出す）。
"""

from dataclasses import dataclass, field

SEVERITY_ORDER = {"error": 0, "warning": 1, "info": 2}
UNREGISTERED_LABEL = {"repository": "リポジトリ", "vault": " Vault", "launchd": " launchd ジョブ", "docker": " Docker コンテナ"}


@dataclass
class Finding:
    severity: str          # error / warning / info
    kind: str              # missing / stopped / unreachable / sync / backup / dependency / unregistered / ...
    resource_id: str | None
    message: str


@dataclass
class Judgement:
    state: str             # Verified / Failed / Unknown
    reason: str
    checked_at: str | None


@dataclass
class Report:
    checked_at: str
    judgements: dict[str, Judgement] = field(default_factory=dict)
    findings: list[Finding] = field(default_factory=list)
    out_of_scope: dict[str, list[str]] = field(default_factory=dict)


def _judge_repository(rid: str, res: dict, facts: dict, out: list[Finding]) -> tuple[str, str]:
    if "skipped" in facts:
        return "Unknown", facts["skipped"]
    if not facts.get("exists"):
        out.append(Finding("error", "missing", rid, f"ローカルにない: {facts.get('path')}"))
        return "Failed", "ローカルにない"
    if facts.get("git_busy"):
        out.append(Finding("error", "sync", rid, f"Git の操作が途中: {', '.join(facts['git_busy'])}"))
        return "Failed", "Git の操作が途中"
    if res.get("branch") and facts.get("branch") != res["branch"]:
        out.append(Finding("warning", "sync", rid,
                           f"ブランチが {facts.get('branch')}（Registry では {res['branch']}）"))
    if facts.get("in_sync_with_remote") is False:
        out.append(Finding("warning", "sync", rid, "リモートと一致していない（未 push または未 pull）"))
    if facts.get("uncommitted"):
        level = "warning" if res.get("resource_type") == "vault" else "info"
        out.append(Finding(level, "sync", rid, f"未コミットの変更 {facts['uncommitted']} 件"))
    if facts.get("open_prs"):
        out.append(Finding("info", "pull_request", rid, f"未マージの PR {facts['open_prs']} 件"))
    if res.get("resource_type") == "vault" and facts.get("registered_in_obsidian") is False:
        out.append(Finding("warning", "obsidian", rid, "Obsidian の Vault 一覧に登録されていない"))
    return "Verified", "ローカルにあり、Git の操作中ではない"


def _judge_launchd(rid: str, res: dict, facts: dict, out: list[Finding]) -> tuple[str, str]:
    if not facts.get("loaded"):
        out.append(Finding("error", "stopped", rid, f"launchd に登録されていない: {facts.get('label')}"))
        return "Failed", "launchd に登録されていない"
    if res.get("resource_type") == "service":
        if not facts.get("running"):
            out.append(Finding("error", "stopped", rid, "常駐サービスが動いていない"))
            return "Failed", "動いていない"
        return "Verified", "稼働中"
    if facts.get("last_exit") not in (0, None):
        out.append(Finding("error", "stopped", rid, f"最後の実行が終了コード {facts['last_exit']} で終わった"))
        return "Failed", f"終了コード {facts['last_exit']}"
    return "Verified", "登録済み、最後の実行は正常（未実行を含む）"


def _judge_http(rid: str, res: dict, facts: dict, out: list[Finding]) -> tuple[str, str]:
    if "skipped" in facts:
        return "Unknown", facts["skipped"]
    if "error" in facts:
        out.append(Finding("error", "unreachable", rid, f"応答なし（{facts['error']}）: {facts.get('url')}"))
        return "Failed", "応答なし"
    code = facts.get("status_code")
    if code is None or code >= 400:
        out.append(Finding("error", "unreachable", rid, f"HTTP {code}: {facts.get('url')}"))
        return "Failed", f"HTTP {code}"
    return "Verified", f"HTTP {code}（{facts.get('elapsed_ms')} ms）"


def _judge_data(rid: str, res: dict, facts: dict, out: list[Finding]) -> tuple[str, str]:
    if "skipped" in facts:
        return "Unknown", facts["skipped"]
    if res.get("backup") == "none":
        out.append(Finding("warning", "backup", rid, "バックアップがない"))
    if not facts.get("exists"):
        level = "info" if res.get("format") == "files" else "error"  # 使う前は作られていないデータもある
        out.append(Finding(level, "missing", rid, f"存在しない: {facts.get('path')}"))
        return ("Unknown", "まだ作られていない") if level == "info" else ("Failed", "存在しない")
    return "Verified", "存在する"


def _judge_host(rid: str, res: dict, facts: dict, out: list[Finding]) -> tuple[str, str]:
    if "skipped" in facts:
        return "Unknown", facts["skipped"]
    free = facts.get("disk_free_gb")
    if free is not None and free < 50:
        out.append(Finding("warning", "capacity", rid, f"ディスクの空きが {free} GB"))
    return "Verified", f"この端末（空き {free} GB）"


JUDGES = {"repository": _judge_repository, "vault": _judge_repository, "service": _judge_launchd,
          "scheduled_job": _judge_launchd, "website": _judge_http, "api": _judge_http,
          "data_store": _judge_data, "host": _judge_host}


def evaluate(registry: dict, snapshot: dict) -> Report:
    report = Report(checked_at=snapshot["checked_at"], out_of_scope=snapshot.get("out_of_scope", {}))
    resources = registry["resources"]
    for rid, res in sorted(resources.items()):
        entry = snapshot["resources"].get(rid)
        if entry is None:
            report.judgements[rid] = Judgement("Unknown", "Discovery の結果がない", None)
            continue
        facts = entry["facts"]
        if "error" in facts and res.get("resource_type") not in ("website", "api"):
            report.findings.append(Finding("warning", "discovery", rid, f"調べられなかった: {facts['error']}"))
            report.judgements[rid] = Judgement("Unknown", "調べられなかった", entry["checked_at"])
            continue
        judge = JUDGES.get(res.get("resource_type"))
        state, reason = judge(rid, res, facts, report.findings) if judge else ("Unknown", facts.get("skipped", "未検査"))
        report.judgements[rid] = Judgement(state, reason, entry["checked_at"])

    # 依存関係の不整合: 動いているものが、Failed のものに依存している
    for rid, res in sorted(resources.items()):
        if report.judgements[rid].state != "Verified":
            continue
        for dep in res.get("depends_on") or []:
            if report.judgements.get(dep) and report.judgements[dep].state == "Failed":
                report.findings.append(Finding("warning", "dependency", rid, f"依存先 {dep} が Failed"))

    for item in snapshot.get("unregistered", []):
        where = item.get("path") or item.get("label") or item.get("name")
        kind = UNREGISTERED_LABEL.get(item["kind"], item["kind"])
        report.findings.append(Finding("warning", "unregistered", None, f"Registry に未登録の{kind}: {where}"))

    report.findings.sort(key=lambda f: (SEVERITY_ORDER[f.severity], f.kind, f.resource_id or ""))
    return report
