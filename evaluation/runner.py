"""評価の実行と指標（../docs/spec.md §3.4.10、ADR-0030、T-1411）。

各問を文字の会話と同じ経路で処理する: 振り分け（orchestrator.route、LLM は使わない）→ knowledge-answer なら
順位の測定（knowledge_engine.search）と答え（knowledge_engine.answer）。すべて tuning.use(t) の中で行う。
ほかのスキルに振り分けられた問は「振り分けの失敗」とし、ハンドラーは呼ばない（書き込みのスキルを実行しない）。
段階ごとの時間は latency.Turn で測り、評価のファイル data/eval/latency/<日時>.jsonl に書く（本番の記録には書かない）。
"""

import sys
import time
from datetime import datetime
from pathlib import Path

import evaluation
import knowledge_engine
import latency
import orchestrator
import tuning as tuning_lib
from evaluation.qa import SPLITS, normalize
from tuning import Tuning

ANSWER_SKILL = orchestrator.DEFAULT_SKILL
PROVIDERS = ("ollama", "claude_warm", "claude_cli")    # 有料の API（anthropic_api）は使わない
LIMITED_PROVIDERS = ("claude_warm", "claude_cli")      # サブスクリプションの利用量を使う（上限をかける）
DEFAULT_MAX_LLM_CALLS = 200
RANK_DEPTH = 8                                         # 順位を測る深さ（max(これ, search_limit)）
RECALL_KS = (1, 3, 8)
# 「ない」と答えたとみなす言い回し（正規化して部分一致）
NONE_PHRASES = ("見つかりません", "見つからない", "ありません", "分かりません", "わかりません", "資料にない")
STAGES = ("route", "search", "llm_ready", "first_text", "done")


class EvalError(RuntimeError):
    """評価を始められない（許可していないプロバイダー・呼び出しの見込みが上限を超える）。"""


class LLMCallLimit(RuntimeError):
    """実行中に LLM の呼び出しが上限に達した。"""


def facts_hit(answer: str, facts: list[str]) -> float:
    """答えに含まれた要点の割合（空白・全角半角・句読点を正規化して部分一致）。"""
    norm = normalize(answer)
    wanted = [normalize(f) for f in facts or []]
    wanted = [f for f in wanted if f]
    return sum(1 for f in wanted if f in norm) / len(wanted) if wanted else 0.0


def said_none(answer: str) -> bool:
    norm = normalize(answer)
    return any(normalize(p) in norm for p in NONE_PHRASES)


def estimate_llm_calls(items: list[dict], answer: bool) -> int:
    """LLM を呼ぶ回数の見込み（答えを作る問の数。振り分けで外れる問も数える上限の見積もり）。"""
    return len(items) if answer else 0


def latency_dir() -> Path:
    return evaluation.EVAL_DIR / "latency"


def _blank(item: dict, answer: bool) -> dict:
    """欠けた値の既定（振り分けの失敗・例外のときに残る値）。"""
    r = {"id": item.get("id"), "question": item.get("question"), "knowledge_id": item.get("knowledge_id"),
         "key_facts": item.get("key_facts") or [], "source": item.get("source", "unknown"),
         "style": item.get("style", "unknown"), "split": item.get("split", "unknown"),
         "skill": None, "routed": False, "rank": None, "recall_eff": 0}
    r.update({f"recall@{k}": 0 for k in RECALL_KS})
    r.update({"facts_hit": 0.0 if answer else None, "said_none": False if answer else None, "answer": None,
              "seconds": 0.0, "stages": {}, "failed_at": None, "error": None})
    return r


def _evaluate(item: dict, t: Tuning, answer: bool, call_llm) -> dict:
    r = _blank(item, answer)
    question = item["question"]
    stage = "route"
    try:
        route = orchestrator.route(question, orchestrator.Context(), use_llm=False)
        latency.mark("route")
        latency.set("skill", route.skill)
        r["skill"] = route.skill
        r["routed"] = route.skill == ANSWER_SKILL
        if not r["routed"]:
            r["failed_at"] = "route"
            return r
        stage = "search"
        hits = knowledge_engine.search(question, limit=max(RANK_DEPTH, t.search_limit))
        latency.mark("search")
        ids = [h.knowledge_id for h in hits]
        rank = ids.index(item["knowledge_id"]) + 1 if item.get("knowledge_id") in ids else None
        r["rank"] = rank
        r.update({f"recall@{k}": int(rank is not None and rank <= k) for k in RECALL_KS})
        r["recall_eff"] = int(rank is not None and rank <= t.search_limit)
        if not answer:
            return r
        stage = "answer"
        text = call_llm(question)
        r["answer"] = text
        r["facts_hit"] = facts_hit(text, r["key_facts"])
        r["said_none"] = said_none(text)
        return r
    except LLMCallLimit:
        raise
    except Exception as e:  # noqa: BLE001  1 問の失敗で評価を止めない
        r["failed_at"], r["error"] = stage, type(e).__name__
        return r


def _failed_at(r: dict, answer: bool) -> str | None:
    """順に最初に落ちた段階: 振り分け → 検索（根拠が答えの文脈に渡らない）→ 答え（要点をすべて含まない）。"""
    if r["failed_at"]:
        return r["failed_at"]
    if not r["routed"]:
        return "route"
    if not r["recall_eff"]:
        return "search"
    if answer and (r["facts_hit"] or 0) < 1:
        return "answer"
    return None


