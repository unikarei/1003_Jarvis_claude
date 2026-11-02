"""改善のループ（../docs/spec.md §3.4.10、ADR-0030、T-1412）。

決めた設定の空間（SPACE）の中だけで、今の設定から 1 つの軸を変えた候補を作り、dev で選び、val で採否を決める。
過学習を避けるための決まり:
- 評価セットは作り直さない（同じ版を使う）。候補の作り方の手がかりは dev の失敗の傾向だけ
- val は各回の採否にだけ使う（1 回につき 1 度）。test はループの後に、始めと最後の設定で 1 度ずつだけ測り、設定を選び直さない
- 回数は既定 3 回（上限 5）。採用のない回があれば、そこで止める
結果は data/eval/tuned/<日時>.json（採用した設定の提案）と data/eval/reports/<日時>-improve.md。本番の設定・コードは書き換えない。

評価関数 evaluate(t, split) は runner.run と同じ形の結果（summary.overall の score / score_retrieval、
items の failed_at、answer、llm_calls）を返す。テストでは偽の関数に差し替える。
"""

import copy
import json
import random
import sys
from collections import Counter
from dataclasses import dataclass, fields, replace
from datetime import datetime
from pathlib import Path

import config
import evaluation
from evaluation import isolate, qa, report, runner
from evaluation.qa import SPLITS
from tuning import Tuning

SYNONYMS_PATH = Path(__file__).with_name("synonyms.json")
EXTRA_KNOWLEDGE_WORDS = ("住所", "所在地", "物件", "図面", "Obsidian", "HTML")
DEFAULT_MAX_ITERATIONS = 3
MAX_ITERATIONS_LIMIT = 5
CANDIDATES_PER_ITERATION = 4
MIN_GAIN = 0.03          # dev の総合点がこれ以上上がった候補だけを選ぶ（答えの揺れ 0.029 より大きく、T-1414）
VAL_TOLERANCE = 0.01     # val の総合点がこれを超えて下がるなら採用しない
EPS = 1e-9               # 浮動小数の比較の誤差
# 落ちた段階 → 優先して変える軸（dev の失敗の多い段階から順に）
STAGE_AXES = {"route": ("extra_knowledge_words", "score_threshold"),
              "search": ("synonyms", "search_limit"),
              "answer": ("context_chars", "answer_prompt")}
ANSWER_AXES = STAGE_AXES["answer"]     # --no-answer（score_retrieval）では点が変わらないので試さない
STOPPED = {"max_iterations": "決めた回数に達した", "no_adoption": "採用のない回があった"}


class ImproveError(runner.EvalError):
    """改善のループを始められない（回数・候補の数・呼び出しの見込みが上限を超える など）。"""


def load_synonyms(path: Path | None = None) -> dict[str, list[str]]:
    """同義語の辞書（組の左右の両向き）を {"住所": ["所在地"], "所在地": ["住所"], …} の形で返す。"""
    data = json.loads(Path(path or SYNONYMS_PATH).read_text(encoding="utf-8"))
    out: dict[str, list[str]] = {}
    for pair in data["pairs"]:
        if not (isinstance(pair, list) and len(pair) == 2
                and all(isinstance(w, str) and w.strip() for w in pair)):
            raise ValueError(f"同義語の組は空でない 2 つの語にしてください: {pair!r}")
        a, b = (w.strip() for w in pair)
        for word, alt in ((a, b), (b, a)):
            alts = out.setdefault(word, [])
            if alt not in alts:
                alts.append(alt)
    return out


SYNONYMS = load_synonyms()
# 探す空間。ここにない値は試さない（synonyms・extra_knowledge_words は「使わない / 使う」の 2 つ）
SPACE = {
    "search_limit": (6, 8, 12),
    "context_chars": (1000, 1500, 2500),
    "score_threshold": (6.0, 8.0, 10.0),
    "synonyms": ({}, SYNONYMS),
    "extra_knowledge_words": ((), EXTRA_KNOWLEDGE_WORDS),
    "answer_prompt": ("default", "grounded"),
}


# ---------------------------------------------------------------- 設定と結果の読み方

