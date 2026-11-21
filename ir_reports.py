"""Insight Reporter（1004）のレポートを会話で使う（上位 spec §3.6、ADR-0044、T-3401）。

- 取り込み: 1004 の reports/<id>/<日付>/report.json のうち、記事の一覧（items）を持つ調査のレポートを、過去の分も
  jarvis.db に取り込む。レポート 1 回分のまとめと、記事 1 件ずつを知識にする。1004 のファイルは読むだけ。
  同じレポートは重ねない（出典の URL insight-reporter://<id>/<generated_at>、記事は #<順位>）。
- 答え（スキル report-digest）: レポートの一覧、1 回分の概要と記事の題名を、1004 のファイルから読んで答える。
  LLM を呼ばない。ピアノ教室（数値のレポート）の取り込みは ingest_insight_reporter.py のまま。

  python ir_reports.py ingest            過去の分も含めて取り込む
  python ir_reports.py show [発言]       答えを表示する（例: "11月20日のフィジカルAIスキャンの題目を全部"）
"""
from __future__ import annotations

import json
import re
import sys
import unicodedata
from datetime import date
from pathlib import Path

import config

SKILL = "report-digest"
CATEGORY = config.CATEGORY_MANUFACTURING
# report_id → (呼び名, 発言の中の言い方, Registry のプロジェクト)
REPORTS = {
    "physical_ai": ("Physical AI Scan（タービン設計へのフィジカル AI 応用）", r"フィジカル|フィジタル|physical", "physical-ai"),
    "turbine_research": ("タービンの研究スキャン", r"タービン|turbine", "turbine-research"),
    "piano_growth": ("ピアノ教室の集客レポート", r"ピアノ|集客|piano", "piano"),
}
WEEKDAYS = "月火水木金土日"


def reports_dir() -> Path:
    return Path(config.INSIGHT_REPORTER_DIR) / "reports"


def runs(report_id: str | None = None) -> list[dict]:
    """レポートの回（日付の新しい順）。[{report_id, day, path, report}]。壊れたファイルは飛ばす。"""
    out = []
    root = reports_dir()
    ids = [report_id] if report_id else sorted(p.name for p in root.iterdir() if p.is_dir()) if root.is_dir() else []
    for rid in ids:
        for path in (root / rid).glob("*/report.json"):
            try:
                day = date.fromisoformat(path.parent.name)
                report = json.loads(path.read_text(encoding="utf-8"))
            except (ValueError, OSError):
                continue
            if isinstance(report, dict):
                out.append({"report_id": rid, "day": day, "path": path, "report": report})
    return sorted(out, key=lambda r: (r["day"], r["report_id"]), reverse=True)


def _is_research(report: dict) -> bool:
    return isinstance(report.get("items"), list) and bool(report["items"])


def _name(rid: str, report: dict) -> str:
    return report.get("name") or REPORTS.get(rid, (rid,))[0]


def _period(report: dict) -> str:
    """対象の期間。調査は {start, end}、ピアノ教室は {search_console: {current: {start, end}}}。"""
    p = report.get("period") or {}
    if isinstance(p.get("search_console"), dict):
        p = p["search_console"].get("current") or {}
    return f"{p['start']}〜{p['end']}" if p.get("start") and p.get("end") else "不明"


# ---------------------------------------------------------------- 取り込み

def _summary_text(run: dict) -> str:
    r = run["report"]
    lines = [f"# {_name(run['report_id'], r)}（{run['day']}、対象 {_period(r)}、出典: Insight Reporter）", ""]
    if r.get("overview"):
        lines += ["## 概要", *[f"- {x}" for x in r["overview"]], ""]
    for section in r.get("closing_sections") or []:
        if isinstance(section, dict):
            lines += [f"## {section.get('heading', '')}", *[f"- {x}" for x in section.get("lines") or []], ""]
    lines.append(f"## 記事（{len(r['items'])} 件）")
    lines += [f"{item.get('rank', i)}. {item.get('title', '')}（{item.get('source', '')}、{item.get('published_date', '')}）"
              for i, item in enumerate(r["items"], 1)]
    if r.get("warnings"):
        lines += ["", "## 注意", *[f"- {x}" for x in r["warnings"]]]
    return "\n".join(lines).strip()


def _item_text(run: dict, item: dict) -> str:
    r = run["report"]
    lines = [f"# {item.get('title', '')}",
             f"（{_name(run['report_id'], r)} {run['day']} の {item.get('rank', '?')} 位、出典: Insight Reporter）",
             f"- 出典: {item.get('source', '')}", f"- 公開: {item.get('published_date', '')}", f"- URL: {item.get('url', '')}"]
    if item.get("summary_points"):
        lines += ["## 要点", *[f"- {x}" for x in item["summary_points"]]]
    for key, label in (("why_it_matters", "なぜ重要か"), ("application_hint", "応用のヒント"),
                       ("caution", "注意"), ("reading_value", "読む価値")):
        if item.get(key):
            lines += [f"## {label}", str(item[key])]
    return "\n".join(lines)