def run(items: list[dict], tuning: Tuning = Tuning(), provider: str = "ollama", answer: bool = True,
        max_llm_calls: int = DEFAULT_MAX_LLM_CALLS, log=None, latency_path: Path | None = None) -> dict:
    """items（評価セットの問）を評価して、問ごとの結果と集計を返す。log は見込みなどの表示先（既定は標準エラー）。"""
    if provider not in PROVIDERS:
        raise EvalError(f"評価に使えないプロバイダーです: {provider}（{' / '.join(PROVIDERS)} だけ。有料の API は使わない）")
    log = log or (lambda msg: print(msg, file=sys.stderr))
    limited = answer and provider in LIMITED_PROVIDERS
    estimate = estimate_llm_calls(items, answer)
    log(f"LLM の呼び出しの見込み: {estimate} 回（{provider}"
        + (f"、上限 {max_llm_calls} 回）" if limited else "、上限なし）" if answer else "、答えを作らない）"))
    if limited and estimate > max_llm_calls:
        raise EvalError(f"LLM の呼び出しの見込み（{estimate} 回）が上限（{max_llm_calls} 回）を超えるため始めません")

    started = datetime.now()
    latency_path = Path(latency_path or latency_dir() / f"{started.strftime('%Y%m%d-%H%M%S-%f')}.jsonl")
    calls = 0

    def call_llm(question: str) -> str:
        nonlocal calls
        if limited and calls >= max_llm_calls:
            raise LLMCallLimit(f"LLM の呼び出しが上限（{max_llm_calls} 回）に達しました")
        calls += 1
        latency.set("provider", provider)
        first = []

        def on_text(chunk):
            if chunk and not first:
                first.append(True)
                latency.mark("first_text")
        text, _ = knowledge_engine.answer(question, provider=provider, on_text=on_text)
        return text

    results, stopped = [], None
    previous = latency.current()
    with tuning_lib.use(tuning):
        for item in items:
            turn = latency.Turn("text")
            latency.use(turn)
            began = time.monotonic()
            try:
                r = _evaluate(item, tuning, answer, call_llm)
            except LLMCallLimit as e:
                stopped = "max_llm_calls"
                log(f"止めました: {e}（{len(results)} / {len(items)} 問）")
                break
            finally:
                latency.use(previous)
            r["seconds"] = round(time.monotonic() - began, 3)
            r["failed_at"] = _failed_at(r, answer)
            record = latency.finish(turn, path=latency_path, ok=r["failed_at"] is None)
            r["stages"] = dict(record["stages"]) if record else dict(turn.stages)
            results.append(r)
    return {"started_at": started.isoformat(timespec="seconds"),
            "finished_at": datetime.now().isoformat(timespec="seconds"),
            "provider": provider, "answer": answer, "tuning": tuning.to_dict(),
            "llm_calls": calls, "llm_calls_estimate": estimate,
            "max_llm_calls": max_llm_calls if limited else None, "stopped": stopped,
            "latency_log": str(latency_path), "items": results, "summary": summarize(results, answer)}


# ---------------------------------------------------------------- 集計

def _mean(values: list) -> float | None:
    values = [v for v in values if v is not None]
    return round(sum(values) / len(values), 4) if values else None


def _aggregate(rs: list[dict], answer: bool) -> dict:
    out = {"count": len(rs), "route_acc": _mean([int(r["routed"]) for r in rs])}
    for k in RECALL_KS:
        out[f"recall@{k}"] = _mean([r[f"recall@{k}"] for r in rs])
    out["recall_eff"] = _mean([r["recall_eff"] for r in rs])
    out["mrr"] = _mean([1 / r["rank"] if r["rank"] else 0 for r in rs])
    out["facts_hit"] = _mean([r["facts_hit"] for r in rs]) if answer else None
    out["none_rate"] = _mean([int(r["said_none"]) if r["said_none"] is not None else None for r in rs]) \
        if answer else None
    seconds = [r["seconds"] for r in rs]
    out["seconds_p50"], out["seconds_p95"] = latency.percentile(seconds, 0.5), latency.percentile(seconds, 0.95)
    out["stages"] = {}
    for stage in STAGES:
        values = [r["stages"][stage] for r in rs if isinstance(r["stages"].get(stage), (int, float))]
        if values:
            out["stages"][stage] = {"p50": latency.percentile(values, 0.5), "p95": latency.percentile(values, 0.95)}
    route_acc, recall_eff = out["route_acc"] or 0, out["recall_eff"] or 0
    # 総合点（採否に使う）。recall@8 は診断用で点に入れない
    out["score"] = round(0.2 * route_acc + 0.3 * recall_eff + 0.5 * (out["facts_hit"] or 0), 4) \
        if answer and rs else None
    out["score_retrieval"] = round(0.4 * route_acc + 0.6 * recall_eff, 4) if rs else None
    return out


def _by(rs: list[dict], key: str, answer: bool, order=None) -> dict:
    groups: dict[str, list[dict]] = {}
    for r in rs:
        groups.setdefault(str(r.get(key)), []).append(r)
    keys = [k for k in (order or ()) if k in groups] + sorted(k for k in groups if k not in (order or ()))
    return {k: _aggregate(groups[k], answer) for k in keys}


def summarize(results: list[dict], answer: bool) -> dict:
    return {"overall": _aggregate(results, answer),
            "by_source": _by(results, "source", answer),
            "by_style": _by(results, "style", answer),
            "by_split": _by(results, "split", answer, SPLITS)}
