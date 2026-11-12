"""T-2103: 架空の記憶を一時領域で作り、Ollama の回答を機械採点する。

本番 DB/Vault/会話は読み込まない。合格率は全質問に合格したシナリオの割合。
absent の作り話は定義した表現の検出件数であり、意味を網羅する判定ではない。
"""
from __future__ import annotations

import argparse
from collections import Counter
from contextlib import ExitStack, contextmanager
from datetime import datetime
import importlib
import json
import os
from pathlib import Path
import re
import tempfile
import time
from unittest.mock import patch

SCENARIOS = Path(__file__).with_name("scenarios.json")
KINDS = ("continue", "decision", "preference", "temporal", "absent", "irrelevant", "retrieval")
# retrieval（Claude 追加、2026-11-12）: 正解の会話を 2〜3 週間前に置き、その後に関係のない会話を 6 回挟む。
# 「直前の会話」の欄では答えられず、要約の検索（BM25、T-2104 では意味検索）で探す力を測る。05〜08 は固有名詞を言わずに言い換えで聞く
# 長い語を先に置く。否定・仮定・引用だけの言及は、同じ節の接尾部で除外する。
CLAIMS = re.compile(r"覚えて(?:おります|います|いる)|前に(?:お)?話し?した|以前(?:に)?(?:お)?話し?した|記憶に(?:ございます|あります|ある)")
DENIAL = re.compile(
    r"^[\s」』\"']*(?:"
    r"(?:とは|と)(?:言えません|言えない|断定できません|断定できない|申し上げられません|申し上げられない)"
    r"|(?:わけでは|というわけでは|ことは)(?:ありません|ない|ございません)"
    r"|(?:かどうか|かは)(?:確認できません|確認できない|不明|わかりません|分かりません)"
    r"|(?:記憶|記録|事実)(?:は|が)(?:ありません|ない|ございません)"
    r"|(?:なら|のであれば|場合は))"
)


def hallucination_matches(response: str) -> list[str]:
    """対象表現の肯定的な出現を返す。例: 覚えているとは言えません は除外。"""
    return [m.group() for m in CLAIMS.finditer(response) if not DENIAL.search(response[m.end():])]


def check_response(checks: dict, response: str, *, kind: str, ok: bool = True) -> dict:
    found = hallucination_matches(response) if kind == "absent" else []
    results = {"execution_ok": bool(ok and response.strip()),
               "must_all": all(word in response for word in checks.get("must_all", [])),
               "must_any": not checks.get("must_any") or any(word in response for word in checks["must_any"]),
               "must_not": not any(word in response for word in checks.get("must_not", [])),
               "no_fabricated_memory": not found}
    return {"passed": all(results.values()), "checks": results, "hallucination": bool(found),
            "hallucination_matches": found}


def load_scenarios(path: Path = SCENARIOS) -> dict:
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    if data.get("fictional") is not True or not isinstance(data.get("scenarios"), list) or not data["scenarios"]:
        raise ValueError("架空の評価セットであることと scenarios が必要です")
    ids = set()
    for item in data["scenarios"]:
        if not isinstance(item.get("id"), str) or not item["id"] or item["id"] in ids or item.get("kind") not in KINDS:
            raise ValueError("シナリオ ID または種類が不正です")
        ids.add(item["id"])
        at = datetime.fromisoformat(item["evaluation_at"])
        max_history = 8 if item["kind"] == "retrieval" else 3   # retrieval は関係のない会話を挟むため多い
        if not 1 <= len(item["history"]) <= max_history or not 1 <= len(item["questions"]) <= 2:
            raise ValueError("過去会話は1〜3回（retrieval は 1〜8 回）、質問は1〜2回です")
        dates = []
        for session in item["history"]:
            date = datetime.fromisoformat(session["at"])
            if date >= at:
                raise ValueError("過去会話の日付は評価日時より前にしてください")
            dates.append(date)
            turns = session["turns"]
            if not turns or not {"owner", "ai"} <= {t["speaker"] for t in turns}:
                raise ValueError("過去会話には Owner と JARVIS が必要です")
            if any(t["speaker"] not in ("owner", "ai") or not isinstance(t["text"], str) or not t["text"].strip() for t in turns):
                raise ValueError("過去の発言が不正です")
        if dates != sorted(dates):
            raise ValueError("過去会話は日付順にしてください")
        for question in item["questions"]:
            checks = question["checks"]
            if not isinstance(question["text"], str) or not question["text"].strip() or not checks:
                raise ValueError("質問と機械判定の期待が必要です")
            if set(checks) - {"must_all", "must_any", "must_not"}:
                raise ValueError("未対応の判定です")
            if any(not isinstance(v, list) or not v or any(not isinstance(w, str) or not w.strip() for w in v) for v in checks.values()):
                raise ValueError("期待は空でない文字列のリストです")
        for pref in item.get("preferences", []):
            if not pref.get("title") or not pref.get("content"):
                raise ValueError("承認済み好みの題名と本文が必要です")
    return data


