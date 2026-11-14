"""Knowledge Engine（MAP Phase 7、../docs/architecture.md §15、ADR-0010）。

ナレッジグラフ（Knowledge / Source）を、プロジェクトや Vault をまたいで検索し、知識から出典までたどる。
Vault を統合せず（ADR-0011）、取り込み済みの Knowledge ノードを横断する。

- search(): 質問から名詞を取り出し（janome）、BM25 で順位付けする。プロジェクトで絞り込める
- trace(): 知識 → 出典（Source）→ 元の場所（URL・Vault 内のパス・Registry の resource_id）
- audit(): 重複（同じ題名・同じ出典）と鮮度（プロジェクトごとの期限切れ）
- stats(): データソース別・分野別の件数（管理・集計の質問に、LLM に推測させず集計で答える。ADR-0027）
- answer(): 検索結果だけを根拠に、出典番号付きで答える（RAG。LLM は llm.complete）

プロジェクトは、CKC の知識なら meta の projects、それ以外はカテゴリから Registry の jarvis_category で決める。
"""

import json
import math
import os
import re
import sqlite3
import unicodedata
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path

import db
import latency
import llm
import prompts
import registry
import tuning
from db import _fetchall, extract_keywords, get_node

BM25_K1, BM25_B = 1.2, 0.75
TITLE_WEIGHT = 2  # 題名に含まれる語は本文の2回分と数える
# プロジェクトごとの鮮度の期限（日）。ここにないプロジェクトは期限なし
FRESHNESS_DAYS = {"ai-news": 30, "trading": 30, "realestate": 90}


@dataclass
class Hit:
    knowledge_id: str
    title: str
    score: float
    projects: list[str]
    url: str | None
    created_at: str
    snippet: str


@dataclass
class Trace:
    knowledge: dict
    sources: list[dict] = field(default_factory=list)


def _category_projects() -> dict[str, str]:
    return {cat: pid for pid, (_, cat) in registry.projects().items() if cat}


def projects_of(row, category_map: dict[str, str] | None = None) -> list[str]:
    meta = json.loads(row["meta"] or "{}")
    if meta.get("projects"):
        return list(meta["projects"])
    category_map = category_map if category_map is not None else _category_projects()
    pid = category_map.get(row["category"] or "")
    return [pid] if pid else []


def _knowledge_rows():
    return _fetchall("SELECT id, title, content, category, url, created_at, meta FROM nodes WHERE type = 'Knowledge'")


def _snippet(text: str, terms: list[str], width: int = 80) -> str:
    text = re.sub(r"\s+", " ", text or "")
    positions = [text.find(t) for t in terms if t in text]
    start = max(0, min(positions) - 20) if positions else 0
    return text[start:start + width]


def expand_terms(query: str, terms: list[str], synonyms: dict[str, list[str]]) -> list[str]:
    """質問に辞書の左の語があれば、右の語を検索の語に加える（逆向きは辞書に書く）。"""
    out = list(terms)
    for word, alts in synonyms.items():
        if word and word in query:
            out += [a for a in alts if a and a not in out]
    return out


def search(query: str, projects: list[str] | None = None, limit: int = 10,
           synonyms: dict[str, list[str]] | None = None) -> list[Hit]:
    """synonyms を省くと tuning.current().synonyms（既定は空）。"""
    synonyms = tuning.current().synonyms if synonyms is None else synonyms
    terms = expand_terms(query, extract_keywords(query) or [query.strip()], synonyms)
    rows = _knowledge_rows()
    category_map = _category_projects()
    docs = []
    for row in rows:
        row_projects = projects_of(row, category_map)
        if projects and not set(projects) & set(row_projects):
            continue
        title, body = row["title"] or "", row["content"] or ""
        tf = {t: title.count(t) * TITLE_WEIGHT + body.count(t) for t in terms}
        docs.append((row, row_projects, tf, len(title) + len(body)))
    if not docs:
        return []
    avg_len = sum(d[3] for d in docs) / len(docs)
    n = len(docs)
    df = {t: sum(1 for d in docs if d[2][t] > 0) for t in terms}
    hits = []
    for row, row_projects, tf, length in docs:
        score = 0.0
        for t in terms:
            if tf[t] == 0:
                continue
            idf = math.log(1 + (n - df[t] + 0.5) / (df[t] + 0.5))
            score += idf * tf[t] * (BM25_K1 + 1) / (tf[t] + BM25_K1 * (1 - BM25_B + BM25_B * length / avg_len))
        if score > 0:
            hits.append(Hit(row["id"], row["title"] or "", round(score, 3), row_projects, row["url"],
                            row["created_at"], _snippet(f'{row["title"]} {row["content"]}', terms)))
    hits.sort(key=lambda h: (h.score, h.created_at), reverse=True)  # 同点なら新しいものを先に
    return hits[:limit]