def axis_value(t: Tuning, axis: str):
    """軸の実際の値（score_threshold の None は config.KNOWLEDGE_SCORE_THRESHOLD）。"""
    value = getattr(t, axis)
    if axis == "score_threshold" and value is None:
        return config.KNOWLEDGE_SCORE_THRESHOLD
    return value


def label(axis: str, value) -> object:
    """表示と JSON に書く値（辞書・語の一覧は on / off）。"""
    if axis == "synonyms":
        return "on" if value == SYNONYMS else "off" if not value else "custom"
    if axis == "extra_knowledge_words":
        return "on" if tuple(value) == EXTRA_KNOWLEDGE_WORDS else "off" if not value else list(value)
    return value


def tuning_key(t: Tuning) -> str:
    return json.dumps(t.to_dict(), ensure_ascii=False, sort_keys=True)


def tuning_from_dict(data: dict) -> Tuning:
    """--start の JSON（Tuning の値、または tuned の結果のファイル）から Tuning を作る。"""
    if not isinstance(data, dict):
        raise ValueError("設定の JSON はオブジェクトにしてください")
    if isinstance(data.get("tuned"), dict):
        data = data["tuned"]
    names = {f.name for f in fields(Tuning)}
    unknown = sorted(set(data) - names)
    if unknown:
        raise ValueError(f"知らない設定の名前です: {', '.join(unknown)}")
    values = dict(data)
    if "extra_knowledge_words" in values:
        values["extra_knowledge_words"] = tuple(values["extra_knowledge_words"] or ())
    if "synonyms" in values:
        values["synonyms"] = {str(k): list(v) for k, v in (values["synonyms"] or {}).items()}
    return Tuning(**values)


def check_space(t: Tuning) -> None:
    """設定の全軸が SPACE の中にあることを確かめる（外の値を候補に持ち込まないため）。"""
    outside = [f"{axis}={label(axis, axis_value(t, axis))!r}" for axis in SPACE
               if axis_value(t, axis) not in SPACE[axis]]
    if outside:
        raise ImproveError(f"始めの設定に探す空間（SPACE）の外の値があります: {', '.join(outside)}")


def load_start(path: str | Path) -> Tuning:
    return tuning_from_dict(json.loads(Path(path).read_text(encoding="utf-8")))


def score_key(result: dict) -> str:
    return "score" if result.get("answer", True) else "score_retrieval"


def score_of(result: dict) -> float:
    value = result["summary"]["overall"].get(score_key(result))
    return float(value) if value is not None else 0.0


def failures(result: dict) -> Counter:
    """落ちた段階ごとの問の数。"""
    return Counter(r["failed_at"] for r in result.get("items", []) if r.get("failed_at"))


# ---------------------------------------------------------------- 1 回の改善

def priority_axes(dev: dict) -> list[str]:
    """dev の失敗の多い段階に関係する軸から順に並べる（同数なら 振り分け → 検索 → 答え）。"""
    counts = failures(dev)
    stages = sorted(STAGE_AXES, key=lambda s: -counts.get(s, 0))
    axes = [a for s in stages for a in STAGE_AXES[s]]
    if not dev.get("answer", True):
        axes = [a for a in axes if a not in ANSWER_AXES]
    return axes


@dataclass
class Candidate:
    axis: str
    value: object
    tuning: Tuning


def make_candidates(current: Tuning, dev: dict, rng: random.Random, n: int = CANDIDATES_PER_ITERATION,
                    seen: set[str] | frozenset = frozenset()) -> list[Candidate]:
    """今の設定から 1 つの軸だけを SPACE の別の値に変えた候補を、優先する軸から順に最大 n 個。
    同じ軸の中の値の順は rng で決める。すでに試した設定（seen）は除く。"""
    out = []
    for axis in priority_axes(dev):
        values = [v for v in SPACE[axis] if v != axis_value(current, axis)]
        rng.shuffle(values)
        for value in values:
            t = replace(current, **{axis: copy.deepcopy(value)})
            if tuning_key(t) in seen:
                continue
            out.append(Candidate(axis, value, t))
            if len(out) >= n:
                return out
    return out


@dataclass
class Iteration:
    adopted: bool
    tuning: Tuning           # この回の後の設定（採用しなければ今のまま）
    dev: dict                # その設定の dev の結果
    val: dict                # その設定の val の結果
    record: dict             # 結果のファイルに書く内容
    tried: list[Candidate]


