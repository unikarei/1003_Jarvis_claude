"""直近のKnowledgeノードからMarkdownレポートを生成し、reports/に出力する。"""

from datetime import date, datetime

from config import REPORTS_DIR
from db import get_recent_knowledge


def build_report(category: str | None = None, since: str | None = None) -> str:
    since = since or date.today().isoformat()
    knowledge_list = get_recent_knowledge(category=category, since=since)

    lines = [f"# AIニュース日次レポート（{date.today().isoformat()}）", ""]
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
    filename = f"{datetime.now().strftime('%Y%m%d_%H%M%S')}.md"
    report_path = REPORTS_DIR / filename
    report_path.write_text(report_text, encoding="utf-8")
    return str(report_path)


if __name__ == "__main__":
    path = write_report()
    print(f"レポート出力: {path}")
