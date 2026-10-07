"""①ピアノ教室運営補助: Search Console・GA4データの分析とJARVISによる改善提案の生成。

目的は単なるアクセス集計ではなく「問い合わせを増やすために次に何をすべきか」を
提案すること。Search ConsoleとGA4はユーザー単位で結合できないため、同一ユーザー
として関連付けず、期間単位・ページ単位の実測値として並記する。

今回はデータ取得→分析→改善提案の提示までを行う。Webサイト本文・title/description・
Google Business Profile・GA4/Search Consoleの設定・広告出稿の変更は一切行わない。
"""

import json
from datetime import date

from config import PIANO_BRAND_KEYWORDS, PIANO_INQUIRY_EVENT_KEYWORDS, PIANO_LOCAL_KEYWORDS, PIANO_SOCIAL_EVENT_KEYWORDS
from db import insert_edge, insert_node
from fetch_ga4 import discover_ga4_events, get_ga4_report, get_ga4_top_pages, get_ga4_traffic_sources
from fetch_search_console import get_search_console_report
from llm import ask_claude_tiered

LOW_CTR_THRESHOLD = 0.03
RANK_CHANGE_THRESHOLD = 0.5

RECOMMENDATION_PROMPT = """以下は小野ピアノ音楽教室（https://ono-piano.com/piano.class/）のアクセス分析データです。
この教室の目的は「問い合わせを増やすこと」です。データを踏まえ、次に行うべき施策を
優先度順に最大3件提案してください。

各提案は必ず以下の形式で、具体的に書いてください。
「SEOを改善しましょう」のような抽象的な提案のみは禁止です。

優先度N:
根拠データ: （どのデータを見てそう判断したか）
問題: （何が課題か）
推奨施策: （具体的に何をするか）
期待効果: （何がどう改善される見込みか）

# データ
{report_text}
"""


def _pct_delta(current, previous) -> str:
    """増減率を安全にフォーマットする（ゼロ除算・データ不足に対応）。"""
    if previous is None:
        return "(前期間データなし)"
    if previous == 0:
        return "(前期間0)" if current == 0 else f"(前期間0 → {current})"
    diff_pct = (current - previous) / previous * 100
    sign = "+" if diff_pct >= 0 else ""
    return f"(前期間比 {sign}{diff_pct:.0f}%)"


def _position_delta(current, previous) -> str:
    """掲載順位の差分をフォーマットする（数値が小さいほど上位＝改善）。"""
    if previous is None:
        return "(前期間データなし)"
    diff = previous - current
    verdict = "改善" if diff > 0 else "悪化" if diff < 0 else "変化なし"
    return f"(前期間 {previous:.1f} → {verdict})"


def classify_queries(queries: list[dict]) -> dict:
    """クエリを複数の観点で分類する（表示回数多×CTR低、掲載順位4〜15位、
    順位・クリック数の増減、地域/ブランドクエリ）。"""
    categories = (
        "high_impression_low_ctr", "mid_rank_opportunity", "rank_improved", "rank_declined",
        "clicks_increased", "clicks_decreased", "local_queries", "branded_queries", "non_branded_queries",
    )
    if not queries:
        return {k: [] for k in categories}

    avg_impressions = sum(q["impressions"] for q in queries) / len(queries)

    high_impression_low_ctr = [
        q for q in queries if q["impressions"] >= avg_impressions and q["ctr"] < LOW_CTR_THRESHOLD
    ]
    mid_rank_opportunity = [q for q in queries if 4 <= q["position"] <= 15]

    rank_improved, rank_declined = [], []
    clicks_increased, clicks_decreased = [], []
    for q in queries:
        if q["prev_position"] is not None and abs(q["position"] - q["prev_position"]) >= RANK_CHANGE_THRESHOLD:
            (rank_improved if q["position"] < q["prev_position"] else rank_declined).append(q)
        if q["prev_clicks"] is not None and q["clicks"] != q["prev_clicks"]:
            (clicks_increased if q["clicks"] > q["prev_clicks"] else clicks_decreased).append(q)

    branded_queries = [q for q in queries if any(kw in q["query"] for kw in PIANO_BRAND_KEYWORDS)]
    branded_set = {q["query"] for q in branded_queries}

    return {
        "high_impression_low_ctr": sorted(high_impression_low_ctr, key=lambda q: -q["impressions"]),
        "mid_rank_opportunity": sorted(mid_rank_opportunity, key=lambda q: q["position"]),
        "rank_improved": sorted(rank_improved, key=lambda q: q["position"] - q["prev_position"]),
        "rank_declined": sorted(rank_declined, key=lambda q: q["prev_position"] - q["position"]),
        "clicks_increased": sorted(clicks_increased, key=lambda q: -(q["clicks"] - q["prev_clicks"])),
        "clicks_decreased": sorted(clicks_decreased, key=lambda q: q["clicks"] - q["prev_clicks"]),
        "local_queries": [q for q in queries if any(kw in q["query"] for kw in PIANO_LOCAL_KEYWORDS)],
        "branded_queries": branded_queries,
        "non_branded_queries": [q for q in queries if q["query"] not in branded_set],
    }