def improve_once(current: Tuning, evaluate, dev: dict, val: dict, rng: random.Random, *,
                 n: int = CANDIDATES_PER_ITERATION, seen: set[str] | frozenset = frozenset()) -> Iteration:
    """1 回の改善。dev・val は今の設定の結果。候補は dev の失敗の傾向だけから作り、val は選んだ 1 つにだけ使う。"""
    key = score_key(dev)
    base_dev, base_val = score_of(dev), score_of(val)
    candidates = make_candidates(current, dev, rng, n=n, seen=seen)
    record = {"failures": dict(failures(dev)), "axes": priority_axes(dev), "base": {"dev": base_dev, "val": base_val},
              "candidates": [], "selected": None, "adopted": False, "reason": ""}
    best = None
    for c in candidates:
        r = evaluate(c.tuning, "dev")
        s = score_of(r)
        record["candidates"].append({"axis": c.axis, "from": label(c.axis, getattr(current, c.axis)),
                                     "to": label(c.axis, c.value), "dev": s, "gain": round(s - base_dev, 4)})
        if s - base_dev >= MIN_GAIN - EPS and (best is None or s > best[1] + EPS):
            best = (c, s, r)
    if not candidates:
        record["reason"] = "試す候補がありません（空間の中の設定をすべて試した）"
        return Iteration(False, current, dev, val, record, candidates)
    if best is None:
        record["reason"] = f"dev の {key} が {MIN_GAIN} 以上上がる候補がありません"
        return Iteration(False, current, dev, val, record, candidates)
    c, s, r = best
    v = evaluate(c.tuning, "val")
    vs = score_of(v)
    record["selected"] = {"axis": c.axis, "from": label(c.axis, getattr(current, c.axis)),
                          "to": label(c.axis, c.value), "dev": s, "val": vs}
    if vs < base_val - VAL_TOLERANCE - EPS:
        record["reason"] = f"val の {key} が {base_val:.3f} → {vs:.3f} と許容幅（{VAL_TOLERANCE}）を超えて下がるため採用しません"
        return Iteration(False, current, dev, val, record, candidates)
    record["adopted"] = True
    record["reason"] = (f"dev の {key} が {base_dev:.3f} → {s:.3f}、val が {base_val:.3f} → {vs:.3f}"
                        f"（許容幅 {VAL_TOLERANCE} の中）のため採用")
    return Iteration(True, c.tuning, r, v, record, candidates)


# ---------------------------------------------------------------- ループ

def estimate_llm_calls(counts: dict[str, int], max_iterations: int, candidates: int, answer: bool) -> int:
    """呼び出しの見込み（最大）: 初めの dev・val ＋ 各回の 候補×dev ＋ val ＋ 最後の test×2。"""
    if not answer:
        return 0
    dev, val, test = (counts.get(s, 0) for s in SPLITS)
    return dev + val + max_iterations * (candidates * dev + val) + 2 * test


def check_settings(max_iterations: int, candidates: int, provider: str, max_llm_calls: int) -> None:
    if provider not in runner.PROVIDERS:
        raise ImproveError(f"評価に使えないプロバイダーです: {provider}（{' / '.join(runner.PROVIDERS)} だけ。有料の API は使わない）")
    if not 1 <= max_iterations <= MAX_ITERATIONS_LIMIT:
        raise ImproveError(f"--max-iterations は 1〜{MAX_ITERATIONS_LIMIT} で指定してください（過学習を避けるため上限 "
                           f"{MAX_ITERATIONS_LIMIT}、既定 {DEFAULT_MAX_ITERATIONS}）: {max_iterations}")
    if not 1 <= candidates <= CANDIDATES_PER_ITERATION:
        raise ImproveError(f"--candidates は 1〜{CANDIDATES_PER_ITERATION} で指定してください: {candidates}")
    if max_llm_calls < 0:
        raise ImproveError("--max-llm-calls は 0 以上で指定してください")


