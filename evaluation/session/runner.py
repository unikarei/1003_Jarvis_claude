"""T-2401: 会話のハーネスのセッション化と引き継ぎの評価（../docs/spec.md §3.4.12 G、testing §19）。

架空の会話を一時の DB・記憶フォルダで本物の orchestrator.handle に通し、機械判定（must_all・must_any・must_not）で採点する。
  long:    4〜10 往復前の話を参照する長い会話
  switch:  会話の途中でハーネスを強制的に失敗させ（claude → codex → ollama）、文脈が続くか
  note:    その場で覚えてと頼んだ内容を、次の会話で思い出せるか（summarize=false は要約の前に次の会話を始める）
  explain: ナレッジ・記憶の仕組みを聞かれて説明できるか
記憶にないことの作り話は evaluation.memory --kind absent で測る。
最初の文字までの時間は latency の first_text（逐次表示の最初の文字）を記録する。claude・codex は利用枠を使う。
"""
from __future__ import annotations

import argparse
import contextlib
from datetime import datetime
import json
import os
from pathlib import Path
import tempfile
import time
from unittest.mock import patch

SCENARIOS = Path(__file__).with_name("scenarios.json")
KINDS = ("long", "switch", "note", "explain")
HARNESSES = ("ollama", "claude", "codex")


def load_scenarios(path: Path = SCENARIOS) -> dict:
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    if data.get("fictional") is not True or not data.get("scenarios"):
        raise ValueError("架空の評価セットであることと scenarios が必要です")
    ids = set()
    for item in data["scenarios"]:
        if item.get("id") in ids or item.get("kind") not in KINDS:
            raise ValueError(f"シナリオ ID または種類が不正です: {item.get('id')}")
        ids.add(item["id"])
        conversations = item.get("conversations") or []
        if not conversations or not all(c.get("turns") for c in conversations):
            raise ValueError(f"会話と発言が必要です: {item['id']}")
        if not any(t.get("checks") for c in conversations for t in c["turns"]):
            raise ValueError(f"判定のある発言が必要です: {item['id']}")
        for c in conversations:
            for t in c["turns"]:
                if set(t.get("checks", {})) - {"must_all", "must_any", "must_not"}:
                    raise ValueError(f"未対応の判定です: {item['id']}")
                if set(t.get("fail", [])) - set(HARNESSES):
                    raise ValueError(f"未知のハーネスです: {item['id']}")
    return data


@contextlib.contextmanager
def forced_failures(failed: set[str]):
    """failed のハーネスのプロバイダーを「利用枠の上限」で失敗させる（切り替えの評価）。"""
    import llm
    import orchestrator
    real = orchestrator.run_with_harnesses
    providers = {p: h for h, ps in orchestrator.HARNESS_PROVIDERS.items() for p in ps}

    def run_with_failures(run, ctx, kind):
        def wrapped(provider, stream):
            if providers.get(provider) in failed:
                raise llm.LLMUnavailable(f"{provider}: usage limit reached（評価の強制失敗）", "limit")
            return run(provider, stream)
        return real(wrapped, ctx, kind)
    with patch.object(orchestrator, "run_with_harnesses", run_with_failures):
        yield


def _end_conversation(ctx, summarize_now: bool) -> int:
    """本番と同じ orchestrator.end_session で会話を終える（裏の要約のプロセスは起動せず、ここで要約する）。"""
    import orchestrator
    from memory import store, summarize
    with patch.object(summarize, "start_detached", lambda: None):
        orchestrator.end_session(ctx)
    store.flush()
    return summarize.summarize_pending() if summarize_now else 0


