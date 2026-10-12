"""隣接プロジェクト 1004_Insight_Reporter が生成するレポート（report.json）を
上位ソースとして取り込む。

定期情報収集は、対応するレポート種別についてはInsight Reporterが担う。JARVISは
それを優先し、不足する場合のみ独自に調べる（フォールバックは呼び出し側が行う）。

JARVISはInsight Reporter配下のファイルを読み取るのみで、変更・importは一切行わない
（Insight Reporter側のclaude.mdにも同様の方針が明記されている）。

2026-10（MAP Phase 5）から、まず Insight Reporter の API（GET /api/v1/reports/{id}/latest）で取得し、
API が止まっているとき・失敗したときは、従来どおりファイル（reports/<id>/latest.json）を読む。
"""

import json
from datetime import datetime
from pathlib import Path

import insight_reporter_api
from config import INSIGHT_REPORTER_DIR, MAX_REPORT_AGE_DAYS, REPORT_CATEGORY_MAP
from db import find_knowledge_by_url, find_source_by_url, insert_edge, insert_node, update_node_content

REQUIRED_KEYS = ("report_id", "generated_at", "metrics")


def _latest_report_path(report_id: str) -> Path:
    return INSIGHT_REPORTER_DIR / "reports" / report_id / "latest.json"


def load_latest_report(report_id: str) -> dict | None:
    """Insight Reporterの最新レポートを取得する。API → ファイルの順に試し、どちらもなければNoneを返す。"""
    try:
        report = insight_reporter_api.latest_report(report_id)
    except (insight_reporter_api.ApiUnavailable, insight_reporter_api.ApiError) as e:
        print(f"[Insight Reporter API] {e}。ファイルを読みます")
        report = None
    if report is not None:
        return report
    return _load_latest_report_file(report_id)


def _load_latest_report_file(report_id: str) -> dict | None:
    """Insight Reporterの最新report.jsonをファイルから読む。存在しない・壊れている場合はNoneを返す。"""
    path = _latest_report_path(report_id)
    if not path.is_file():
        return None
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except (OSError, json.JSONDecodeError):
        return None


def is_sufficient(report: dict | None) -> bool:
    """report.jsonが「上位ソース」として十分かを判定する。
    欠落・鮮度切れ（MAX_REPORT_AGE_DAYSより古い）・必須フィールド不足ならFalse。
    """
    if report is None:
        return False
    if not all(k in report for k in REQUIRED_KEYS):
        return False
    if not report.get("metrics"):
        return False

    try:
        generated_at = datetime.fromisoformat(report["generated_at"]).replace(tzinfo=None)
    except ValueError:
        return False

    return (datetime.now() - generated_at).days <= MAX_REPORT_AGE_DAYS


def _format_report_text(report: dict) -> str:
    """report.jsonの構造化データを人間可読なMarkdownに整形する（JARVISのKnowledge用）。"""
    lines = [f"# {report.get('name', report['report_id'])}（{report['generated_at']}、出典: Insight Reporter）", ""]

    for warning in report.get("warnings") or []:
        lines.append(f"[WARN] {warning}")
    if report.get("warnings"):
        lines.append("")

    summary = report.get("summary") or []
    if summary:
        lines.append("## Summary")
        lines.extend(f"- {s}" for s in summary)
        lines.append("")

    if report.get("metrics"):
        lines.append("## Metrics")
        lines.append(f"```\n{json.dumps(report['metrics'], ensure_ascii=False, indent=2)}\n```")
        lines.append("")

    top_queries = report.get("top_queries") or []
    if top_queries:
        lines.append("## Top Queries")
        for q in top_queries[:10]:
            lines.append(f"- {q}")
        lines.append("")

    if report.get("conversions"):
        lines.append("## Conversions")
        lines.append(f"```\n{json.dumps(report['conversions'], ensure_ascii=False, indent=2)}\n```")
        lines.append("")

    recommendations = report.get("recommendations") or []
    if recommendations:
        lines.append("## Recommendations（Insight Reporter生成）")
        for r in recommendations:
            lines.append(f"- {r}")
        lines.append("")

    for note in report.get("data_notes") or []:
        lines.append(f"（注）{note}")

    return "\n".join(lines).strip()


def ingest_report(report_id: str) -> dict | None:
    """Insight Reporterの最新レポートをJARVISのSource/Knowledgeとして取り込む。
    取り込んだレポートのdictを返す。不十分な場合はNoneを返す（呼び出し側でフォールバックする）。
    """
    report = load_latest_report(report_id)
    if not is_sufficient(report):
        return None

    category = REPORT_CATEGORY_MAP.get(report_id, report_id)
    source_url = f"insight-reporter://{report_id}/{report['generated_at']}"
    raw_json = json.dumps(report, ensure_ascii=False)
    text = _format_report_text(report)
    title = f"{report.get('name', report_id)} {report['generated_at']}（出典: Insight Reporter）"

    existing_source = find_source_by_url(source_url)
    if existing_source is None:
        source_id = insert_node(
            node_type="Source", category=category,
            title=f"{report.get('name', report_id)}（Insight Reporter）",
            content=raw_json, url=source_url,
        )
        knowledge_id = insert_node(node_type="Knowledge", category=category, title=title, content=text, url=source_url)
        insert_edge(knowledge_id, source_id, "derived_from")
    elif existing_source["content"] != raw_json:
        update_node_content(existing_source["id"], raw_json)
        existing_knowledge = find_knowledge_by_url(source_url)
        if existing_knowledge:
            update_node_content(existing_knowledge["id"], text)

    return report