def improve(qa_version: str, max_iterations: int = DEFAULT_MAX_ITERATIONS, *,
            candidates: int = CANDIDATES_PER_ITERATION, answer: bool = True, provider: str = "ollama",
            max_llm_calls: int = runner.DEFAULT_MAX_LLM_CALLS, start: Tuning | None = None, seed: int = 0,
            evaluate=None, items: list[dict] | None = None, log=None, write: bool = True, judge=False) -> dict:
    """改善のループを回し、結果（と書いたファイルのパス）を返す。evaluate を省くと runner.run で評価する。
    judge は runner.run と同じ（要点の LLM の判定。常に Ollama で、Claude の上限に数えない。CLI の既定は判定する）。"""
    check_settings(max_iterations, candidates, provider, max_llm_calls)
    start = start or Tuning()
    check_space(start)
    log = log or (lambda msg: print(msg, file=sys.stderr))
    items = qa.load(qa_version) if items is None else items
    by_split = {s: [i for i in items if i.get("split") == s] for s in SPLITS}
    counts = {s: len(v) for s, v in by_split.items()}
    limited = answer and provider in runner.LIMITED_PROVIDERS
    estimate = estimate_llm_calls(counts, max_iterations, candidates, answer)
    judging = bool(judge) and answer
    log(f"LLM の呼び出しの見込み: 最大 {estimate} 回（dev {counts['dev']}・val {counts['val']}・test {counts['test']} 問、"
        f"{max_iterations} 回 × 候補 {candidates} 個、{provider}"
        + (f"、上限 {max_llm_calls} 回）" if limited else "、上限なし）" if answer else "、答えを作らない）")
        + (f"、判定 最大 {estimate} 回（{runner.JUDGE_PROVIDER}）" if judging else ""))
    if limited and estimate > max_llm_calls:
        raise ImproveError(f"LLM の呼び出しの見込み（{estimate} 回）が上限（{max_llm_calls} 回）を超えるため始めません。"
                           "--max-iterations・--candidates を減らすか、--no-answer（振り分けと検索だけ）、"
                           "または --provider ollama を使ってください")

    calls = {"total": 0, "evaluations": 0, "judge": 0}

    def run_split(t: Tuning, split: str) -> dict:
        remaining = max(max_llm_calls - calls["total"], 0) if limited else max_llm_calls
        return runner.run(by_split[split], tuning=t, provider=provider, answer=answer,
                          max_llm_calls=remaining, log=lambda msg: None, judge=judge)

    base_evaluate = evaluate or run_split

    def tracked(t: Tuning, split: str) -> dict:
        r = base_evaluate(t, split)
        calls["total"] += r.get("llm_calls") or 0          # 判定の呼び出しは Claude の上限に数えない
        calls["judge"] += r.get("judge_calls") or 0
        calls["evaluations"] += 1
        if r.get("stopped"):
            raise ImproveError(f"評価が途中で止まりました（{r['stopped']}）")
        return r

    started = datetime.now()
    rng = random.Random(seed)
    dev, val = tracked(start, "dev"), tracked(start, "val")
    key = score_key(dev)
    start_dev, start_val = dev, val
    log(f"始めの設定: dev {score_of(dev):.3f}・val {score_of(val):.3f}（{key}）")
    current, seen, iterations, changes = start, {tuning_key(start)}, [], []
    stopped = "max_iterations"
    for n in range(1, max_iterations + 1):
        it = improve_once(current, tracked, dev, val, rng, n=candidates, seen=seen)
        seen |= {tuning_key(c.tuning) for c in it.tried}
        record = {"iteration": n, **it.record}
        iterations.append(record)
        log(_iteration_line(record))
        if not it.adopted:
            stopped = "no_adoption"
            break
        sel = record["selected"]
        changes.append({"iteration": n, "axis": sel["axis"], "from": sel["from"], "to": sel["to"],
                        "dev": [record["base"]["dev"], sel["dev"]], "val": [record["base"]["val"], sel["val"]]})
        current, dev, val = it.tuning, it.dev, it.val
    # test はループの後に 1 度だけ（始めと最後の設定）。結果で設定を選び直さない
    test_start = tracked(start, "test")
    test_final = test_start if tuning_key(current) == tuning_key(start) else tracked(current, "test")
    log(f"test（確認用、1 度だけ）: 始め {score_of(test_start):.3f} → 終わり {score_of(test_final):.3f}")

    result = {
        "kind": "improve", "qa_version": qa_version,
        "started_at": started.isoformat(timespec="seconds"), "finished_at": datetime.now().isoformat(timespec="seconds"),
        "provider": provider, "answer": answer, "score_key": key,
        "settings": {"max_iterations": max_iterations, "candidates": candidates, "min_gain": MIN_GAIN,
                     "val_tolerance": VAL_TOLERANCE, "seed": seed},
        "counts": counts, "start_tuning": start.to_dict(), "tuned": current.to_dict(), "changes": changes,
        "scores": {"start": {"dev": score_of(start_dev), "val": score_of(start_val), "test": score_of(test_start)},
                   "final": {"dev": score_of(dev), "val": score_of(val), "test": score_of(test_final)}},
        "summaries": {"start": {"dev": start_dev.get("summary"), "val": start_val.get("summary"),
                                "test": test_start.get("summary")},
                      "final": {"dev": dev.get("summary"), "val": val.get("summary"), "test": test_final.get("summary")}},
        "iterations": iterations, "iterations_run": len(iterations), "stopped": stopped,
        "evaluations": calls["evaluations"], "llm_calls": calls["total"], "llm_calls_estimate": estimate,
        "max_llm_calls": max_llm_calls if limited else None,
        "judge": runner.JUDGE_PROVIDER if judging else None, "judge_calls": calls["judge"], **isolate.source_info(),
    }
    if write:
        json_path, md_path = write_result(result)
        result["paths"] = {"tuned": str(json_path), "report": str(md_path)}
    return result