def trace(knowledge_id: str) -> Trace | None:
    node = get_node(knowledge_id)
    if node is None or node["type"] != "Knowledge":
        return None
    result = Trace({"id": node["id"], "title": node["title"], "category": node["category"], "url": node["url"],
                    "created_at": node["created_at"], "projects": projects_of(node)})
    rows = _fetchall("SELECT n.* FROM nodes n JOIN edges e ON e.dst_id = n.id "
                     "WHERE e.src_id = ? AND e.type = 'derived_from' ORDER BY n.created_at", (knowledge_id,))
    vault_by_repo = _vault_by_github_repo()
    for s in rows:
        meta = json.loads(s["meta"] or "{}")
        origin = meta.get("resource_id")
        if not origin and s["url"] and s["url"].startswith("https://github.com/"):
            origin = vault_by_repo.get("/".join(s["url"].split("/")[3:5]))
        result.sources.append({"id": s["id"], "type": s["type"], "title": s["title"], "url": s["url"],
                               "vault_path": meta.get("vault_path"), "resource_id": origin,
                               "conversation_id": meta.get("ckc_conversation_id")})
    return result


def _vault_by_github_repo() -> dict[str, str]:
    data = registry.load() or {}
    out = {}
    for rid, r in (data.get("resources") or {}).items():
        remote = r.get("git_remote") or ""
        if "github.com/" in remote:
            out[remote.split("github.com/", 1)[1].removesuffix(".git")] = rid
    return out


def _norm_title(title: str) -> str:
    t = unicodedata.normalize("NFKC", title or "").lower()
    return re.sub(r"[\s\W_]+", "", t)


def audit(now: datetime | None = None) -> dict:
    """重複と鮮度の監査結果。{"duplicate_titles": [[id, ...]], "duplicate_urls": [...], "stale": {project: n}}"""
    now = now or datetime.now()
    rows = _knowledge_rows()
    by_title, by_url, stale = {}, {}, {}
    category_map = _category_projects()
    for row in rows:
        key = _norm_title(row["title"])
        if key:
            by_title.setdefault(key, []).append(row["id"])
        if row["url"]:
            by_url.setdefault(row["url"], []).append(row["id"])
        try:
            created = datetime.fromisoformat(row["created_at"]).replace(tzinfo=None)
        except (TypeError, ValueError):
            continue
        for pid in projects_of(row, category_map):
            days = FRESHNESS_DAYS.get(pid)
            if days and now - created > timedelta(days=days):
                stale[pid] = stale.get(pid, 0) + 1
    return {"knowledge": len(rows),
            "duplicate_titles": sorted([ids for ids in by_title.values() if len(ids) > 1], key=len, reverse=True),
            "duplicate_urls": sorted([ids for ids in by_url.values() if len(ids) > 1], key=len, reverse=True),
            "stale": dict(sorted(stale.items()))}


STATS_TARGET = "jarvis.db の Knowledge"
STATS_UNAVAILABLE = "jarvis.db に取り込まれていない（件数は未取得）"
NO_CATEGORY = "(なし)"
_FIXED_SOURCES = {
    "data-insight-reporter-db": "Insight Reporter",
    "web": "Web の記事（RSS など）",
    "unknown": "出所の記録なし",
    "other": "その他の出所",
}


def _source_name(rid: str, res: dict | None) -> str:
    if rid in _FIXED_SOURCES:
        return _FIXED_SOURCES[rid]
    res = res or {}
    repo = (res.get("git_remote") or "").rstrip("/").removesuffix(".git").rsplit("/", 1)[-1]
    if repo and res.get("resource_type") == "vault":
        return f"Obsidian の Vault「{repo}」"
    if repo and res.get("resource_type") == "repository":
        return f"リポジトリ「{repo}」"
    return rid


def _source_prefixes(resources: dict) -> list[tuple[str, str]]:
    """(URL・パスの先頭, resource_id) の一覧。長いものを先に。git_remote を持つ資源だけ。"""
    out = []
    for rid, res in resources.items():
        remote = res.get("git_remote")
        if not remote:
            continue
        out.append((remote.removesuffix(".git"), rid))
        locator = res.get("locator")
        if isinstance(locator, dict):
            locator = locator.get("host-mac-mini")
        if isinstance(locator, str) and locator not in ("", "unknown"):
            out.append((os.path.expanduser(locator).rstrip("/"), rid))
    return sorted(out, key=lambda p: len(p[0]), reverse=True)