def _upsert(url: str, title: str, text: str, source_id: str, project: str) -> str:
    """出典の URL の知識がなければ作り、中身が違えば更新する。'new' / 'updated' / 'same'。"""
    from db import find_knowledge_by_url, insert_edge, insert_node, update_node_content
    existing = find_knowledge_by_url(url)
    if existing is None:
        kid = insert_node("Knowledge", category=CATEGORY, title=title, content=text, url=url, meta={"projects": [project]})
        insert_edge(kid, source_id, "derived_from")
        return "new"
    if existing["content"] != text:
        update_node_content(existing["id"], text)
        return "updated"
    return "same"


def ingest_all() -> dict[str, int]:
    """調査のレポートを過去の分も取り込む。{new, updated, same}（知識の件数）。"""
    from db import find_source_by_url, insert_node, update_node_content
    counts = {"new": 0, "updated": 0, "same": 0}
    for run in runs():
        report = run["report"]
        if not _is_research(report) or not report.get("generated_at"):
            continue
        rid = run["report_id"]
        project = REPORTS.get(rid, ("", "", rid))[2]
        base = f"insight-reporter://{rid}/{report['generated_at']}"
        raw = json.dumps(report, ensure_ascii=False)
        source = find_source_by_url(base)
        if source is None:
            source_id = insert_node("Source", category=CATEGORY, title=f"{_name(rid, report)}（Insight Reporter）",
                                    content=raw, url=base)
        else:
            source_id = source["id"]
            if source["content"] != raw:
                update_node_content(source_id, raw)
        title = f"{_name(rid, report)} {run['day']}（対象 {_period(report)}、出典: Insight Reporter）"
        counts[_upsert(base, title, _summary_text(run), source_id, project)] += 1
        for i, item in enumerate(report["items"], 1):
            rank = item.get("rank", i)
            item_title = f"{item.get('title', '')}（{_name(rid, report)} {run['day']} の {rank} 位）"
            counts[_upsert(f"{base}#{rank}", item_title, _item_text(run, item), source_id, project)] += 1
    return counts


# ---------------------------------------------------------------- 発言の判定と答え（report-digest）

_IR = r"インサイト(?:レポート|レポーター|リポート)|insight ?report|フィジカルaiスキャン|フィジタルaiスキャン|physical ?ai ?scan|研究スキャン"
_ASK = r"一覧|題目|題名|タイトル|見出し|まとめ|中身|内容|何件|件数|どんな|ダイジェスト|並べて|全部|最新|教えて|見せて|ある[?？]?$|あった"
_FILLER = (r"の|は|を|が|って|て|で|に|と|な|も|か|よ|ね|だけ|全部|ぜんぶ|すべて|一覧|題目|題名|タイトル|見出し|まとめ|中身|内容|"
           r"何件|件数|どんな|ダイジェスト|並べて|並べ|みて|最新|今週|先週|前回|教えて|見せて|ある|あった|ください|くれる|"
           r"書かれて|書いて|いる|いた|あの|えっと|ちょっと|じゃあ|検索して|検索|調べて|の分|分|レポート|レポーター|リポート|"
           r"インサイト|insight|reporter|report|スキャン|scan|ai|研究|ジャービス|jarvis|一つ|個|件|もの")
RESIDUE_MAX = 4


def _norm(text: str) -> str:
    return re.sub(r"\s+", "", unicodedata.normalize("NFKC", text).lower())


def _kind(t: str) -> str | None:
    for rid, (_, pattern, _) in REPORTS.items():
        if re.search(pattern, t):
            return rid
    return None


def matches(text: str) -> bool:
    """Insight Reporter のレポートの一覧・題目・まとめを求める発言か（spec §3.6）。話題が残れば False。"""
    t = _norm(text)
    named = re.search(_IR, t) or (_kind(t) and re.search(r"レポート|リポート|スキャン|scan", t))
    if not named or not re.search(_ASK, t):
        return False
    if re.search(r"保存|記録して|残して|ナレッジ化|追加|メモして|obsidian|送って|メール", t):
        return False
    rest = re.sub(r"\d+月\d+日|\d+", "", t)
    for pattern in (_IR, *(p for _, p, _ in REPORTS.values()), _FILLER):
        rest = re.sub(pattern, "", rest)
    rest = re.sub(r"[、。,.!！?？・「」『』()（）〜~ー\-]", "", rest)
    return len(rest) <= RESIDUE_MAX


def _day_label(d: date) -> str:
    return f"{d.month}月{d.day}日（{WEEKDAYS[d.weekday()]}）"


