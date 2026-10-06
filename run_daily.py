"""日次バッチのエントリポイント: 取得 → 要約 → レポート生成を一気通貫で実行する。

レポートはカテゴリ（RSS_FEEDSで設定したカテゴリ）ごとに別ファイルとして出力する。
"""

from config import RSS_FEEDS
from db import init_db
from fetch_news import fetch_all
from generate_report import write_report
from summarize import summarize_all


def main():
    init_db()

    new_sources = fetch_all()
    print(f"[1/3] 新規Source: {len(new_sources)}件")

    new_knowledge = summarize_all()
    print(f"[2/3] 新規Knowledge: {len(new_knowledge)}件")

    categories = sorted({feed["category"] for feed in RSS_FEEDS})
    for i, category in enumerate(categories, 1):
        report_path = write_report(category=category)
        print(f"[3/3] ({i}/{len(categories)}) {category} レポート出力: {report_path}")


if __name__ == "__main__":
    main()
