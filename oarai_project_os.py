"""⑥民泊: 大洗Project OS（oarai-project-os-since2026）への改造提案。

対話から抽出した構造化変更案（Decision/Task/Evidence/Activity/Risk/Topic Summary）を、
同リポジトリの `docs/09_it_dx/orchestrator-ingestion-contract.md` が定義するingestion
envelopeと同じスキーマで構築する。

JARVISはoarai-project-os側の data/*.json を直接書き換えない。GitHubへの直接コミットに
よるステージングも行わない（runtime/は同リポジトリの.gitignore対象で、GitHubコミットでは
本番のランタイムに反映されないため）。

本番API（/health, /api/ingest）が疎通可能な場合のみ送信する。疎通不可の場合は、提案を
JARVISのDBにTaskノードとして保持し、`resubmit_pending_proposals()`で後から送信し直せる
ようにする。
"""

import json
from datetime import datetime, timezone
from typing import Any, Literal

import requests

from config import OARAI_PROJECT_OS_API_BASE
from db import find_nodes_by_title_prefix, insert_node, update_node_content, update_node_title

PENDING_PREFIX = "Oarai Project OS提案(未送信): "
SUBMITTED_PREFIX = "Oarai Project OS提案(送信済み): "

EntityType = Literal["activity", "evidence", "task", "decision_candidate", "risk", "topic_update", "note"]


def build_ingestion_envelope(
    raw_summary: str,
    proposed_changes: list[dict[str, Any]],
    source: str = "conversation",
    source_ref: str | None = None,
    occurred_at: str | None = None,
    tags: list[str] | None = None,
) -> dict[str, Any]:
    """ingestion envelope（oarai-project-os側のPOST /api/ingestと同スキーマ）を構築する。"""
    changes = []
    for change in proposed_changes:
        change = dict(change)
        if change.get("entity_type") == "decision_candidate":
            change["requires_user_approval"] = True
        changes.append(change)

    return {
        "source": source,
        "source_ref": source_ref,
        "occurred_at": occurred_at or datetime.now(timezone.utc).isoformat(),
        "raw_summary": raw_summary,
        "proposed_changes": changes,
        "tags": tags or [],
    }


def propose_change(
    summary: str,
    entity_type: EntityType,
    raw_summary: str | None = None,
    confidence: Literal["low", "medium", "high", "confirmed"] = "medium",
    payload: dict[str, Any] | None = None,
    area_ids: list[str] | None = None,
    topic_ids: list[str] | None = None,
    action: Literal["create", "update"] = "create",
    entity_id: str | None = None,
    **envelope_kwargs: Any,
) -> dict[str, Any]:
    """単一の変更案からingestion envelopeを構築する（複数案を一度に送る場合はbuild_ingestion_envelopeを使う）。"""
    change = {
        "entity_type": entity_type,
        "action": action,
        "entity_id": entity_id,
        "area_ids": area_ids or [],
        "topic_ids": topic_ids or [],
        "summary": summary,
        "payload": payload or {},
        "confidence": confidence,
        "requires_user_approval": entity_type == "decision_candidate",
    }
    return build_ingestion_envelope(raw_summary or summary, [change], **envelope_kwargs)


def check_api_health(timeout: int = 5) -> bool:
    """本番API（/health）が疎通可能かを確認する。"""
    try:
        res = requests.get(f"{OARAI_PROJECT_OS_API_BASE}/health", timeout=timeout)
        return res.status_code == 200 and res.json().get("status") == "ok"
    except (requests.exceptions.RequestException, ValueError):
        return False


def submit_envelope(envelope: dict[str, Any], timeout: int = 15) -> dict[str, Any]:
    """POST /api/ingestでenvelopeを送信する（ステージングのみ。正本は書き換わらない）。"""
    res = requests.post(f"{OARAI_PROJECT_OS_API_BASE}/api/ingest", json=envelope, timeout=timeout)
    res.raise_for_status()
    return res.json()


def submit_or_hold(envelope: dict[str, Any]) -> dict[str, Any]:
    """APIが疎通可能なら送信し、不可能ならJARVISのDBに保留として保持する。"""
    if check_api_health():
        response = submit_envelope(envelope)
        insert_node(
            node_type="Task", category="minpaku",
            title=f"{SUBMITTED_PREFIX}{envelope['raw_summary'][:60]}",
            content=json.dumps({"envelope": envelope, "response": response}, ensure_ascii=False),
        )
        return {"status": "submitted", **response}

    node_id = insert_node(
        node_type="Task", category="minpaku",
        title=f"{PENDING_PREFIX}{envelope['raw_summary'][:60]}",
        content=json.dumps(envelope, ensure_ascii=False),
    )
    return {"status": "held", "task_id": node_id, "message": "本番API未応答のためJARVIS側に保留しました。"}


def resubmit_pending_proposals() -> list[dict[str, Any]]:
    """保留中の提案を再走査し、API疎通が回復していれば送信し直す。"""
    if not check_api_health():
        return []

    results = []
    for node in find_nodes_by_title_prefix("Task", PENDING_PREFIX):
        envelope = json.loads(node["content"])
        response = submit_envelope(envelope)
        update_node_title(node["id"], f"{SUBMITTED_PREFIX}{envelope['raw_summary'][:60]}")
        update_node_content(node["id"], json.dumps({"envelope": envelope, "response": response}, ensure_ascii=False))
        results.append({"task_id": node["id"], **response})

    return results