def classify_events(events: list[dict]) -> dict:
    """GA4で実際に発生しているイベントを問い合わせ系／SNS遷移系／その他に分類する
    （イベント名は discover_ga4_events() の実測値のみを対象とし、推測で決め打ちしない）。"""
    inquiry, social, other = [], [], []
    for e in events:
        name = e["event_name"].lower()
        if any(kw in name for kw in PIANO_INQUIRY_EVENT_KEYWORDS):
            inquiry.append(e)
        elif any(kw in name for kw in PIANO_SOCIAL_EVENT_KEYWORDS):
            social.append(e)
        else:
            other.append(e)
    return {"inquiry": inquiry, "social": social, "other": other}


def build_report_text(
    gsc_report: dict | None,
    ga4_report: dict | None,
    ga4_top_pages: list[dict],
    ga4_traffic_sources: list[dict],
    event_classification: dict,
    query_classification: dict,
    warnings: list[str],
) -> str:
    lines = [f"# ①ピアノ教室 アクセス分析レポート（{date.today().isoformat()}）", ""]

    if warnings:
        lines.extend(warnings)
        lines.append("")

    lines.append("## Google Search Performance（実測値、Search Console）")
    if gsc_report:
        lines.append(f"対象期間: {gsc_report['current_period']['start']} 〜 {gsc_report['current_period']['end']}"
                      f"（前期間: {gsc_report['previous_period']['start']} 〜 {gsc_report['previous_period']['end']}）")
        lines.append(f"- Clicks: {gsc_report['total_clicks']} {_pct_delta(gsc_report['total_clicks'], gsc_report['prev_total_clicks'])}")
        lines.append(f"- Impressions: {gsc_report['total_impressions']} {_pct_delta(gsc_report['total_impressions'], gsc_report['prev_total_impressions'])}")
        lines.append(f"- CTR: {gsc_report['total_ctr']:.2%} {_pct_delta(gsc_report['total_ctr'], gsc_report['prev_total_ctr'])}")
        lines.append(f"- Average Position: {gsc_report['total_position']:.1f} {_position_delta(gsc_report['total_position'], gsc_report['prev_total_position'])}")
    else:
        lines.append("（データ取得不可）")
    lines.append("")

    lines.append("## Website Traffic（実測値、GA4）")
    if ga4_report:
        cur, prev = ga4_report["current"], ga4_report["previous"]
        lines.append(f"直近{ga4_report['days']}日 / 前{ga4_report['days']}日 比較")
        lines.append(f"- Active Users: {cur.get('active_users', 0)} {_pct_delta(cur.get('active_users', 0), prev.get('active_users'))}")
        lines.append(f"- Sessions: {cur.get('sessions', 0)} {_pct_delta(cur.get('sessions', 0), prev.get('sessions'))}")
        lines.append(f"- Page Views: {cur.get('page_views', 0)} {_pct_delta(cur.get('page_views', 0), prev.get('page_views'))}")
        lines.append(f"- New Users: {cur.get('new_users', 0)} {_pct_delta(cur.get('new_users', 0), prev.get('new_users'))}")
        lines.append(f"- Engagement Rate: {cur.get('engagement_rate', 0):.1%}")
        lines.append(f"- Avg Session Duration: {cur.get('avg_session_duration', 0):.0f}秒")

        if ga4_top_pages:
            lines.append("")
            lines.append("Top Pages:")
            for p in ga4_top_pages[:10]:
                lines.append(f"- {p['page_path']}: page_views={p['page_views']}, active_users={p['active_users']}")

        if ga4_traffic_sources:
            lines.append("")
            lines.append("Traffic Sources:")
            for s in ga4_traffic_sources[:10]:
                lines.append(f"- {s['source']} / {s['medium']}: sessions={s['sessions']}")
    else:
        lines.append("（データ取得不可）")
    lines.append("")

    lines.append("## Conversion / Inquiry（実測値、GA4イベント）")
    inquiry_events = event_classification.get("inquiry", [])
    if inquiry_events:
        for e in inquiry_events:
            lines.append(f"- {e['event_name']}: {e['event_count']}件")
    else:
        lines.append("問い合わせ系イベントは検出されませんでした（計測未整備の可能性）。")
    social_events = event_classification.get("social", [])
    if social_events:
        lines.append("")
        lines.append("SNS遷移系イベント（参考）:")
        for e in social_events:
            lines.append(f"- {e['event_name']}: {e['event_count']}件")
    lines.append("")

    if gsc_report:
        lines.append("## Top Search Queries（実測値、Search Console・上位10件）")
        lines.append("|Query|Clicks|Impressions|CTR|Position|前期間比(Clicks)|")
        lines.append("|---|---|---|---|---|---|")
        for q in gsc_report["queries"][:10]:
            lines.append(
                f"|{q['query']}|{q['clicks']}|{q['impressions']}|{q['ctr']:.1%}|{q['position']:.1f}|"
                f"{_pct_delta(q['clicks'], q['prev_clicks'])}|"
            )
        lines.append("")

        lines.append("## クエリ分析（JARVISによる分類、実測値ベース）")
        qc = query_classification
        if qc["high_impression_low_ctr"]:
            top = qc["high_impression_low_ctr"][0]
            lines.append(f"- 表示回数が多いがCTRが低いクエリ: {top['query']}（impressions={top['impressions']}, ctr={top['ctr']:.1%}）他{len(qc['high_impression_low_ctr'])}件")
        if qc["mid_rank_opportunity"]:
            top = qc["mid_rank_opportunity"][0]
            lines.append(f"- 掲載順位の改善余地あり（4〜15位）: {top['query']}（{top['position']:.1f}位）他{len(qc['mid_rank_opportunity'])}件")
        if qc["rank_improved"]:
            lines.append(f"- 順位上昇クエリ: {len(qc['rank_improved'])}件（例: {qc['rank_improved'][0]['query']}）")
        if qc["rank_declined"]:
            lines.append(f"- 順位低下クエリ: {len(qc['rank_declined'])}件（例: {qc['rank_declined'][0]['query']}）")
        lines.append(f"- 地域クエリ（{'/'.join(PIANO_LOCAL_KEYWORDS)}等）: {len(qc['local_queries'])}件")
        lines.append(f"- ブランドクエリ（{'/'.join(PIANO_BRAND_KEYWORDS)}等）: {len(qc['branded_queries'])}件 / 非ブランドクエリ: {len(qc['non_branded_queries'])}件")
        lines.append("")

    lines.append("## 分析方法についての注記")
    lines.append("Search ConsoleとGA4はユーザー単位で結合できないため、同一ユーザーとして関連付けていません。"
                  "検索クエリの傾向（Search Console）とサイト内のページ別アクセス・流入元（GA4）は、期間単位・ページ単位の実測値として別々に並記しています。")

    return "\n".join(lines)


