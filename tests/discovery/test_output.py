"""出力（T-403）のテスト。"""

import re

from discovery import guardian, output


def _registry():
    return {"resources": {
        "host-mac-mini": {"resource_type": "host"},
        "repo-a": {"resource_type": "repository", "host": "host-mac-mini", "project_id": "jarvis",
                   "depends_on": ["ext-gh", "host-mac-mini"], "publishes_to": ["web-x"]},
        "ext-gh": {"resource_type": "external_service", "project_id": "jarvis"},
        "web-x": {"resource_type": "website", "project_id": "piano"},
    }}


def _report():
    r = guardian.Report(checked_at="2026-10-13T01:00:00+09:00")
    r.judgements = {"host-mac-mini": guardian.Judgement("Verified", "この端末", "2026-10-13T01:00:00+09:00"),
                    "repo-a": guardian.Judgement("Verified", "ok", "2026-10-13T01:00:00+09:00"),
                    "ext-gh": guardian.Judgement("Unknown", "未検査", "2026-10-13T01:00:00+09:00"),
                    "web-x": guardian.Judgement("Failed", "HTTP 500", "2026-10-13T01:00:00+09:00")}
    r.findings = [guardian.Finding("error", "unreachable", "web-x", "HTTP 500")]
    r.out_of_scope = {"docker": ["pocs0108_a"], "launchd": []}
    return r


def test_mermaid_is_well_formed_and_colored():
    diagram = output.mermaid(_registry(), _report())
    assert diagram.startswith("flowchart LR\n")
    assert 'subgraph host_mac_mini["host-mac-mini"]' in diagram and 'subgraph external["外部・Web"]' in diagram
    assert "repo_a --> ext_gh" in diagram and "host_mac_mini" not in diagram.split("end")[-1].split("classDef")[0]
    assert "repo_a -.->|publish| web_x" in diagram
    assert "class repo_a verified" in diagram and "class web_x failed" in diagram and "class ext_gh unknown" in diagram
    for line in diagram.splitlines():
        m = re.match(r"\s+(\S+)\[", line)
        if m:
            assert re.fullmatch(r"[A-Za-z0-9_]+", m.group(1))


def test_reports():
    texts = output.render_outputs(_registry(), {"checked_at": "x", "resources": {}}, _report())
    assert set(texts) == set(output.OUTPUT_FILES)
    report = texts["generated/guardian-report.md"]
    assert "エラー 1 件" in report and "`web-x`（unreachable）: HTTP 500" in report and "docker: 1 件" in report
    status = texts["generated/STATUS.md"]
    assert "最終確認日時: 2026-10-13T01:00:00+09:00" in status and "```mermaid" in status
    assert "| `web-x` | Web | piano | Failed | HTTP 500 | 2026-10-13T01:00:00+09:00 |" in status


def test_write_outputs(tmp_path):
    texts = output.render_outputs(_registry(), {"checked_at": "x", "resources": {}}, _report())
    paths = output.write_outputs(tmp_path, texts)
    assert sorted(p.relative_to(tmp_path).as_posix() for p in paths) == sorted(output.OUTPUT_FILES)
