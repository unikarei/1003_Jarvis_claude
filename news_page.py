"""ニュースの一覧のページを作る（上位 spec §3.5.3、T-3301）。

reports/news/index.html と、日ごとのデータ reports/news/data/<日>.json・data/index.json を作る。
ワークスペースの docs/news がこのフォルダへのリンクで、既存の配信（127.0.0.1:8765、tailnet の 8443）で開く。
読み取りだけ（jarvis.db・点数の記録に書かない）。記事の文字はページで textContent として表示する。

  python news_page.py [--out DIR]
"""
from __future__ import annotations

import html
import json
import os
import re
import sys
import tempfile
from datetime import date, datetime
from html.parser import HTMLParser
from pathlib import Path

import config
import news_rank

DEFAULT_OUT = config.REPORTS_DIR / "news"
SHORT_CHARS = 100          # 一覧の要約の長さ
BODY_CHARS = 20000         # 詳細の本文の上限
TOP = 10                   # 日ごとの上位


class _Text(HTMLParser):
    """HTML のタグを除いて文字だけにする（script・style の中身も除く）。"""
    BLOCKS = {"p", "br", "div", "li", "tr", "h1", "h2", "h3", "h4", "h5", "h6", "blockquote", "section", "article"}

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.parts, self.skip = [], 0

    def handle_starttag(self, tag, attrs):
        if tag in ("script", "style"):
            self.skip += 1
        elif tag in self.BLOCKS:
            self.parts.append("\n")

    def handle_endtag(self, tag):
        if tag in ("script", "style") and self.skip:
            self.skip -= 1
        elif tag in self.BLOCKS:
            self.parts.append("\n")

    def handle_data(self, data):
        if not self.skip:
            self.parts.append(data)


def plain_text(text: str) -> str:
    """RSS の本文（HTML のことがある）を文字だけにする。"""
    parser = _Text()
    try:
        parser.feed(text or "")
        parser.close()
        out = "".join(parser.parts)
    except Exception:  # noqa: BLE001  壊れた HTML は文字としてそのまま
        out = html.unescape(re.sub(r"<[^>]*>", " ", text or ""))
    out = re.sub(r"[ \t\r\f\v ]+", " ", out)
    out = re.sub(r" *\n[ \n]*", "\n", out)
    return out.strip()


def _short(text: str) -> str:
    text = re.sub(r"\s+", " ", text or "").strip()
    return text if len(text) <= SHORT_CHARS else text[:SHORT_CHARS] + "…"


def _safe_url(url: str) -> str:
    return url if re.match(r"https?://", url or "") else ""


def _article(a: dict) -> dict:
    return {
        "id": a["id"], "category": a["category"],
        "label": news_rank.CATEGORY_LABELS.get(a["category"], a["category"] or "分類なし"),
        "feed": a["feed"], "title": a["title"], "headline": a.get("headline", ""),
        "short": _short(a["summary"]), "summary": a["summary"],
        "score": a.get("score"), "reason": a.get("reason", ""),
        "url": _safe_url(a["url"]), "created_at": a["created_at"],
        "body": plain_text(a.get("body", ""))[:BODY_CHARS],
    }


def _write(path: Path, text: str) -> None:
    """一時ファイルに書いてから置き換える（読み込み中のページが半端な JSON を読まない）。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(text)
        os.replace(tmp, path)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise


def build(out_dir: Path | None = None, now: datetime | None = None) -> Path:
    """ページとデータを作り、index.html のパスを返す。"""
    out_dir = Path(out_dir or DEFAULT_OUT)
    now = now or datetime.now()
    counts = news_rank.day_counts(date(2000, 1, 1), now.date())
    days = []
    for day in sorted(counts, reverse=True):
        d = date.fromisoformat(day)
        items = [_article(a) for a in news_rank.ranked(news_rank.articles(d, d, with_body=True))]
        _write(out_dir / "data" / f"{day}.json", json.dumps({"day": day, "articles": items}, ensure_ascii=False))
        days.append({"day": day, "count": len(items), "scored": sum(1 for a in items if a["score"] is not None)})
    updated = now.strftime("%Y-%m-%d %H:%M")
    index = {"updated": updated, "days": days, "top": TOP,
             "categories": [[slug, label] for slug, label in news_rank.CATEGORY_LABELS.items()]}
    _write(out_dir / "data" / "index.json", json.dumps(index, ensure_ascii=False))
    page = out_dir / "index.html"
    _write(page, PAGE.replace("{{UPDATED}}", html.escape(updated)))
    return page


PAGE = r"""<!doctype html>
<html lang="ja">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Jarvis ニュース</title>
<style>
/* 1003 の news_page.py が作る（上位 spec §3.5.3）。手で編集しない。色はランチャーと同じ。 */
:root {
  --paper: #f3f5f6; --sheet: #ffffff; --ink: #17222c; --muted: #56646f; --rule: #d3dbe0;
  --accent: #2b5a84; --accent-soft: #e3ecf4; --hi: #b4541f; --hi-soft: #f7e6da;
  --f-body: "BIZ UDPGothic", "Hiragino Sans", "Yu Gothic", "Meiryo", sans-serif;
  --f-mono: "IBM Plex Mono", ui-monospace, "SFMono-Regular", Menlo, Consolas, monospace;
}
@media (prefers-color-scheme: dark) {
  :root:not([data-theme="light"]) {
    color-scheme: dark; --paper: #11171c; --sheet: #172028; --ink: #e2e8ec; --muted: #97a5b0; --rule: #2c3944;
    --accent: #7fb0dc; --accent-soft: #1d3042; --hi: #f0a070; --hi-soft: #3a2a1f;
  }
}
:root[data-theme="dark"] {
  color-scheme: dark; --paper: #11171c; --sheet: #172028; --ink: #e2e8ec; --muted: #97a5b0; --rule: #2c3944;
  --accent: #7fb0dc; --accent-soft: #1d3042; --hi: #f0a070; --hi-soft: #3a2a1f;
}
* { box-sizing: border-box; }
body { margin: 0; background: var(--paper); color: var(--ink); font-family: var(--f-body); line-height: 1.6; }
main { max-width: 920px; margin: 0 auto; padding: 24px 16px 48px; }
h1 { font-size: 1.5rem; margin: 0 0 2px; }
h2 { font-size: 1.05rem; margin: 28px 0 8px; padding-bottom: 4px; border-bottom: 1px solid var(--rule); }
.updated, .note { color: var(--muted); font-size: 0.8rem; margin: 0; }
.days { display: flex; gap: 6px; margin: 16px 0 8px; overflow-x: auto; padding-bottom: 6px; scrollbar-width: thin; }
.days a { flex: none; font-family: var(--f-mono); font-size: 0.75rem; padding: 3px 8px; border: 1px solid var(--rule);
  border-radius: 4px; background: var(--sheet); color: var(--ink); text-decoration: none; }
