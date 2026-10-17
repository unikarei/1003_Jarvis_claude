"""Knowledge Engine の CLI（MAP Phase 7）。

    python knowledge_cli.py search "大洗 物件" [--project oarai-minpaku]   # プロジェクト横断の検索
    python knowledge_cli.py trace <knowledge_id>                          # 知識から出典までたどる
    python knowledge_cli.py audit                                         # 重複と鮮度
    python knowledge_cli.py audit --json                                  # 重複と鮮度（ほかのプログラム向けの JSON）
    python knowledge_cli.py stats [--json]                                # データソース別・分野別の件数
    python knowledge_cli.py ask "質問" [--project ...]                    # 検索結果だけを根拠に答える（RAG）
"""

import argparse
import json
import sqlite3
import sys

import knowledge_engine as ke
import llm


def _when(iso: str | None) -> str:
    return iso[:16].replace("T", " ") if iso else "-"


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="Knowledge Engine")
    sub = parser.add_subparsers(dest="cmd", required=True)
    for name in ("search", "ask"):
        p = sub.add_parser(name)
        p.add_argument("query")
        p.add_argument("--project", action="append", help="プロジェクトで絞り込む（複数指定可）")
        p.add_argument("--limit", type=int, default=10 if name == "search" else 8)
    sub.add_parser("trace").add_argument("knowledge_id")
    for name in ("audit", "stats"):
        sub.add_parser(name).add_argument("--json", action="store_true", help="結果を JSON で出す")
    args = parser.parse_args(argv)

    if args.cmd == "search":
        hits = ke.search(args.query, projects=args.project, limit=args.limit)
        if not hits:
            print("該当する知識はありません。")
        for i, h in enumerate(hits, 1):
            print(f"{i}. [{h.score}] {h.title}（{', '.join(h.projects) or '-'}、{h.created_at[:10]}）\n"
                  f"   {h.knowledge_id}  {h.url or ''}\n   …{h.snippet}…")
        return 0
    if args.cmd == "trace":
        t = ke.trace(args.knowledge_id)
        if t is None:
            print(f"Knowledge が見つかりません: {args.knowledge_id}", file=sys.stderr)
            return 1
        k = t.knowledge
        print(f"知識: {k['title']}（{', '.join(k['projects']) or '-'}、{k['created_at'][:10]}）\n  {k['id']}  {k['url'] or ''}")
        for s in t.sources or []:
            where = " / ".join(x for x in (s["resource_id"], s["vault_path"], s["conversation_id"]) if x)
            print(f"  ← 出典: {s['title']}  {s['url'] or ''}\n          {where}")
        if not t.sources:
            print("  （出典の記録なし）")
        return 0
    if args.cmd == "audit":
        try:
            a = ke.audit()
        except (sqlite3.Error, OSError) as e:
            print(f"エラー: {e}", file=sys.stderr)
            return 1
        if args.json:
            print(json.dumps(a, ensure_ascii=False, indent=2))
            return 0
        print(f"Knowledge {a['knowledge']} 件")
        print(f"題名の重複: {len(a['duplicate_titles'])} 組（最大 {max(map(len, a['duplicate_titles']), default=0)} 件）")
        print(f"出典 URL の重複: {len(a['duplicate_urls'])} 組")
        print("鮮度の期限切れ: " + (", ".join(f"{k} {v} 件" for k, v in a["stale"].items()) or "なし"))
        return 0
    if args.cmd == "stats":
        try:
            s = ke.stats()
        except (sqlite3.Error, OSError) as e:
            print(f"エラー: {e}", file=sys.stderr)
            return 1
        if args.json:
            print(json.dumps(s, ensure_ascii=False, indent=2))
            return 0
        print(f"{ke.STATS_TARGET} {s['total']} 件")
        print("データソース別:\n順位\t名前\t件数\t最終更新")
        for i, x in enumerate(s["by_source"], 1):
            print(f"{i}\t{x['name']}\t{x['count']}\t{_when(x['last_updated'])}")
        if s.get("registry_unavailable"):
            print("（Registry を読めないため、Vault・リポジトリの区別なし）")
        print("未取得: " + ("、".join(x["name"] for x in s["unavailable"]) or "なし"))
        print("分野別: " + (", ".join(f"{x['category']} {x['count']} 件" for x in s["by_category"]) or "なし"))
        return 0
    if args.cmd == "ask":
        try:
            text, hits = ke.answer(args.query, projects=args.project, limit=args.limit)
        except llm.LLMError as e:
            print(f"エラー: {e}", file=sys.stderr)
            return 1
        print(text)
        for i, h in enumerate(hits, 1):
            print(f"[{i}] {h.title}  {h.url or h.knowledge_id}")
        return 0
    return 0


if __name__ == "__main__":
    sys.exit(main())
