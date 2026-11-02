"""評価のレポート（../docs/spec.md §3.4.10、ADR-0030、T-1411）。

data/eval/reports/<日時>.json: 全部の数値と問ごとの結果（答えの本文は先頭 ANSWER_CHARS 字まで）
data/eval/reports/<日時>.md:   集計の表、失敗の多いデータソース・形、失敗した問の一覧と落ちた段階、設定の値
"""

import copy
import json
from datetime import datetime
from pathlib import Path

import evaluation

ANSWER_CHARS = 300
FAILED_AT = {"route": "振り分け", "search": "検索", "answer": "答え"}
COLUMNS = (("件数", "count"), ("振り分け", "route_acc"), ("recall@1", "recall@1"), ("recall@3", "recall@3"),
           ("recall@8", "recall@8"), ("recall_eff", "recall_eff"), ("MRR", "mrr"), ("要点", "facts_hit"),
           ("facts_hit_strict", "facts_hit_strict"), ("「ない」", "none_rate"), ("秒 p50", "seconds_p50"), ("秒 p95", "seconds_p95"),
           ("score", "score"), ("score_retrieval", "score_retrieval"))


def reports_dir() -> Path:
    return evaluation.EVAL_DIR / "reports"


def for_json(result: dict) -> dict:
    """保存する形（答えの本文を先頭 ANSWER_CHARS 字に切る）。"""
    data = copy.deepcopy(result)
    for r in data.get("items", []):
        if isinstance(r.get("answer"), str):
            r["answer"] = r["answer"][:ANSWER_CHARS]
    return data


def _cell(value) -> str:
    if value is None:
        return "-"
    if isinstance(value, float):
        return f"{value:.3f}"
    return str(value)


def _table(rows: dict[str, dict], label: str) -> list[str]:
    head = f"| {label} | " + " | ".join(c for c, _ in COLUMNS) + " |"
    lines = [head, "|" + "---|" * (len(COLUMNS) + 1)]
    for name, agg in rows.items():
        lines.append(f"| {name} | " + " | ".join(_cell(agg.get(k)) for _, k in COLUMNS) + " |")
    return lines


def _escape(text) -> str:
    return (text or "").replace("|", "｜").replace("\n", " ")


def _fact_matches(r: dict) -> str:
    """要点ごとの一致の方法（「要点: exact」の形。外れた要点は「×」）。答えを作っていなければ「-」。"""
    matches = r.get("fact_match")
    if not matches:
        return "-"
    return "<br>".join(f"{_escape(f)}: {m or '×'}" for f, m in zip(r.get("key_facts") or [], matches))


def _worst(rows: dict[str, dict], key: str) -> list[str]:
    """失敗の多い順（総合点の低い順）。"""
    ranked = sorted(rows.items(), key=lambda kv: (kv[1].get(key) if kv[1].get(key) is not None else 0, kv[0]))
    return [f"- {name}: {key} {_cell(agg.get(key))}（{agg['count']} 問）" for name, agg in ranked]


def to_markdown(result: dict) -> str:
    s = result["summary"]
    key = "score" if result.get("answer") else "score_retrieval"
    lines = [f"# 評価レポート（{result.get('qa_version', '-')}）", "",
             f"- 実行: {result.get('started_at')} 〜 {result.get('finished_at')}",
             f"- 組: {result.get('split', 'all')}、問の数: {s['overall']['count']}",
             f"- 答え: {'作る' if result.get('answer') else '作らない（振り分けと検索だけ）'}、"
             f"プロバイダー: {result.get('provider')}",
             f"- LLM の呼び出し: {result.get('llm_calls')} 回（見込み {result.get('llm_calls_estimate')} 回"
             + (f"、上限 {result['max_llm_calls']} 回" if result.get("max_llm_calls") else "") + "）"
             + (f"、判定 {result.get('judge_calls', 0)} 回（{result['judge']}）" if result.get("judge") else "")]
    if result.get("stopped"):
        lines.append(f"- **途中で止めました**（{result['stopped']}）。集計は終わった問だけ")
    if result.get("source_db"):
        lines.append(f"- 元の DB: {result['source_db']}（コピー {result.get('copied_at') or '-'}）")
    lines += ["", f"採否に使う点: `{key}`（score = 0.2×振り分け + 0.3×recall_eff + 0.5×要点、"
                  "score_retrieval = 0.4×振り分け + 0.6×recall_eff。recall@8 は診断用）", "",
              "要点の一致: 文字の一致（exact）→ 2 文字の組の重なり（bigram）→ ローカル LLM の判定（judge）。"
              "facts_hit_strict は文字の一致だけの割合", "",
              "## 集計", ""]
    lines += _table({"全体": s["overall"]}, "")
    for title, name in (("組別", "by_split"), ("データソース別", "by_source"), ("形別", "by_style")):
        lines += ["", f"### {title}", ""] + _table(s[name], title)
    stages = s["overall"].get("stages") or {}
    if stages:
        lines += ["", "### 段階ごとの時間（ミリ秒）", "", "| 段階 | p50 | p95 |", "|---|---|---|"]
        lines += [f"| {st} | {v['p50']} | {v['p95']} |" for st, v in stages.items()]
    lines += ["", "## 失敗の多いデータソース", ""] + _worst(s["by_source"], key)
    lines += ["", "## 失敗の多い形", ""] + _worst(s["by_style"], key)
    failed = [r for r in result["items"] if r.get("failed_at")]
    lines += ["", f"## 失敗した問（{len(failed)} 問）", ""]
    if failed:
        lines += ["| ID | 落ちた段階 | 振り分け先 | 順位 | 要点 | 要点ごとの一致 | データソース | 形 | 質問 |",
                  "|---|---|---|---|---|---|---|---|---|"]
        for r in failed:
            stage = FAILED_AT.get(r["failed_at"], r["failed_at"]) + (f"（{r['error']}）" if r.get("error") else "")
            question = _escape(r.get("question"))
            lines.append(f"| {r['id']} | {stage} | {r.get('skill') or '-'} | {_cell(r.get('rank'))} | "
                         f"{_cell(r.get('facts_hit'))} | {_fact_matches(r)} | {r['source']} | {r['style']} | "
                         f"{question} |")
    else:
        lines.append("なし")
    lines += ["", "## 設定の値", "", "```json", json.dumps(result.get("tuning", {}), ensure_ascii=False, indent=2),
              "```", ""]
    return "\n".join(lines)


def write(result: dict, out_dir: Path | None = None, stamp: str | None = None) -> tuple[Path, Path]:
    """<日時>.json と <日時>.md を書いて、そのパスを返す。

    日時はマイクロ秒まで。同じ名前がすでにあれば -2, -3 … を付けて別名にする（上書きしない。各実行を別に残す）。
    """
    out_dir = Path(out_dir or reports_dir())
    out_dir.mkdir(parents=True, exist_ok=True)
    stamp = stamp or datetime.now().strftime("%Y%m%d-%H%M%S-%f")
    n = 1
    while True:
        name = stamp if n == 1 else f"{stamp}-{n}"
        json_path, md_path = out_dir / f"{name}.json", out_dir / f"{name}.md"
        if not md_path.exists():
            try:
                f = open(json_path, "x", encoding="utf-8")      # 名前の確保（同時の実行とも衝突しない）
            except FileExistsError:
                pass
            else:
                break
        n += 1
    with f:
        json.dump(for_json(result), f, ensure_ascii=False, indent=2)
        f.write("\n")
    with open(md_path, "x", encoding="utf-8") as f:
        f.write(to_markdown(result))
    return json_path, md_path