def _source_of(url: str | None, prefixes: list[tuple[str, str]]) -> str:
    if not url:
        return "unknown"
    for prefix, rid in prefixes:
        if url == prefix or (url.startswith(prefix) and url[len(prefix)] in "/?#"):
            return rid
    if url.startswith("insight-reporter://"):
        return "data-insight-reporter-db"
    if url.startswith(("http://", "https://")):
        return "web"
    return "other"


def _iso(value: str | None) -> str | None:
    try:
        return datetime.fromisoformat(value).isoformat() if value else None
    except ValueError:
        return value


def _fetchall_readonly(query: str) -> list[sqlite3.Row]:
    """読み取り専用（mode=ro）で DB を開いて読む。DB がなければファイルを作らず sqlite3.OperationalError。"""
    conn = sqlite3.connect(Path(db.DB_PATH).resolve().as_uri() + "?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    try:
        return conn.execute(query).fetchall()
    finally:
        conn.close()


def _knowledge_sources(resources: dict) -> list[tuple[sqlite3.Row, str]]:
    """(Knowledge の行, データソースの ID) の一覧（読み取りだけ）。根拠の Source の URL を優先する。"""
    prefixes = _source_prefixes(resources)
    rows = _fetchall_readonly(
        "SELECT k.id, k.category, k.created_at, k.url AS k_url, "
        "CASE WHEN json_valid(k.meta) THEN json_extract(k.meta, '$.section_key') END AS section_key, "
        "(SELECT s.id FROM edges e JOIN nodes s ON s.id = e.dst_id "
        " WHERE e.src_id = k.id AND e.type = 'derived_from' ORDER BY s.created_at LIMIT 1) AS s_id, "
        "(SELECT s.url FROM edges e JOIN nodes s ON s.id = e.dst_id "
        " WHERE e.src_id = k.id AND e.type = 'derived_from' ORDER BY s.created_at LIMIT 1) AS s_url, "
        "EXISTS (SELECT 1 FROM edges e WHERE e.src_id = k.id AND e.type = 'derived_from') AS has_source "
        "FROM nodes k WHERE k.type = 'Knowledge'")
    return [(row, _source_of(row["s_url"] if row["has_source"] else row["k_url"], prefixes)) for row in rows]


def source_by_knowledge() -> dict[str, str]:
    """知識 ID → データソースの ID（stats() と同じ判定。評価部門の層化に使う、T-1410）。"""
    resources = (registry.load() or {}).get("resources") or {}
    return {row["id"]: sid for row, sid in _knowledge_sources(resources)}


def _count_units(rows: list[tuple[sqlite3.Row, str]]) -> list[tuple[str | None, str, str | None]]:
    """数える単位 (分野, データソースの ID, 最終更新) の一覧。節の Knowledge（meta に section_key、T-1419）は
    根拠の Source ごとに 1 件とし、最終更新はその節の中の最新。ほかの知識は 1 件ずつ。"""
    units, files = [], {}
    for row, sid in rows:
        if row["section_key"] is None or row["s_id"] is None:
            units.append((row["category"], sid, row["created_at"]))
            continue
        key = row["s_id"]
        if key not in files:
            files[key] = len(units)
            units.append((row["category"], sid, row["created_at"]))
            continue
        category, source, last = units[files[key]]
        created = row["created_at"]
        units[files[key]] = (category, source, created if last is None or (created and created > last) else last)
    return units


def stats(now: datetime | None = None) -> dict:
    """データソース別・分野別の Knowledge の件数（読み取りだけ）。件数は jarvis.db の Knowledge の数
    （節に分けた Knowledge はファイルごとに 1 件）。"""
    data = registry.load()
    resources = (data or {}).get("resources") or {}
    rows = _count_units(_knowledge_sources(resources))
    by_source, by_category = {}, {}
    for category, sid, created in rows:
        for key, table in ((sid, by_source), (category or NO_CATEGORY, by_category)):
            count, last = table.get(key, (0, None))
            table[key] = (count + 1, created if last is None or (created and created > last) else last)

    def ranked(items):
        return sorted(items, key=lambda x: (-x["count"], x.get("name", x.get("category"))))

    sources = ranked([{"source_id": sid, "name": _source_name(sid, resources.get(sid)), "count": count,
                       "last_updated": _iso(last), "target": STATS_TARGET}
                      for sid, (count, last) in by_source.items()])
    categories = ranked([{"category": cat, "count": count, "last_updated": _iso(last)}
                         for cat, (count, last) in by_category.items()])
    unavailable = [{"source_id": rid, "name": _source_name(rid, res), "reason": STATS_UNAVAILABLE}
                   for rid, res in sorted(resources.items())
                   if res.get("resource_type") == "vault" and rid not in by_source]
    result = {"total": len(rows), "by_source": sources, "by_category": categories, "unavailable": unavailable}
    if data is None:
        result["registry_unavailable"] = True
    return result


# 答えのプロンプトの部品は prompts.py（決まりの文を 1 か所に置く、ADR-0038）。ここには 1 回限りの呼び出しの形を置く
ANSWER_PROMPT = prompts.answer_template("default")
GROUNDED_PROMPT = prompts.answer_template("grounded")
# 答えのプロンプト（tuning.Tuning.answer_prompt で選ぶ。T-1411）
ANSWER_PROMPTS = {"default": ANSWER_PROMPT, "grounded": GROUNDED_PROMPT}
VOICE_STYLE = prompts.VOICE_STYLE
HISTORY_TURNS = 12         # 渡す直近の発言の数（6 往復、config.HANDOFF_EXCHANGES。ADR-0038 で 3 往復から変更）
HISTORY_CHARS = 600        # 1 発言あたりの上限


def format_history(history) -> str:
    """これまでの会話（ckc.models.Turn か記録の {"speaker", "text"} の並び）を引き継ぎメモの形にする: 直近 6 往復は
    'Owner: ...' / 'JARVIS: ...' のまま、それより前は機械的な短いまとめ（ADR-0038。前は直近 6 発言だけ）。"""
    import harness_session
    return harness_session.handoff_memo(list(history or []))


def cited(text: str, hits: list) -> set[int]:
    """答えの中で使われた資料の番号（1 始まり）。"""
    return {int(n) for n in re.findall(r"\[(\d+)\]", text) if 1 <= int(n) <= len(hits)}


def prepare(question: str, projects: list[str] | None = None, limit: int | None = None,
            context_chars: int | None = None, prompt: str | None = None) -> tuple[str, str, list[Hit]]:
    """検索して資料の文を作る。(資料の使い方の名前, 資料の文, hits)。limit・context_chars・prompt を省くと tuning.current()。
    ハーネスのセッションでは、資料の文を発言ごとのメッセージ（prompts.answer_turn）に入れる（ADR-0038）。"""
    t = tuning.current()
    limit = t.search_limit if limit is None else limit
    context_chars = t.context_chars if context_chars is None else context_chars
    name = t.answer_prompt if prompt is None else prompt
    if name not in ANSWER_PROMPTS:
        raise ValueError(f"未知の答えのプロンプトです: {name}（{', '.join(ANSWER_PROMPTS)}）")
    hits = search(question, projects=projects, limit=limit)   # 同義語も tuning.current() から
    latency.mark("search")
    context = "\n\n".join(f"[{i}] {h.title}（{', '.join(h.projects) or '-'}、{h.created_at[:10]}）\n"
                            f"{(get_node(h.knowledge_id)['content'] or '')[:context_chars]}"
                            for i, h in enumerate(hits, 1)) or "（なし）"
    return name, context, hits


def answer(question: str, projects: list[str] | None = None, limit: int | None = None,
           on_text=None, history=None, voice: bool = False, cancel=None,
           provider: str | None = None, context_chars: int | None = None,
           prompt: str | None = None, memory: str | None = None) -> tuple[str, list[Hit]]:
    """蓄積した知識を根拠に答え、なければ LLM 自身の知識で答える（ADR-0026）。
    on_text を渡すと、答えを生成しながら差分を渡す（逐次表示、T-1212）。返す hits は検索結果の全件
    （番号は答えの [n] と対応する。表示する出典は cited() で絞る）。provider を省くと設定（JARVIS_LLM_PROVIDER）。
    limit・context_chars・prompt（ANSWER_PROMPTS のキー）を省くと tuning.current() の値（T-1411）。"""
    name, context, hits = prepare(question, projects=projects, limit=limit, context_chars=context_chars, prompt=prompt)
    prompt = ANSWER_PROMPTS[name].format(question=question, context=context, history=format_history(history),
                                  style=VOICE_STYLE if voice else "", memory=memory or "（なし）")
    if on_text is not None:
        extra = {"cancel": cancel} if cancel is not None else {}   # 取り消しのない呼び出しは従来どおり
        return llm.complete_stream(prompt, on_text, provider=provider, **extra), hits
    return llm.complete(prompt, provider=provider), hits
