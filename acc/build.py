"""ACC の静的なページを作る（../docs/architecture.md §14）。

入力: Registry の generated/registry.json（設計上の意図）と discovered/latest.json（Discovery の実態）。
Guardian（discovery/guardian.py）で判定し直し、Project View・Infrastructure View・Layer View・問題の一覧・
全体構成図（Mermaid）を1つの HTML にまとめる。外部への送信はしない。Mermaid の描画だけ CDN の固定版を読む
（読めないときは図の元のテキストを表示する）。

使い方: python -m acc.build [--registry-dir DIR] [--out reports/acc/index.html]
"""

import argparse
import html
import json
import sys
from collections import defaultdict
from pathlib import Path

import config
from discovery import settings
from discovery.guardian import Report, evaluate
from discovery.output import TYPE_LABEL, mermaid

DEFAULT_OUT = config.REPORTS_DIR / "acc" / "index.html"
MERMAID_JS = "https://cdn.jsdelivr.net/npm/mermaid@10.9.1/dist/mermaid.min.js"
LAYERS = {"L0": "Architecture Registry", "L1": "Orchestration", "L2": "Data Foundation",
          "L3": "Knowledge", "L4": "Intelligence", "L5": "Presentation"}
SEVERITY_LABEL = {"error": "エラー", "warning": "警告", "info": "情報"}

CSS = """
:root { --bg:#f8fafc; --fg:#0f172a; --muted:#64748b; --card:#fff; --line:#e2e8f0;
        --ok:#16a34a; --ng:#dc2626; --unk:#94a3b8; --warn:#d97706; }
@media (prefers-color-scheme: dark) { :root { --bg:#0b1120; --fg:#e2e8f0; --muted:#94a3b8; --card:#111827; --line:#1f2937; } }
* { box-sizing: border-box; }
body { margin:0; font-family: -apple-system, "Hiragino Sans", "Noto Sans JP", sans-serif; background:var(--bg); color:var(--fg); }
header { padding:20px 24px; border-bottom:1px solid var(--line); }
header h1 { margin:0 0 6px; font-size:20px; }
.muted { color:var(--muted); font-size:13px; }
main { padding:16px 24px 48px; max-width:1200px; margin:0 auto; }
nav a { margin-right:14px; font-size:14px; }
.kpis { display:flex; gap:12px; flex-wrap:wrap; margin:16px 0; }
.kpi { background:var(--card); border:1px solid var(--line); border-radius:10px; padding:10px 14px; min-width:120px; }
.kpi b { display:block; font-size:22px; }
section { margin-top:28px; }
h2 { font-size:17px; border-bottom:1px solid var(--line); padding-bottom:6px; }
.grid { display:grid; grid-template-columns: repeat(auto-fill, minmax(320px, 1fr)); gap:12px; }
.card { background:var(--card); border:1px solid var(--line); border-radius:10px; padding:12px 14px; }
.card h3 { margin:0 0 8px; font-size:15px; }
ul.res { list-style:none; padding:0; margin:0; }
ul.res li { padding:3px 0; font-size:13px; display:flex; gap:8px; align-items:baseline; }
code { font-size:12px; }
.st { font-size:11px; padding:1px 7px; border-radius:999px; border:1px solid currentColor; white-space:nowrap; }
.Verified { color:var(--ok); } .Failed { color:var(--ng); } .Unknown { color:var(--unk); }
.error { color:var(--ng); } .warning { color:var(--warn); } .info { color:var(--muted); }
table { border-collapse: collapse; width:100%; font-size:13px; }
td, th { border-bottom:1px solid var(--line); padding:6px 8px; text-align:left; vertical-align:top; }
.diagram { background:var(--card); border:1px solid var(--line); border-radius:10px; padding:12px; overflow:auto; }
@media (max-width: 600px) { main, header { padding-left:16px; padding-right:16px; } }
"""


def _e(text) -> str:
    return html.escape(str(text if text is not None else ""))


def _badge(state: str) -> str:
    return f'<span class="st {_e(state)}">{_e(state)}</span>'


def _resource_line(rid: str, registry: dict, report: Report) -> str:
    r = registry["resources"][rid]
    j = report.judgements.get(rid)
    state = j.state if j else "Unknown"
    reason = f' <span class="muted">{_e(j.reason)}</span>' if j else ""
    return (f"<li>{_badge(state)}<code>{_e(rid)}</code>"
            f'<span class="muted">{_e(TYPE_LABEL.get(r.get("resource_type"), ""))}</span>{reason}</li>')


def project_view(registry: dict, report: Report) -> str:
    by_project = defaultdict(list)
    for rid, r in sorted(registry["resources"].items()):
        by_project[r.get("project_id")].append(rid)
    cards = []
    for pid, p in sorted(registry.get("projects", {}).items()):
        items = "".join(_resource_line(rid, registry, report) for rid in by_project.get(pid, []))
        cards.append(f'<div class="card"><h3>{_e(p.get("name", pid))} <code>{_e(pid)}</code></h3>'
                     f'<div class="muted">{_e(p.get("description", ""))}</div>'
                     f'<ul class="res">{items or "<li class=muted>Resource なし</li>"}</ul></div>')
    return '<div class="grid">' + "".join(cards) + "</div>"