def _iteration_line(record: dict) -> str:
    cands = "、".join(f"{c['axis']}={c['to']} dev {c['dev']:.3f}" for c in record["candidates"]) or "なし"
    sel = record["selected"]
    chosen = f"、選んだ候補 {sel['axis']}={sel['to']}（dev {sel['dev']:.3f}・val {sel['val']:.3f}）" if sel else ""
    return (f"{record['iteration']} 回目: 候補 {len(record['candidates'])} 個（{cands}）{chosen} → "
            f"{'採用' if record['adopted'] else '不採用'}: {record['reason']}")


# ---------------------------------------------------------------- 結果のファイル

def tuned_dir() -> Path:
    return evaluation.EVAL_DIR / "tuned"


def write_result(result: dict, stamp: str | None = None) -> tuple[Path, Path]:
    """data/eval/tuned/<日時>.json と data/eval/reports/<日時>-improve.md を書く（同じ名前があれば -2, -3 …）。"""
    tdir, rdir = tuned_dir(), report.reports_dir()
    tdir.mkdir(parents=True, exist_ok=True)
    rdir.mkdir(parents=True, exist_ok=True)
    stamp = stamp or datetime.now().strftime("%Y%m%d-%H%M%S-%f")
    n = 1
    while True:
        name = stamp if n == 1 else f"{stamp}-{n}"
        json_path, md_path = tdir / f"{name}.json", rdir / f"{name}-improve.md"
        if not md_path.exists():
            try:
                f = open(json_path, "x", encoding="utf-8")
            except FileExistsError:
                pass
            else:
                break
        n += 1
    with f:
        json.dump(result, f, ensure_ascii=False, indent=2)
        f.write("\n")
    with open(md_path, "x", encoding="utf-8") as f:
        f.write(to_markdown(result))
    return json_path, md_path


def _cell(value) -> str:
    return report._cell(value)


def _diff(a, b) -> str:
    if a is None or b is None:
        return "-"
    return f"{b - a:+.3f}"


def _group_rows(result: dict, name: str, split: str = "test") -> list[str]:
    key = result["score_key"]
    before = ((result["summaries"]["start"].get(split) or {}).get(name)) or {}
    after = ((result["summaries"]["final"].get(split) or {}).get(name)) or {}
    rows = []
    for group in list(before) + [g for g in after if g not in before]:
        a, b = (before.get(group) or {}).get(key), (after.get(group) or {}).get(key)
        count = (after.get(group) or before.get(group) or {}).get("count")
        rows.append(f"| {group} | {_cell(count)} | {_cell(a)} | {_cell(b)} | {_diff(a, b)} |")
    return rows or ["| - | - | - | - | - |"]