def _pick(text: str, today: date) -> tuple[str | None, dict | None, str]:
    """(種類, 選んだ回, 選べなかった理由)。種類も日付もなければ (None, None, "")。"""
    t = _norm(text)
    rid = _kind(t)
    m = re.search(r"(\d+)月(\d+)日", t)
    if rid is None and not m:
        return None, None, ""
    candidates = runs(rid)
    if not candidates:
        return rid, None, "そのレポートはまだありません。"
    if m:
        month, day = int(m.group(1)), int(m.group(2))
        year = today.year if (month, day) <= (today.month, today.day) else today.year - 1
        wanted = [r for r in candidates if r["day"] == date(year, month, day)]
        if not wanted:
            days = "、".join(sorted({_day_label(r['day']) for r in candidates}, reverse=True)[:5])
            return rid, None, f"{month}月{day}日のレポートはありません。ある日: {days}"
        if rid is None and len({r["report_id"] for r in wanted}) > 1:
            return None, {"day": wanted[0]["day"], "many": wanted}, ""
        return wanted[0]["report_id"], wanted[0], ""
    if re.search(r"先週|前回", t) and len(candidates) > 1:
        return rid, candidates[1], ""
    return rid, candidates[0], ""


def _list_all() -> str:
    lines = ["Insight Reporter のレポート（毎週金曜 18:00 に作成。日付は作った日）:"]
    by_kind = {}
    for run in runs():
        by_kind.setdefault(run["report_id"], []).append(run)
    if not by_kind:
        return "Insight Reporter のレポートが見つかりません（1004 の reports フォルダを読めません）。"
    for rid, items in by_kind.items():
        latest = items[0]
        count = len(latest["report"].get("items") or [])
        what = f"記事 {count} 件" if count else "数値のレポート"
        older = "、".join(_day_label(r["day"]) for r in items[1:])
        lines.append(f"- {_name(rid, latest['report'])}: 最新 {_day_label(latest['day'])}（{what}）"
                     + (f"。前の回: {older}" if older else ""))
    lines.append("「11月20日のフィジカルAIスキャンの題目を全部」のように聞けば、その回の概要と題目を並べます。")
    return "\n".join(lines)


def _show_run(run: dict) -> str:
    r, rid = run["report"], run["report_id"]
    head = f"{_name(rid, r)}、{_day_label(run['day'])}の回（対象 {_period(r)}）"
    if _is_research(r):
        lines = [f"{head}の記事は {len(r['items'])} 件です。"]
        if r.get("overview"):
            lines.append("概要: " + r["overview"][0])
        for i, item in enumerate(r["items"], 1):
            lines.append(f"{item.get('rank', i)}. {item.get('title', '')}（{item.get('source', '')}、"
                         f"{item.get('published_date', '')}）— {item.get('reading_value', '')}")
        for section in r.get("closing_sections") or []:
            if isinstance(section, dict) and section.get("lines"):
                lines.append(f"{section.get('heading', '')}: {section['lines'][0]}")
        lines.append("記事の中身は、続けて「1 位の記事の要点は？」のように聞いてください（検索して答えます）。")
        return "\n".join(lines)
    lines = [f"{head}です。"]
    lines += [f"- {x}" for x in (r.get("summary") or [])[:5]]
    if r.get("recommendations"):
        lines.append("改善の提案:")
        for x in r["recommendations"][:5]:
            if isinstance(x, dict):
                lines.append(f"- {x.get('observation', '')} → {x.get('recommended_action', '')}")
            else:
                lines.append(f"- {x}")
    return "\n".join(lines)


def digest(text: str = "", today: date | None = None) -> str:
    """report-digest の答え（画面に出す文）。1004 のファイルを読むだけ。"""
    today = today or date.today()
    try:
        rid, run, reason = _pick(text, today)
        if reason:
            return reason
        if run is None:
            return _list_all()
        if "many" in run:
            return "\n\n".join(_show_run(r) for r in run["many"])
        return _show_run(run)
    except OSError as e:
        return f"Insight Reporter のレポートを読めませんでした（{type(e).__name__}）。"


def briefing_lines() -> list[str]:
    """会話の始めの概要に入れる、レポートの種類・最新の日付・件数と言い方（spec §3.6）。"""
    try:
        latest = {}
        for run in runs():
            latest.setdefault(run["report_id"], run)
    except OSError:
        return ["- Insight Reporter のレポート: 未確認"]
    lines = [f"- {_name(rid, run['report'])}: 最新 {run['day']}（"
             + (f"記事 {len(run['report']['items'])} 件" if _is_research(run["report"]) else "数値のレポート") + "）"
             for rid, run in latest.items()]
    lines.append("- 調査のレポートは記事ごとに検索 DB に入っている（中身の質問は検索で答える）。一覧・題目は"
                 "「インサイトレポートの一覧」「11月20日のフィジカルAIスキャンの題目を全部」と言ってもらえば並べる")
    return lines or ["- Insight Reporter のレポート: なし"]


def main(argv: list[str]) -> int:
    if argv[:1] == ["ingest"]:
        from db import init_db
        init_db()
        print(f"Insight Reporter の取り込み: {ingest_all()}")
        return 0
    if argv[:1] == ["show"]:
        print(digest(" ".join(argv[1:]) or "インサイトレポートの一覧"))
        return 0
    print(__doc__)
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