def run_scenario(item: dict, harness: str, *, model: str = "gemma4:12b") -> dict:
    import latency
    import llm
    import orchestrator
    from ckc.models import Speaker
    from evaluation import EVAL_OLLAMA_OPTIONS
    from evaluation.conversation.runner import harness_only, ollama_only
    from evaluation.memory.runner import check_response, isolated_scenario
    from memory import store
    counter = [0]
    rows, setup_error = [], None
    order = tuple(item["order"]) if item.get("order") else None
    with tempfile.TemporaryDirectory(prefix="jarvis-session-eval-") as temporary:
        root = Path(temporary)
        if harness == "ollama" and not order:
            answer = ollama_only(counter, True)
        else:
            answer = harness_only(counter, harness, order)
        with isolated_scenario(root, model), answer, llm.ollama_options(EVAL_OLLAMA_OPTIONS):
            for c_index, conversation in enumerate(item["conversations"]):
                ctx = orchestrator.Context(terminal=False, harness=order[0] if order else harness, channel="text")
                ctx.review = lambda *args, **kwargs: "n"
                ctx.stream = lambda _chunk: None     # 逐次表示の最初の文字の時刻（first_text）を取る
                for t_index, turn in enumerate(conversation["turns"]):
                    response, skill, error, ok, used, private = "", "", None, False, None, False
                    turn_latency = latency.start("text")
                    start = time.perf_counter()
                    try:
                        with forced_failures(set(turn.get("fail", []))):
                            outcome = orchestrator.handle(turn["text"], ctx, ask=lambda *a, **k: "n", use_llm=False)
                        response, skill, error, ok = outcome.text, outcome.skill, outcome.error, outcome.ok
                        used = (outcome.llm or {}).get("harness")
                        private = bool(getattr(outcome, "private", False))
                    except Exception as failure:  # noqa: BLE001  評価は続ける
                        error = f"{type(failure).__name__}: {failure}"
                    seconds = time.perf_counter() - start
                    record = latency.finish(turn_latency, path=root / "latency.jsonl") or {}
                    row = {"conversation": c_index + 1, "turn": t_index + 1, "text": turn["text"], "response": response,
                           "skill": skill, "error": error, "harness": used, "seconds": round(seconds, 3),
                           "first_text_ms": (record.get("stages") or {}).get("first_text"), "judged": bool(turn.get("checks"))}
                    if turn.get("checks"):
                        result = check_response(turn["checks"], response, kind=item["kind"], ok=ok and error is None)
                        if turn.get("expect_harness"):
                            result["checks"]["harness"] = used == turn["expect_harness"]
                            result["passed"] = result["passed"] and result["checks"]["harness"]
                        row.update(result)
                    rows.append(row)
                    if not private:
                        orchestrator.add_turn(ctx, Speaker.OWNER, turn["text"])
                        if ok and error is None and response:
                            orchestrator.add_turn(ctx, Speaker.AI, response)
                try:
                    _end_conversation(ctx, bool(item.get("summarize")) and c_index + 1 < len(item["conversations"]))
                except Exception as failure:  # noqa: BLE001
                    setup_error = setup_error or f"要約: {type(failure).__name__}: {failure}"
                store.flush()
    judged = [r for r in rows if r["judged"]]
    return {"id": item["id"], "kind": item["kind"], "harness": harness, "order": list(order) if order else None,
            "passed": bool(judged) and all(r["passed"] for r in judged), "turns": rows, "llm_calls": counter[0],
            "setup_error": setup_error}


def _median(values):
    values = sorted(v for v in values if v is not None)
    if not values:
        return None
    mid = len(values) // 2
    return values[mid] if len(values) % 2 else (values[mid - 1] + values[mid]) / 2


def summarize_results(rows: list[dict]) -> dict:
    by_kind = {}
    for kind in KINDS:
        group = [r for r in rows if r["kind"] == kind]
        if not group:
            continue
        checked = [t for r in group for t in r["turns"] if t["judged"]]
        by_kind[kind] = {"scenarios": len(group), "passed": sum(r["passed"] for r in group),
                         "pass_rate": sum(r["passed"] for r in group) / len(group),
                         "questions": len(checked), "questions_passed": sum(t["passed"] for t in checked)}
    turns = [t for r in rows for t in r["turns"]]
    return {"scenarios": len(rows), "passed": sum(r["passed"] for r in rows),
            "pass_rate": sum(r["passed"] for r in rows) / len(rows) if rows else None, "by_kind": by_kind,
            "hallucination_count": sum(bool(t.get("hallucination")) for t in turns),
            "errors": sum(bool(t["error"]) for t in turns),
            "first_text_ms_median": _median(t["first_text_ms"] for t in turns),
            "seconds_median": _median(t["seconds"] for t in turns),
            "failures": [{"id": r["id"], "conversation": t["conversation"], "turn": t["turn"], "response": t["response"],
                          "harness": t["harness"], "failed_checks": [k for k, v in t["checks"].items() if not v],
                          "error": t["error"]}
                         for r in rows for t in r["turns"] if t["judged"] and not t["passed"]]}


