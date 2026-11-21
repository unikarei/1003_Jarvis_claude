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
    print(f"[1/4] 新規Source: {len(new_sources)}件")

    new_knowledge = summarize_all()
    print(f"[2/4] 新規Knowledge: {len(new_knowledge)}件")

    # ニュースの採点（上位 spec §3.5.1、T-3300）。失敗しても続く処理は行う
    try:
        from news_rank import score_pending
        scored, failed = score_pending()
        print(f"[2/4] ニュースの採点: {scored}件、採点できず: {failed}件")
    except Exception as e:
        print(f"[2/4] ニュースの採点: スキップ（{type(e).__name__}: {e}）")

    try:
        from piano_analytics import run_piano_update
        print(f"[3/4] ①ピアノ教室: {run_piano_update()}")
    except Exception as e:
        print(f"[3/4] ①ピアノ教室アクセス分析: スキップ（{type(e).__name__}）")

    categories = sorted({feed["category"] for feed in RSS_FEEDS} | {"piano"})
    for i, category in enumerate(categories, 1):
        report_path = write_report(category=category)
        print(f"[4/4] ({i}/{len(categories)}) {category} レポート出力: {report_path}")


if __name__ == "__main__":
    main()