def _clock(at: str):
    fixed = datetime.fromisoformat(at)

    class FixedDatetime(datetime):
        @classmethod
        def now(cls, tz=None):
            return fixed.astimezone(tz) if tz else fixed
    return FixedDatetime


@contextmanager
def isolated_scenario(root: Path, model: str):
    """生成した一時ルートだけへ全保存先を切り替え、終了時に設定とキャッシュを復元する。"""
    import config
    import db
    import llm
    import registry
    import skills
    root = root.resolve()
    paths = {"DB_PATH": root / "jarvis.db", "MEMORY_DIR": root / "memory",
             "VAULT_ROOT": root / "vaults", "CKC_DATA_DIR": root / "ckc",
             "LATENCY_LOG": root / "latency.jsonl", "REGISTRY_JSON": root / "registry.json",
             "SKILLS_JSON": root / "skills.json"}
    for key in ("VAULT_ROOT", "CKC_DATA_DIR"):
        paths[key].mkdir()
    paths["REGISTRY_JSON"].write_text(json.dumps({"projects": {}, "resources": {}}), encoding="utf-8")
    skill_data = {name: {"status": "active", "permission": "read", "description": "評価用の読み取り専用スキル",
                         "entry": {"repo": "repo-1003-jarvis-claude", "function": function},
                         "inputs": {"question": {"type": "string", "required": True}}}
                  for name, function in (("general-chat", "orchestrator._chat"), ("knowledge-answer", "knowledge_engine.answer"))}
    paths["SKILLS_JSON"].write_text(json.dumps({"skills": skill_data}), encoding="utf-8")
    env = {"JARVIS_" + key: str(value) for key, value in paths.items()}
    env.update(JARVIS_MEMORY="1", JARVIS_LLM_PROVIDER="ollama", JARVIS_HARNESS="ollama",
               JARVIS_CHAT_MODEL=model, JARVIS_CAPTURE_ON_END="0", JARVIS_OLLAMA_URL="http://127.0.0.1:11434")
    with ExitStack() as stack:
        stack.enter_context(patch.dict(os.environ, env))
        for key, value in paths.items():
            stack.enter_context(patch.object(config, key, value))
        for key, value in {"MEMORY_ENABLED": True, "LLM_PROVIDER": "ollama", "HARNESS": "ollama",
                           "CHAT_MODEL": model, "OLLAMA_URL": env["JARVIS_OLLAMA_URL"]}.items():
            stack.enter_context(patch.object(config, key, value))
        stack.enter_context(patch.object(db, "DB_PATH", paths["DB_PATH"]))
        stack.enter_context(patch.object(llm, "CHAT_MODEL", model))
        stack.enter_context(patch.object(llm, "OLLAMA_URL", env["JARVIS_OLLAMA_URL"]))
        stack.enter_context(patch.object(registry, "_cache", None))
        stack.enter_context(patch.object(registry, "_loaded", False))
        stack.enter_context(patch.object(skills, "_cache", None))
        db.init_db()
        yield paths


def seed_history(item: dict, *, no_summary: bool) -> int:
    """正本 JSONL も索引も store 経由で同じ架空日時を記録する。"""
    import db
    from memory import store, summarize
    for index, past in enumerate(item["history"]):
        date = datetime.fromisoformat(past["at"])
        sid = f"sess_{date:%Y%m%d_%H%M%S}_{index:04x}"
        with patch.object(store, "datetime", _clock(past["at"])):
            for turn in past["turns"]:
                store.log_turn(sid, "text", turn["speaker"], turn["text"])
            store.end_session(sid)
    for pref in item.get("preferences", []):
        db.insert_node("Knowledge", title=pref["title"], content=pref["content"],
                       meta={"kind": "Preference", "status": "active"})
    if no_summary:
        return 0
    with patch.object(store, "datetime", _clock(item["evaluation_at"])):
        count = summarize.summarize_pending()
    if count != len(item["history"]):
        raise RuntimeError("過去会話の要約に失敗しました（全会話の要約が必要です）")
    return count


