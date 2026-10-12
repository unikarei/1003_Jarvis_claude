"""既存の知識との照合（CKC Phase 1 は簡易版: キーワードの一致数で似た知識を探す）。

対象はナレッジグラフに登録済みの CKC の知識（meta に ckc_id を持つ Knowledge ノード）で、状態が active のもの。
重複・更新の判断は Owner が CLI で行う（新規 / 既存を置き換え / 保存しない）。
"""

import json

from db import _fetchall, _keyword_match_clause, extract_keywords

MAX_SIMILAR = 3


def find_similar(title: str, body: str, limit: int = MAX_SIMILAR) -> list[dict]:
    """似た既存の知識を、一致したキーワードの多い順に返す。[{ckc_id, title, kind, note_relpath, resource_id}]"""
    keywords = extract_keywords(f"{title} {body}")
    if not keywords:
        return []
    clause, params = _keyword_match_clause(keywords)
    rows = _fetchall(
        f"SELECT * FROM nodes WHERE type = 'Knowledge' AND meta LIKE '%\"ckc_id\"%' AND ({clause}) "
        "ORDER BY created_at DESC LIMIT 200",
        params,
    )
    needed = min(2, len(keywords))  # 一般的な語1つだけの一致は除く
    scored = []
    for row in rows:
        meta = json.loads(row["meta"] or "{}")
        if meta.get("status") != "active":
            continue
        text = f"{row['title']} {row['content']}"
        score = sum(1 for kw in keywords if kw in text)
        if score >= needed:
            scored.append((score, {"ckc_id": meta["ckc_id"], "title": row["title"], "kind": meta.get("kind"),
                                   "note_relpath": meta.get("note_relpath"), "resource_id": meta.get("resource_id")}))
    scored.sort(key=lambda x: x[0], reverse=True)
    return [item for _, item in scored[:limit]]
