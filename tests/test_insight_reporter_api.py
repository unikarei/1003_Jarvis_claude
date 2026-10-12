"""Insight Reporter の API クライアント（MAP Phase 5）のテスト。HTTP はモック。"""

import json
from types import SimpleNamespace

import pytest
import requests

import config
import ingest_insight_reporter as iir
import insight_reporter_api as api


def _resp(status=200, body=None, text=None):
    def json_():
        if text is not None:
            raise ValueError("not json")
        return body
    return SimpleNamespace(status_code=status, json=json_)


@pytest.fixture
def calls(monkeypatch):
    seen = []

    def install(handler):
        def fake_get(url, headers=None, timeout=None):
            seen.append((url, headers))
            return handler(url)
        monkeypatch.setattr(requests, "get", fake_get)
        return seen
    return install


def test_base_url_from_registry(tmp_path, monkeypatch):
    path = tmp_path / "registry.json"
    path.write_text(json.dumps({"projects": {}, "resources": {
        "api-insight-reporter": {"resource_type": "api", "base_url": "http://127.0.0.1:18080/api/v1/"}}}))
    monkeypatch.setattr(config, "REGISTRY_JSON", path)
    assert api.base_url() == "http://127.0.0.1:18080/api/v1"


def test_base_url_falls_back_to_config():
    assert api.base_url() == config.INSIGHT_REPORTER_API  # テスト用の Registry には API がない


def test_latest_report_and_health(calls):
    seen = calls(lambda url: _resp(200, {"status": "ok"} if url.endswith("/health") else {"report_id": "x"}))
    assert api.latest_report("x") == {"report_id": "x"}
    assert api.health() == {"status": "ok"}
    assert seen[0][0] == "http://127.0.0.1:18080/api/v1/reports/x/latest"
    assert seen[1][0] == "http://127.0.0.1:18080/health"


def test_errors_are_classified(calls, monkeypatch):
    calls(lambda url: _resp(404))
    assert api.latest_report("x") is None
    calls(lambda url: _resp(500))
    with pytest.raises(api.ApiError) as e:
        api.latest_report("x")
    assert e.value.status == 500
    calls(lambda url: _resp(200, text="<html>"))
    with pytest.raises(api.ApiError, match="JSON"):
        api.list_reports()

    def down(url):
        raise requests.ConnectionError()
    calls(down)
    with pytest.raises(api.ApiUnavailable):
        api.list_reports()


def test_token_header(calls, monkeypatch):
    seen = calls(lambda url: _resp(200, []))
    monkeypatch.setenv("INSIGHT_REPORTER_API_TOKEN", "t0ken")
    api.list_reports()
    assert seen[0][1] == {"Authorization": "Bearer t0ken"}


def test_ingest_prefers_api_and_falls_back_to_file(calls, tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(iir, "INSIGHT_REPORTER_DIR", tmp_path)
    (tmp_path / "reports" / "piano_growth").mkdir(parents=True)
    (tmp_path / "reports" / "piano_growth" / "latest.json").write_text(json.dumps({"report_id": "from-file"}))
    calls(lambda url: _resp(200, {"report_id": "from-api"}))
    assert iir.load_latest_report("piano_growth") == {"report_id": "from-api"}

    def down(url):
        raise requests.Timeout()
    calls(down)
    assert iir.load_latest_report("piano_growth") == {"report_id": "from-file"}
    assert "ファイルを読みます" in capsys.readouterr().out
    calls(lambda url: _resp(404))
    assert iir.load_latest_report("piano_growth") == {"report_id": "from-file"}