def run_scenario(item: dict, memory_enabled: bool, *, model="gemma4:12b", no_summary=False) -> dict:
    import config
    import llm
    import orchestrator
    from ckc.models import Speaker
    from evaluation import EVAL_OLLAMA_OPTIONS
    from evaluation.conversation.runner import ollama_only
    from memory import store
    recall_module = importlib.import_module("memory.recall")
    counter = [0]
    rows = []
    setup_error = None
    with tempfile.TemporaryDirectory(prefix="jarvis-memory-eval-") as temporary:
        with isolated_scenario(Path(temporary), model), ollama_only(counter, True), llm.ollama_options(EVAL_OLLAMA_OPTIONS):
            try:
                seed_history(item, no_summary=no_summary)
            except Exception as error:
                setup_error = f"{type(error).__name__}: {error}"
            ctx = orchestrator.Context(terminal=False, harness="ollama")
            ctx.review = lambda *args, **kwargs: "n"
            with patch.dict(os.environ, {"JARVIS_MEMORY": "1" if memory_enabled else "0"}), \
                    patch.object(config, "MEMORY_ENABLED", memory_enabled), \
                    patch.object(store, "datetime", _clock(item["evaluation_at"])), \
                    patch.object(recall_module, "datetime", _clock(item["evaluation_at"])):
                for index, question in enumerate(item["questions"]):
                    timings = []
                    real_recall = recall_module.recall

                    def timed_recall(*args, **kwargs):
                        start = time.perf_counter()
                        try:
                            return real_recall(*args, **kwargs)
                        finally:
                            timings.append(time.perf_counter() - start)

                    response, skill, error, ok = "", "", setup_error, False
                    start = time.perf_counter()
                    if not setup_error:
                        try:
                            with patch.object(recall_module, "recall", timed_recall):
                                outcome = orchestrator.handle(question["text"], ctx, ask=lambda *a, **k: "n", use_llm=False)
                            response, skill, error, ok = outcome.text, outcome.skill, outcome.error, outcome.ok
                        except Exception as failure:
                            error = f"{type(failure).__name__}: {failure}"
                    result = check_response(question["checks"], response, kind=item["kind"], ok=ok and error is None)
                    rows.append({"turn": index + 1, "question": question["text"], "response": response, "skill": skill,
                                 "error": error, "recall_seconds": sum(timings), "recall_calls": len(timings),
                                 "seconds": time.perf_counter() - start, **result})
                    orchestrator.add_turn(ctx, Speaker.OWNER, question["text"])
                    if ok and response:
                        orchestrator.add_turn(ctx, Speaker.AI, response)
    return {"id": item["id"], "kind": item["kind"], "memory": memory_enabled,
            "passed": all(row["passed"] for row in rows), "turns": rows, "llm_calls": counter[0],
            "setup_error": setup_error}


def summarize_results(rows: list[dict]) -> dict:
    summary = {}
    for mode, enabled in (("with_memory", True), ("without_memory", False)):
        group = [row for row in rows if row["memory"] == enabled]
        by_kind = {}
        for kind in KINDS:
            selected = [row for row in group if row["kind"] == kind]
            if selected:
                by_kind[kind] = {"scenarios": len(selected), "passed": sum(r["passed"] for r in selected),
                                 "pass_rate": sum(r["passed"] for r in selected) / len(selected)}
        turns = [turn for row in group for turn in row["turns"]]
        summary[mode] = {"scenarios": len(group), "passed": sum(row["passed"] for row in group),
                         "pass_rate": sum(row["passed"] for row in group) / len(group) if group else None,
                         "by_kind": by_kind, "hallucination_count": sum(t["hallucination"] for t in turns),
                         "hallucination_scenarios": sum(any(t["hallucination"] for t in r["turns"]) for r in group),
                         "recall_seconds": sum(t["recall_seconds"] for t in turns),
                         "recall_calls": sum(t["recall_calls"] for t in turns),
                         "failures": [{"id": r["id"], "kind": r["kind"], "turn": t["turn"], "response": t["response"],
                                       "failed_checks": [k for k, v in t["checks"].items() if not v], "error": t["error"]}
                                      for r in group for t in r["turns"] if not t["passed"]]}
    return summary


