"""直近のKnowledgeノードからMarkdownレポートを生成し、reports/に出力する。"""

from datetime import date, datetime

from config import REPORTS_DIR
from db import get_recent_knowledge

CATEGORY_LABELS = {
    "news": "AIニュース",
    "trading": "仮想取引",
    "realestate": "不動産",
    "manufacturing": "製造業",
    "minpaku": "民泊",
}


def build_report(category: str | None = None, since: str | None = None) -> str:
    since = since or date.today().isoformat()
    knowledge_list = get_recent_knowledge(category=category, since=since)
    label = CATEGORY_LABELS.get(category, category or "全カテゴリ")

    lines = [f"# {label}日次レポート（{date.today().isoformat()}）", ""]
    if not knowledge_list:
        lines.append("本日の新規Knowledgeはありません。")
    else:
        for knowledge in knowledge_list:
            lines.append(f"## {knowledge['title']}")
            lines.append("")
            lines.append(knowledge["content"] or "")
            lines.append("")
            if knowledge["url"]:
                lines.append(f"出典: {knowledge['url']}")
                lines.append("")

    return "\n".join(lines)


def write_report(category: str | None = None) -> str:
    REPORTS_DIR.mkdir(exist_ok=True)
    report_text = build_report(category=category)
    prefix = f"{category}_" if category else ""
    filename = f"{prefix}{datetime.now().strftime('%Y%m%d_%H%M%S')}.md"
    report_path = REPORTS_DIR / filename
    report_path.write_text(report_text, encoding="utf-8")
    return str(report_path)


if __name__ == "__main__":
    import sys

    path = write_report(category=sys.argv[1] if len(sys.argv) > 1 else None)
    print(f"レポート出力: {path}")