def infrastructure_view(registry: dict, report: Report) -> str:
    groups = defaultdict(list)
    for rid, r in sorted(registry["resources"].items()):
        if r.get("resource_type") == "host":
            continue
        groups[r.get("host") or "外部・Web"].append(rid)
    cards = []
    for host in sorted(groups):
        title = host
        if host in registry["resources"]:
            h = registry["resources"][host]
            j = report.judgements.get(host)
            title = f'{_badge(j.state if j else "Unknown")} {_e(host)} <span class="muted">{_e(h.get("model") or h.get("os"))}</span>'
        items = "".join(_resource_line(rid, registry, report) for rid in groups[host])
        cards.append(f'<div class="card"><h3>{title}</h3><ul class="res">{items}</ul></div>')
    return '<div class="grid">' + "".join(cards) + "</div>"


def layer_view(registry: dict, report: Report) -> str:
    rows = []
    for layer, name in LAYERS.items():
        rids = [rid for rid, r in sorted(registry["resources"].items()) if layer in (r.get("layers") or [])]
        items = " ".join(f'{_badge(report.judgements[rid].state if rid in report.judgements else "Unknown")} '
                         f"<code>{_e(rid)}</code>" for rid in rids)
        rows.append(f"<tr><th>{layer} {name}</th><td>{items}</td></tr>")
    return "<table>" + "".join(rows) + "</table>"


def problems(report: Report) -> str:
    if not report.findings:
        return '<p class="muted">問題はありません。</p>'
    rows = "".join(f'<tr><td class="{f.severity}">{SEVERITY_LABEL[f.severity]}</td><td>{_e(f.kind)}</td>'
                   f"<td><code>{_e(f.resource_id or '-')}</code></td><td>{_e(f.message)}</td></tr>"
                   for f in report.findings)
    return "<table><tr><th>重要度</th><th>種類</th><th>Resource</th><th>内容</th></tr>" + rows + "</table>"


def build_html(registry: dict, snapshot: dict) -> str:
    report = evaluate(registry, snapshot)
    states = [j.state for j in report.judgements.values()]
    sev = {s: sum(1 for f in report.findings if f.severity == s) for s in SEVERITY_LABEL}
    kpis = [("Resource", len(registry["resources"])), ("Verified", states.count("Verified")),
            ("Failed", states.count("Failed")), ("Unknown", states.count("Unknown")),
            ("エラー", sev["error"]), ("警告", sev["warning"])]
    diagram = mermaid(registry, report)
    return f"""<!doctype html>
<html lang="ja"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>Jarvis ACC</title><style>{CSS}</style></head>
<body>
<header><h1>Jarvis Architecture Control Center</h1>
<div class="muted">最終確認 {_e(report.checked_at)}（Discovery の実行時点）。このページは 1003 の <code>acc/build.py</code> が生成する。</div>
<nav><a href="#problems">問題</a><a href="#projects">Project View</a><a href="#infra">Infrastructure View</a><a href="#layers">Layer View</a><a href="#diagram">構成図</a></nav>
</header>
<main>
<div class="kpis">{"".join(f'<div class="kpi"><span class="muted">{_e(k)}</span><b>{v}</b></div>' for k, v in kpis)}</div>
<section id="problems"><h2>問題（Guardian）</h2>{problems(report)}</section>
<section id="projects"><h2>Project View</h2>{project_view(registry, report)}</section>
<section id="infra"><h2>Infrastructure View</h2>{infrastructure_view(registry, report)}</section>
<section id="layers"><h2>Layer View</h2>{layer_view(registry, report)}</section>
<section id="diagram"><h2>全体構成図</h2><div class="diagram"><pre class="mermaid">{_e(diagram)}</pre></div></section>
</main>
<script src="{MERMAID_JS}"></script>
<script>if (window.mermaid) {{ mermaid.initialize({{ startOnLoad: true, securityLevel: "strict" }}); }}</script>
</body></html>
"""


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="ACC の静的なページを作る")
    parser.add_argument("--registry-dir", type=Path, default=settings.REGISTRY_DIR)
    parser.add_argument("--out", type=Path, default=DEFAULT_OUT)
    args = parser.parse_args(argv)
    try:
        registry = json.loads((args.registry_dir / "generated" / "registry.json").read_text(encoding="utf-8"))
        snapshot = json.loads((args.registry_dir / "discovered" / "latest.json").read_text(encoding="utf-8"))
    except (OSError, ValueError) as e:
        print(f"Registry または Discovery の結果を読めません: {e}", file=sys.stderr)
        return 2
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(build_html(registry, snapshot), encoding="utf-8")
    print(f"書きました: {args.out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