def write_results(result: dict, output: Path) -> tuple[Path, Path]:
    output = Path(output)
    output.mkdir(parents=True, exist_ok=True)
    stem = f"{result['stamp']}-memory"
    json_path, md_path = output / f"{stem}.json", output / f"{stem}.md"
    json_path.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    lines = [f"# 記憶評価 {result['stamp']}", "", f"モデル: {result['model']} / Ollama のみ / 要約: {result['summary_enabled']}",
             f"部分実行: {result['partial']} / 選択: {result['selected']} / 全セット: {result['total']}",
             "合格率は全質問が合格したシナリオの割合。想起時間は生成を含まない recall 呼出しの合計。", "",
             "作り話は absent の対象表現を含む返事の件数（1返事1件）。否定・仮定の接尾部を除外する表現ベースの判定。",
             f"対象: {CLAIMS.pattern}", f"否定除外: {DENIAL.pattern}", "",
             "|記憶|種類|合格/件数|合格率|", "|---|---|---:|---:|"]
    for mode, summary in result["summary"].items():
        for kind, values in {"全体": summary, **summary["by_kind"]}.items():
            rate = values["pass_rate"]
            lines.append(f"|{mode}|{kind}|{values['passed']}/{values['scenarios']}|{rate:.1%}|" if rate is not None else f"|{mode}|{kind}|0/0|未測定|")
        lines += ["", f"{mode}: 作り話 {summary['hallucination_count']} 件 / 想起 {summary['recall_seconds']:.6f} 秒 ({summary['recall_calls']} 呼出し)", ""]
        for failure in summary["failures"]:
            lines += [f"- 不合格 {failure['id']} / 質問 {failure['turn']} / {', '.join(failure['failed_checks'])}",
                      f"  - 応答: {failure['response']}", f"  - エラー: {failure['error']}"]
    lines += ["", "本結果だけでは他の会話・QA評価の低下を判定できない。改善ループは別途実施する。"]
    md_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return json_path, md_path


def parser():
    p = argparse.ArgumentParser(prog="python -m evaluation.memory", description=__doc__)
    p.add_argument("--scenarios", type=Path, default=SCENARIOS)
    p.add_argument("--kind", choices=KINDS, action="append", help="複数指定可")
    p.add_argument("--limit", type=int, help="種類の絞り込み後のシナリオ件数")
    p.add_argument("--no-summary", action="store_true", help="要約せず、既存 recall の直近発言フォールバックを使う")
    p.add_argument("--model", default="gemma4:12b")
    p.add_argument("--output-dir", type=Path, default=Path("data/eval/memory"))
    return p


def main(argv=None):
    p = parser()
    args = p.parse_args(argv)
    if args.limit is not None and args.limit < 1:
        p.error("--limit は1以上です")
    data = load_scenarios(args.scenarios)
    items = [s for s in data["scenarios"] if not args.kind or s["kind"] in args.kind]
    if args.limit:
        items = items[:args.limit]
    if not items:
        p.error("対象シナリオがありません")
    rows = []
    for item in items:
        for enabled in (True, False):
            print(f"評価中: {item['id']} / 記憶 {'あり' if enabled else 'なし'}", flush=True)
            rows.append(run_scenario(item, enabled, model=args.model, no_summary=args.no_summary))
    result = {"stamp": datetime.now().strftime("%Y%m%d-%H%M%S-%f"), "model": args.model,
              "summary_enabled": not args.no_summary, "partial": len(items) != len(data["scenarios"]),
              "selected": len(items), "total": len(data["scenarios"]), "kind_filter": args.kind,
              "aggregation": "全質問合格のシナリオ数 / シナリオ数", "rows": rows, "summary": summarize_results(rows)}
    for path in write_results(result, args.output_dir):
        print(path)
    # 合格率の低さは評価結果。生成・要約の実行失敗だけ非ゼロで知らせる。
    return int(any(r["setup_error"] or any(t["error"] or not t["checks"]["execution_ok"] for t in r["turns"]) for r in rows))
