"""ACC の静的なページ（MAP Phase 6）のテスト。"""

from acc import build


def _registry():
    return {"projects": {"jarvis": {"name": "Jarvis", "description": "基盤"}, "piano": {"name": "ピアノ"}},
            "resources": {
                "host-mac-mini": {"resource_type": "host", "project_id": "jarvis", "layers": ["L0"], "model": "Mac"},
                "repo-a": {"resource_type": "repository", "project_id": "jarvis", "host": "host-mac-mini",
                           "layers": ["L1", "L4"], "branch": "main"},
                "web-x": {"resource_type": "website", "project_id": "piano", "layers": ["L5"]},
                "ext-<b>": {"resource_type": "external_service", "project_id": "jarvis", "layers": ["L4"]},
            }}


def _snapshot():
    t = "2026-10-13T01:00:00+09:00"
    return {"checked_at": t, "resources": {
        "host-mac-mini": {"checked_at": t, "facts": {"this_host": True, "disk_free_gb": 700}},
        "repo-a": {"checked_at": t, "facts": {"exists": True, "branch": "main", "git_busy": [], "uncommitted": 0}},
        "web-x": {"checked_at": t, "facts": {"url": "https://x/", "status_code": 500}},
        "ext-<b>": {"checked_at": t, "facts": {"skipped": "未検査"}},
    }, "unregistered": [], "out_of_scope": {}}


def test_page_has_all_views_and_states():
    page = build.build_html(_registry(), _snapshot())
    for anchor in ('id="problems"', 'id="projects"', 'id="infra"', 'id="layers"', 'id="diagram"'):
        assert anchor in page
    assert "最終確認 2026-10-13T01:00:00+09:00" in page
    assert '<span class="st Failed">Failed</span><code>web-x</code>' in page
    assert '<span class="st Verified">Verified</span><code>repo-a</code>' in page
    assert "HTTP 500" in page and "L4 Intelligence" in page
    assert '<pre class="mermaid">flowchart LR' in page


def test_values_are_escaped():
    page = build.build_html(_registry(), _snapshot())
    assert "<code>ext-&lt;b&gt;</code>" in page and "ext-<b>" not in page.split("<pre")[0]


def test_main_writes_file(tmp_path, monkeypatch):
    import json
    reg = tmp_path / "registry"
    (reg / "generated").mkdir(parents=True)
    (reg / "discovered").mkdir()
    (reg / "generated" / "registry.json").write_text(json.dumps(_registry()))
    (reg / "discovered" / "latest.json").write_text(json.dumps(_snapshot()))
    out = tmp_path / "acc.html"
    assert build.main(["--registry-dir", str(reg), "--out", str(out)]) == 0
    assert out.read_text(encoding="utf-8").startswith("<!doctype html>")
    assert build.main(["--registry-dir", str(tmp_path / "none"), "--out", str(out)]) == 2


def test_dev_section_lists_runs_and_escapes():
    runs = [{"task_id": "T-<x>", "status": "PASS", "cycles": 2, "reason": None, "started_at": "2026-10-17T16:51:16+09:00",
             "pr_url": "https://github.com/unikarei/demo/pull/7"},
            {"task_id": "T-2", "status": "BLOCKED", "cycles": 1, "reason": "rate_limited", "started_at": "t",
             "pr_url": "javascript:alert(1)"}]
    page = build.build_html(_registry(), _snapshot(), dev_runs=runs)
    assert 'id="dev"' in page and "python -m acc.dev_console" in page
    assert "T-&lt;x&gt;" in page and '<a href="https://github.com/unikarei/demo/pull/7">7</a>' in page
    assert "rate_limited" in page and "javascript:" not in page


def test_dev_section_without_runs():
    assert "実行の記録はまだありません" in build.build_html(_registry(), _snapshot())


def test_dev_section_hides_archived_runs():
    runs = [{"task_id": "T-1304", "status": "PASS", "cycles": 1, "started_at": "t", "archived": True},
            {"task_id": "T-1400", "status": "running", "cycles": 0, "started_at": "t", "archived": False}]
    page = build.build_html(_registry(), _snapshot(), dev_runs=runs)
    section = page.split('id="dev"')[1].split("</section>")[0]
    assert "T-1400" in section and "T-1304" not in section and "後片付け済みの run 1 件" in section