def write_results(result: dict, output: Path) -> tuple[Path, Path]:
    output = Path(output)
    output.mkdir(parents=True, exist_ok=True)
    stem = f"{result['stamp']}-session-{result['harness']}"
    json_path, md_path = output / f"{stem}.json", output / f"{stem}.md"
    json_path.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    s = result["summary"]
    lines = [f"# セッションの評価 {result['stamp']}", "", f"ハーネス: {result['harness']} / 版: {result.get('label') or '-'}"
             f" / 選択: {result['selected']} / 全セット: {result['total']}",
             "合格率は判定のある発言がすべて合格した会話の割合。switch は指定の順番（claude → codex → ollama）で強制失敗させる。", "",
             "|種類|合格/件数|合格率|判定の発言（合格/件数）|", "|---|---:|---:|---:|"]
    for kind, v in {"全体": s, **s["by_kind"]}.items():
        q = f"{v['questions_passed']}/{v['questions']}" if "questions" in v else "-"
        lines.append(f"|{kind}|{v['passed']}/{v['scenarios']}|{v['pass_rate']:.1%}|{q}|")
    lines += ["", f"最初の文字まで（中央値）: {s['first_text_ms_median']} ms / 1 発言（中央値）: {s['seconds_median']} 秒"
              f" / 作り話の表現: {s['hallucination_count']} 件 / 実行の失敗: {s['errors']} 件", ""]
    for f in s["failures"]:
        lines += [f"- 不合格 {f['id']} / 会話 {f['conversation']} 発言 {f['turn']}（{f['harness']}）: {', '.join(f['failed_checks'])}",
                  f"  - 応答: {f['response'][:300]}", f"  - エラー: {f['error']}"]
    md_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return json_path, md_path


def parser():
    p = argparse.ArgumentParser(prog="python -m evaluation.session", description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--harness", choices=HARNESSES, default="ollama", help="答えのハーネス（switch は claude → codex → ollama）")
    p.add_argument("--kind", choices=KINDS, action="append", help="複数指定可")
    p.add_argument("--limit", type=int)
    p.add_argument("--scenarios", type=Path, default=SCENARIOS)
    p.add_argument("--model", default="gemma4:12b")
    p.add_argument("--label", default="")
    p.add_argument("--output-dir", type=Path, default=Path("data/eval/session"))
    return p


def main(argv=None):
    p = parser()
    args = p.parse_args(argv)
    data = load_scenarios(args.scenarios)
    items = [s for s in data["scenarios"] if not args.kind or s["kind"] in args.kind]
    if args.limit:
        items = items[:args.limit]
    if not items:
        p.error("対象シナリオがありません")
    for key in ("ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN", "OPENAI_API_KEY"):
        os.environ.pop(key, None)            # サブスクリプションのログインだけを使う（ADR-0020）
    rows = []
    for item in items:
        print(f"評価中: {item['id']}（{args.harness}）", flush=True)
        rows.append(run_scenario(item, args.harness, model=args.model))
        print(f"  → {'合格' if rows[-1]['passed'] else '不合格'}", flush=True)
    result = {"stamp": datetime.now().strftime("%Y%m%d-%H%M%S"), "harness": args.harness, "label": args.label,
              "model": args.model, "selected": len(items), "total": len(data["scenarios"]), "kind_filter": args.kind,
              "rows": rows, "summary": summarize_results(rows)}
    for path in write_results(result, args.output_dir):
        print(path)
    return int(any(r["setup_error"] for r in rows))


if __name__ == "__main__":
    raise SystemExit(main())