.days a[aria-current="true"] { background: var(--accent); border-color: var(--accent); color: var(--sheet); }
.list { list-style: none; margin: 0; padding: 0; display: grid; gap: 8px; }
.item { background: var(--sheet); border: 1px solid var(--rule); border-radius: 8px; padding: 10px 12px;
  display: grid; grid-template-columns: 3.2rem 1fr; gap: 10px; }
.score { font-family: var(--f-mono); font-weight: 700; font-size: 1.1rem; text-align: center; align-self: start;
  border-radius: 6px; padding: 2px 0; background: var(--accent-soft); color: var(--accent); }
.score.high { background: var(--hi-soft); color: var(--hi); }
.score.none { font-size: 0.7rem; font-weight: 400; color: var(--muted); background: transparent; border: 1px dashed var(--rule); }
.item > div { min-width: 0; overflow-wrap: anywhere; }
main, .days { min-width: 0; }
.title { font-weight: 700; color: var(--ink); text-decoration: none; }
.title:hover, .title:focus-visible { color: var(--accent); text-decoration: underline; }
.orig { color: var(--muted); font-size: 0.8rem; }
.short { margin: 4px 0 0; font-size: 0.875rem; }
.meta { color: var(--muted); font-size: 0.75rem; margin-top: 2px; white-space: pre-line; }
.back { display: inline-block; margin: 12px 0; color: var(--accent); }
.detail { background: var(--sheet); border: 1px solid var(--rule); border-radius: 8px; padding: 16px; }
.detail h2 { border: 0; margin: 0 0 4px; font-size: 1.2rem; }
.detail h3 { font-size: 0.9rem; margin: 18px 0 4px; color: var(--muted); }
.body { white-space: pre-wrap; overflow-wrap: anywhere; font-size: 0.9rem; }
.empty { color: var(--muted); }
</style>
</head>
<body>
<main>
  <h1>Jarvis ニュース</h1>
  <p class="updated">最終更新 {{UPDATED}}</p>
  <p class="note">毎朝の取り込みで集めた記事。点数（0〜100）は重要度の目安で、Jarvis の LLM が付けた。日付は取り込んだ日（横に送ると前の日）。詳細の本文は取得元の RSS が配る抜粋で、全文は元の記事で読む。</p>
  <nav class="days" id="days" aria-label="取り込んだ日"></nav>
  <div id="view"><p class="empty">読み込み中…</p></div>
</main>
<script>
"use strict";
// 記事の文字はすべて textContent で入れる（HTML として解釈しない）。
const view = document.getElementById("view");
const daysNav = document.getElementById("days");
let index = null;
const cache = {};

function el(tag, cls, text) {
  const node = document.createElement(tag);
  if (cls) node.className = cls;
  if (text !== undefined && text !== null) node.textContent = text;
  return node;
}
const WEEK = "日月火水木金土";
function dayLabel(day) {
  const d = new Date(day + "T00:00:00");
  return `${d.getMonth() + 1}月${d.getDate()}日（${WEEK[d.getDay()]}）`;
}
function hasJapanese(s) { return /[぀-ヿ一-鿿]/.test(s || ""); }

