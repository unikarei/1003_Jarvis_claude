"""Project OS への提案の送信と保留のテスト。HTTP はモック（../docs/project-os-api-restart-plan.md）。"""

import importlib
import json
from types import SimpleNamespace

import pytest
import requests

import config
import db
import oarai_project_os as pos


class _Api:
    """Project OS の API の代わり。up が False なら接続できない。"""

    def __init__(self):
        self.up = False
        self.posted = []

    def get(self, url, timeout=None):
        if not self.up:
            raise requests.ConnectionError()
        return SimpleNamespace(status_code=200, json=lambda: {"status": "ok"})

    def post(self, url, json=None, timeout=None):
        if not self.up:
            raise requests.ConnectionError()
        self.posted.append((url, json))
        return SimpleNamespace(status_code=202, raise_for_status=lambda: None,
                               json=lambda: {"ingestion_id": f"ING-{len(self.posted)}", "status": "staged"})


@pytest.fixture
def api(db_path, monkeypatch):
    fake = _Api()
    monkeypatch.setattr(pos.requests, "get", fake.get)
    monkeypatch.setattr(pos.requests, "post", fake.post)
    return fake


def test_default_base_is_local_and_overridable(monkeypatch):
    monkeypatch.delenv("JARVIS_OARAI_API_BASE", raising=False)
    assert importlib.reload(config).OARAI_PROJECT_OS_API_BASE == "http://127.0.0.1:8010"
    monkeypatch.setenv("JARVIS_OARAI_API_BASE", "http://127.0.0.1:9000/")
    assert importlib.reload(config).OARAI_PROJECT_OS_API_BASE == "http://127.0.0.1:9000"
    monkeypatch.delenv("JARVIS_OARAI_API_BASE")
    importlib.reload(config)


def test_held_while_api_is_down_then_resubmitted(api):
    env = pos.propose_change("サウナを入れるか", "decision_candidate")
    held = pos.submit_or_hold(env)
    assert held["status"] == "held" and api.posted == []
    assert pos.resubmit_pending_proposals() == []          # まだ止まっている
    api.up = True
    sent = pos.resubmit_pending_proposals()
    assert [r["task_id"] for r in sent] == [held["task_id"]] and len(api.posted) == 1
    url, body = api.posted[0]
    assert url.endswith("/api/ingest") and body["proposed_changes"][0]["requires_user_approval"] is True
    node = db.get_node(held["task_id"])
    assert node["title"].startswith(pos.SUBMITTED_PREFIX) and json.loads(node["content"])["response"]["status"] == "staged"
    assert pos.resubmit_pending_proposals() == []          # 送信済みは二度送らない


def test_submitted_when_api_is_up(api):
    api.up = True
    res = pos.submit_or_hold(pos.propose_change("見積もりを依頼する", "task"))
    assert res["status"] == "submitted" and res["api_status"] == "staged" and res["ingestion_id"] == "ING-1"
