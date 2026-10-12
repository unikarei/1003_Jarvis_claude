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