async function load(path) {
  if (!cache[path]) {
    const response = await fetch(path, { cache: "no-store" });
    if (!response.ok) throw new Error("HTTP " + response.status);
    cache[path] = await response.json();
  }
  return cache[path];
}

function scoreBox(a) {
  if (a.score === null || a.score === undefined) return el("div", "score none", "未採点");
  return el("div", a.score >= 70 ? "score high" : "score", String(a.score));
}

function item(a, day) {
  const li = el("li", "item");
  li.appendChild(scoreBox(a));
  const main = el("div");
  const link = el("a", "title", a.headline && !hasJapanese(a.title) ? a.headline : a.title);
  link.href = `#${day}/${encodeURIComponent(a.id)}`;
  main.appendChild(link);
  if (a.headline && !hasJapanese(a.title)) main.appendChild(el("div", "orig", a.title));
  main.appendChild(el("p", "short", a.short));
  main.appendChild(el("div", "meta", `${a.label}・${a.feed}` + (a.reason ? `　理由: ${a.reason}` : "")));
  li.appendChild(main);
  return li;
}

function section(title, articles, day) {
  const box = document.createDocumentFragment();
  box.appendChild(el("h2", null, title));
  const ul = el("ul", "list");
  articles.forEach(a => ul.appendChild(item(a, day)));
  box.appendChild(ul);
  return box;
}

function renderDays(current) {
  daysNav.replaceChildren();
  for (const d of index.days) {
    const a = el("a", null, `${dayLabel(d.day)} ${d.count}件`);
    a.href = `#${d.day}`;
    a.setAttribute("aria-current", d.day === current ? "true" : "false");
    daysNav.appendChild(a);
    if (d.day === current) requestAnimationFrame(() => a.scrollIntoView({ block: "nearest", inline: "center" }));
  }
}

function renderDay(data) {
  const day = data.day;
  const list = data.articles;           // 点数の高い順、未採点は後ろ（news_rank.ranked）
  const frag = document.createDocumentFragment();
  const scored = list.filter(a => a.score !== null && a.score !== undefined);
  frag.appendChild(el("p", "note", `${dayLabel(day)}に取り込んだ ${list.length} 件（採点済み ${scored.length} 件）`));
  if (scored.length) frag.appendChild(section(`この日の上位 ${Math.min(index.top, scored.length)} 件`, scored.slice(0, index.top), day));
  const known = new Set(index.categories.map(c => c[0]));
  const groups = index.categories.concat([...new Set(list.map(a => a.category))].filter(c => !known.has(c)).map(c => [c, c || "分類なし"]));
  for (const [slug, label] of groups) {
    const rows = list.filter(a => a.category === slug);
    if (rows.length) frag.appendChild(section(`${label}（${rows.length} 件）`, rows, day));
  }
  view.replaceChildren(frag);
}

function renderDetail(day, a) {
  const frag = document.createDocumentFragment();
  const back = el("a", "back", `← ${dayLabel(day)}の一覧`);
  back.href = `#${day}`;
  frag.appendChild(back);
  const box = el("article", "detail");
  box.appendChild(el("h2", null, a.title));
  if (a.headline) box.appendChild(el("div", "orig", a.headline));
  const top = el("div", "item");
  top.style.border = "0"; top.style.padding = "8px 0";
  top.appendChild(scoreBox(a));
  top.appendChild(el("div", "meta", (a.reason ? `点数の理由: ${a.reason}\n` : "") +
    `${a.label}・${a.feed}・取り込み ${a.created_at.replace("T", " ").slice(0, 16)}`));
  box.appendChild(top);
  box.appendChild(el("h3", null, "要約"));
  box.appendChild(el("p", "body", a.summary || "（要約なし）"));
  box.appendChild(el("h3", null, "取得元から得た本文"));
  box.appendChild(el("p", "body", a.body || "（本文なし）"));
  if (a.url) {
    const link = el("a", "back", "元の記事を開く ↗");
    link.href = a.url; link.target = "_blank"; link.rel = "noopener noreferrer";
    box.appendChild(link);
  }
  frag.appendChild(box);
  view.replaceChildren(frag);
  window.scrollTo(0, 0);
}

async function route() {
  try {
    index = index || await load("data/index.json");
    if (!index.days.length) { view.replaceChildren(el("p", "empty", "取り込んだニュースはまだありません。")); return; }
    const [rawDay, rawId] = location.hash.slice(1).split("/");
    const day = index.days.some(d => d.day === rawDay) ? rawDay : index.days[0].day;
    renderDays(day);
    const data = await load(`data/${day}.json`);
    const id = rawId ? decodeURIComponent(rawId) : "";
    const found = id && data.articles.find(a => a.id === id);
    if (found) renderDetail(day, found); else renderDay(data);
  } catch (e) {
    view.replaceChildren(el("p", "empty", "ニュースのデータを読めませんでした（" + e.message + "）。"));
  }
}
window.addEventListener("hashchange", route);
route();
</script>
</body>
</html>
"""


def main(argv: list[str]) -> int:
    out = Path(argv[argv.index("--out") + 1]) if "--out" in argv else None
    print(f"ニュースのページ: {build(out)}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