def to_markdown(result: dict) -> str:
    key = result["score_key"]
    st = result["settings"]
    lines = [f"# 改善のループ（{result['qa_version']}）", "",
             f"- 実行: {result['started_at']} 〜 {result['finished_at']}",
             f"- 答え: {'作る' if result['answer'] else '作らない（振り分けと検索だけ）'}、プロバイダー: {result['provider']}、"
             f"採否に使う点: `{key}`",
             f"- 回数: {result['iterations_run']} / {st['max_iterations']} 回（止めた理由: "
             f"{STOPPED.get(result['stopped'], result['stopped'])}）、候補: 1 回 {st['candidates']} 個まで、"
             f"MIN_GAIN {st['min_gain']}、VAL_TOLERANCE {st['val_tolerance']}、seed {st['seed']}",
             f"- LLM の呼び出し: {result['llm_calls']} 回（見込み 最大 {result['llm_calls_estimate']} 回"
             + (f"、上限 {result['max_llm_calls']} 回" if result.get("max_llm_calls") is not None else "") + "）、"
             + (f"判定 {result.get('judge_calls', 0)} 回（{result['judge']}）、" if result.get("judge") else "")
             + f"評価 {result['evaluations']} 回",
             f"- 問の数: " + "・".join(f"{s} {result['counts'].get(s, 0)}" for s in SPLITS)]
    if result.get("source_db"):
        lines.append(f"- 元の DB: {result['source_db']}（コピー {result.get('copied_at') or '-'}）")
    lines += ["", "過学習の防止: 評価セットは同じ版を使い、候補の手がかりは dev の失敗の傾向だけ。val は各回の採否に 1 度だけ、"
                  "test はループの後に 1 度だけ測り、設定の選択に使わない。本番の設定への反映は PR で行う。", "",
              "## 前後の比較", "", "| 組 | 指標 | 始め | 終わり | 差 |", "|---|---|---|---|---|"]
    metrics = [key, "route_acc", "recall_eff", "mrr"] + (["facts_hit", "facts_hit_strict", "none_rate"] if result["answer"] else [])
    for split in SPLITS:
        before = ((result["summaries"]["start"].get(split) or {}).get("overall")) or {}
        after = ((result["summaries"]["final"].get(split) or {}).get("overall")) or {}
        for m in metrics:
            a, b = before.get(m), after.get(m)
            lines.append(f"| {split} | {m} | {_cell(a)} | {_cell(b)} | {_diff(a, b)} |")
    for title, name in (("データソース別", "by_source"), ("形別", "by_style")):
        lines += ["", f"## {title}の変化（test、`{key}`）", "", f"| {title[:-1]} | 件数 | 始め | 終わり | 差 |",
                  "|---|---|---|---|---|"] + _group_rows(result, name)
    lines += ["", "## 採用した変更", ""]
    if result["changes"]:
        for c in result["changes"]:
            lines.append(f"- {c['iteration']} 回目: `{c['axis']}` {c['from']} → {c['to']}"
                         f"（dev {c['dev'][0]:.3f} → {c['dev'][1]:.3f}、val {c['val'][0]:.3f} → {c['val'][1]:.3f}）")
    else:
        lines.append("なし（始めの設定のまま）")
    lines += ["", "## 各回の候補", ""]
    for it in result["iterations"]:
        fails = "、".join(f"{report.FAILED_AT.get(k, k)} {v}" for k, v in it["failures"].items()) or "なし"
        lines += [f"### {it['iteration']} 回目（{'採用' if it['adopted'] else '不採用'}）", "",
                  f"- dev の失敗: {fails} → 優先した軸: {'・'.join(it['axes'])}",
                  f"- 今の設定: dev {it['base']['dev']:.3f}・val {it['base']['val']:.3f}", "",
                  "| 軸 | 変更 | dev | 差 |", "|---|---|---|---|"]
        lines += [f"| {c['axis']} | {c['from']} → {c['to']} | {c['dev']:.3f} | {c['gain']:+.3f} |"
                  for c in it["candidates"]] or ["| - | - | - | - |"]
        sel = it["selected"]
        if sel:
            lines += ["", f"- 選んだ候補: `{sel['axis']}` → {sel['to']}（dev {sel['dev']:.3f}、val {sel['val']:.3f}）"]
        lines += [f"- 採否: {it['reason']}", ""]
    lines += ["## 採用した設定（提案）", "", "```json", json.dumps(result["tuned"], ensure_ascii=False, indent=2), "```", ""]
    return "\n".join(lines)