def generate_recommendations(report_text: str) -> str:
    """分析レポートをもとに、Claudeで優先度順Top3の改善提案を生成する（推測であることが分かる見出しを付ける）。"""
    return ask_claude_tiered(RECOMMENDATION_PROMPT.format(report_text=report_text), max_tokens=2500)


def run_piano_analysis() -> str:
    """Search Console・GA4からデータを取得して分析し、改善提案を生成、Source/Knowledgeとして保存する。
    どちらかの取得に失敗しても処理を止めず、取得できた方のデータのみで続行する。
    """
    warnings: list[str] = []
    gsc_report = None
    ga4_report = None
    ga4_top_pages: list[dict] = []
    ga4_traffic_sources: list[dict] = []
    ga4_events: list[dict] = []

    try:
        gsc_report = get_search_console_report(days=28)
    except Exception as e:
        warnings.append(f"[WARN] Search Console unavailable ({type(e).__name__}). Using available data only.")

    try:
        ga4_report = get_ga4_report(days=7)
        ga4_top_pages = get_ga4_top_pages(days=7)
        ga4_traffic_sources = get_ga4_traffic_sources(days=7)
        ga4_events = discover_ga4_events(days=28)
    except Exception as e:
        warnings.append(f"[WARN] GA4 unavailable ({type(e).__name__}). Using available data only.")

    query_classification = classify_queries(gsc_report["queries"]) if gsc_report else classify_queries([])
    event_classification = classify_events(ga4_events)

    report_text = build_report_text(
        gsc_report, ga4_report, ga4_top_pages, ga4_traffic_sources,
        event_classification, query_classification, warnings,
    )

    if gsc_report is None and ga4_report is None:
        full_text = report_text + "\n\nSearch Console・GA4とも取得できなかったため、改善提案は生成していません。"
    else:
        recommendations = generate_recommendations(report_text)
        full_text = report_text + "\n\n## JARVIS Recommendations（JARVISによる提案、推測を含む）\n\n" + recommendations

    snapshot = {
        "date": date.today().isoformat(),
        "search_console": gsc_report,
        "ga4_report": ga4_report,
        "ga4_top_pages": ga4_top_pages,
        "ga4_traffic_sources": ga4_traffic_sources,
        "ga4_events": ga4_events,
    }
    source_id = insert_node(
        node_type="Source", category="piano",
        title=f"ピアノ教室アクセス分析スナップショット {date.today().isoformat()}",
        content=json.dumps(snapshot, ensure_ascii=False, default=str),
    )
    knowledge_id = insert_node(
        node_type="Knowledge", category="piano",
        title=f"①ピアノ教室アクセス分析レポート {date.today().isoformat()}",
        content=full_text,
    )
    insert_edge(knowledge_id, source_id, "derived_from")

    return full_text


def run_piano_update() -> str:
    """①ピアノ教室の定期更新。1004_Insight_Reporterの最新レポートを上位ソースとして優先し、
    不足する場合（レポート不在・古い・必須フィールド欠落）のみJARVIS独自に取得する。
    """
    from ingest_insight_reporter import ingest_report

    report = ingest_report("piano_growth")
    if report is not None:
        return f"Insight Reporterのレポートを取り込みました（generated_at={report['generated_at']}）"

    return run_piano_analysis()


if __name__ == "__main__":
    print(run_piano_update())
