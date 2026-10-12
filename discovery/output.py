"""Discovery / Guardian の出力（../docs/architecture.md §11.3）。Registry リポジトリの discovered/・generated/ に書く。"""

import json
import re
from pathlib import Path

from discovery.guardian import Report

OUTPUT_FILES = ("discovered/latest.json", "generated/guardian-report.md",
                "generated/architecture.mmd", "generated/STATUS.md")
SEVERITY_LABEL = {"error": "エラー", "warning": "警告", "info": "情報"}
TYPE_LABEL = {"host": "端末", "repository": "リポジトリ", "vault": "Vault", "module": "モジュール",
              "service": "サービス", "scheduled_job": "定期ジョブ", "data_store": "データ", "website": "Web",
              "api": "API", "external_service": "外部サービス", "docker_service": "Docker"}


def _node_id(resource_id: str) -> str:
    return re.sub(r"[^A-Za-z0-9_]", "_", resource_id)


def mermaid(registry: dict, report: Report) -> str:
    """状態で色分けした全体構成図（flowchart）。端末ごとにまとめ、端末のないものは「外部」にまとめる。"""
    resources = registry["resources"]
    groups: dict[str, list[str]] = {}
    for rid, r in sorted(resources.items()):
        if r.get("resource_type") == "host":
            continue
        groups.setdefault(r.get("host") or "external", []).append(rid)
    lines = ["flowchart LR"]
    for group, rids in sorted(groups.items()):
        title = group if group != "external" else "外部・Web"
        lines.append(f'  subgraph {_node_id(group)}["{title}"]')
        for rid in rids:
            label = f"{rid}<br/>{TYPE_LABEL.get(resources[rid].get('resource_type'), '')}"
            lines.append(f'    {_node_id(rid)}["{label}"]')
        lines.append("  end")
    for rid, r in sorted(resources.items()):
        if r.get("resource_type") == "host":
            continue
        for dep in r.get("depends_on") or []:
            if dep in resources and resources[dep].get("resource_type") != "host":
                lines.append(f"  {_node_id(rid)} --> {_node_id(dep)}")
        for dst in r.get("publishes_to") or []:
            if dst in resources:
                lines.append(f"  {_node_id(rid)} -.->|publish| {_node_id(dst)}")
    lines += ["  classDef verified fill:#dcfce7,stroke:#16a34a,color:#14532d",
              "  classDef failed fill:#fee2e2,stroke:#dc2626,color:#7f1d1d",
              "  classDef unknown fill:#f3f4f6,stroke:#9ca3af,color:#374151"]
    for state in ("Verified", "Failed", "Unknown"):
        ids = [_node_id(rid) for rid, j in sorted(report.judgements.items())
               if j.state == state and resources.get(rid, {}).get("resource_type") != "host"]
        if ids:
            lines.append(f"  class {','.join(ids)} {state.lower()}")
    return "\n".join(lines) + "\n"


def guardian_report(report: Report) -> str:
    counts = {s: sum(1 for f in report.findings if f.severity == s) for s in SEVERITY_LABEL}
    out = ["# Guardian レポート", "",
           "> 1003 の `discover.py` が生成する。手で編集しない。Registry（設計上の意図）と実際の状態の差分。", "",
           f"- 検査日時: {report.checked_at}",
           f"- エラー {counts['error']} 件・警告 {counts['warning']} 件・情報 {counts['info']} 件", ""]
    for severity, label in SEVERITY_LABEL.items():
        items = [f for f in report.findings if f.severity == severity]
        if not items:
            continue
        out += [f"## {label}", ""]
        out += [f"- `{f.resource_id or '-'}`（{f.kind}）: {f.message}" for f in items]
        out.append("")
    oos = {k: v for k, v in report.out_of_scope.items() if v}
    if oos:
        out += ["## 対象外（ワークスペースの外。登録しない）", ""]
        out += [f"- {kind}: {len(v)} 件（{', '.join(v)}）" for kind, v in sorted(oos.items())]
        out.append("")
    return "\n".join(out).rstrip() + "\n"


def status_md(registry: dict, report: Report, diagram: str) -> str:
    resources = registry["resources"]
    states = [j.state for j in report.judgements.values()]
    out = ["# STATUS（Architecture Registry の実態）", "",
           "> 1003 の `discover.py` が生成する。手で編集しない。", "",
           f"- 最終確認日時: {report.checked_at}",
           f"- Verified {states.count('Verified')} 件・Failed {states.count('Failed')} 件・Unknown {states.count('Unknown')} 件"
           "（Verified には検査日時を付ける。JARVIS-MAP-002 §10.3）", "",
           "## 全体構成図", "", "```mermaid", diagram.rstrip(), "```", "",
           "## Resource の状態", "",
           "| resource_id | 種類 | project | 状態 | 理由 | 検査日時 |", "|---|---|---|---|---|---|"]
    for rid, j in sorted(report.judgements.items()):
        r = resources.get(rid, {})
        out.append(f"| `{rid}` | {TYPE_LABEL.get(r.get('resource_type'), '')} | {r.get('project_id', '')} | "
                   f"{j.state} | {j.reason} | {j.checked_at or '—'} |")
    return "\n".join(out) + "\n"


def render_outputs(registry: dict, snapshot: dict, report: Report) -> dict[str, str]:
    diagram = mermaid(registry, report)
    return {
        "discovered/latest.json": json.dumps(snapshot, ensure_ascii=False, indent=2) + "\n",
        "generated/guardian-report.md": guardian_report(report),
        "generated/architecture.mmd": diagram,
        "generated/STATUS.md": status_md(registry, report, diagram),
    }


def write_outputs(registry_dir: Path, texts: dict[str, str]) -> list[Path]:
    paths = []
    for rel, text in texts.items():
        path = registry_dir / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
        paths.append(path)
    return paths
