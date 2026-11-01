"""評価部門の改善のループ（T-1412、ADR-0030）のテスト。
評価は偽の関数（設定 → dev・val・test の点と失敗の傾向）。本物の LLM・本番の DB は使わない。"""

import hashlib
import json
import random
from dataclasses import fields

import pytest

import config
import db
import evaluation
import llm
import skills
from evaluation import __main__ as cli
from evaluation import improve, isolate, qa, runner
from evaluation.improve import SPACE, STAGE_AXES
from tuning import Tuning

AXES = tuple(SPACE)
BASE = 0.5


def make_items(dev=30, val=15, test=15) -> list[dict]:
    out = []
    for split, n in (("dev", dev), ("val", val), ("test", test)):
        out += [{"id": f"{split}{i}", "question": f"問{i}", "key_facts": ["答え"], "knowledge_id": f"k{i}",
                 "source": "web" if i % 2 else "obsidian", "style": "direct", "split": split} for i in range(n)]
    return out


ITEMS = make_items()


def changed_axes(t: Tuning, base: Tuning = Tuning()) -> list[str]:
    return [a for a in AXES if improve.axis_value(t, a) != improve.axis_value(base, a)]


def fake_result(score: float, fails: dict | None = None, answer: bool = True, count: int = 0) -> dict:
    key = "score" if answer else "score_retrieval"
    items = [{"failed_at": stage} for stage, n in (fails or {}).items() for _ in range(n)]
    group = {key: score, "count": count}
    return {"answer": answer, "llm_calls": count if answer else 0, "items": items, "stopped": None,
            "summary": {"overall": {**group, "route_acc": 0.9, "recall_eff": score, "mrr": score,
                                    "facts_hit": score if answer else None, "none_rate": 0.0 if answer else None},
                        "by_source": {"web": group, "obsidian": group}, "by_style": {"direct": group}}}


class FakeEval:
    """偽の評価関数。score(t, split) と fails(t, split) で点と失敗の傾向を決め、呼び出しを記録する。"""

    def __init__(self, score=None, fails=None, answer=True, items=ITEMS):
        self.score = score or (lambda t, split: BASE + 0.05 * len(changed_axes(t)))
        self.fails = fails or (lambda t, split: {"route": 3, "search": 2, "answer": 1})
        self.answer = answer
        self.counts = {s: sum(1 for i in items if i["split"] == s) for s in qa.SPLITS}
        self.calls: list[tuple[Tuning, str]] = []

    def __call__(self, t, split):
        self.calls.append((t, split))
        return fake_result(round(self.score(t, split), 4), self.fails(t, split), self.answer, self.counts[split])

    def splits(self, split):
        return [t for t, s in self.calls if s == split]


@pytest.fixture
def eval_dir(tmp_path, monkeypatch):
    path = tmp_path / "eval"
    monkeypatch.setattr(evaluation, "EVAL_DIR", path)
    monkeypatch.setattr(config, "KNOWLEDGE_SCORE_THRESHOLD", 8.0)
    for name in (isolate.ISOLATED_ENV, isolate.SOURCE_DB_ENV, isolate.COPIED_AT_ENV):
        monkeypatch.delenv(name, raising=False)
    return path


def run(fake, **kw):
    kw.setdefault("items", ITEMS)
    kw.setdefault("log", lambda msg: None)
    return improve.improve("qa-t", evaluate=fake, **kw)


# ---------------------------------------------------------------- 回数と停止

def test_stops_after_default_3_iterations(eval_dir):
    fake = FakeEval()
    result = run(fake)
    assert improve.DEFAULT_MAX_ITERATIONS == 3
    assert result["iterations_run"] == 3 and result["stopped"] == "max_iterations"
    assert len(result["changes"]) == 3 and all(it["adopted"] for it in result["iterations"])
    assert len(changed_axes(improve.tuning_from_dict(result["tuned"]))) == 3


@pytest.mark.parametrize("n", [0, 6, 10])
def test_max_iterations_over_limit_is_error(eval_dir, n):
    fake = FakeEval()
    with pytest.raises(improve.ImproveError):
        run(fake, max_iterations=n)
    assert fake.calls == []


def test_five_iterations_is_the_limit(eval_dir):
    assert run(FakeEval(), max_iterations=5)["iterations_run"] == 5


def test_stops_at_first_iteration_without_adoption(eval_dir):
    # 2 回目は dev が上がらない（1 つ目の変更の後は横ばい）
    fake = FakeEval(score=lambda t, split: BASE + 0.05 * min(len(changed_axes(t)), 1))
    result = run(fake)
    assert result["iterations_run"] == 2 and result["stopped"] == "no_adoption"
    assert [it["adopted"] for it in result["iterations"]] == [True, False]
    assert "上がる候補がありません" in result["iterations"][1]["reason"]
    assert len(result["changes"]) == 1


def test_gain_below_min_gain_is_not_selected(eval_dir):
    fake = FakeEval(score=lambda t, split: BASE + 0.009 * len(changed_axes(t)))
    result = run(fake)
    assert result["iterations_run"] == 1 and not result["changes"]
    assert result["tuned"] == Tuning().to_dict()
    assert fake.splits("val") == [Tuning()]          # 選んだ候補がないので val は始めの 1 度だけ


# ---------------------------------------------------------------- val による採否

def test_candidate_that_drops_val_beyond_tolerance_is_rejected(eval_dir):
    def score(t, split):
        n = len(changed_axes(t))
        return BASE + (0.05 * n if split == "dev" else -0.02 * n)
    fake = FakeEval(score=score)
    result = run(fake)
    assert result["iterations_run"] == 1 and result["stopped"] == "no_adoption"
    it = result["iterations"][0]
    assert not it["adopted"] and it["selected"]["val"] == pytest.approx(BASE - 0.02)
    assert "許容幅" in it["reason"]
    assert result["tuned"] == Tuning().to_dict() and result["changes"] == []
    # 次点の候補は val で試さない（val は始め + 選んだ 1 つだけ）
    assert len(fake.splits("val")) == 2


def test_val_drop_within_tolerance_is_adopted(eval_dir):
    def score(t, split):
        n = len(changed_axes(t))
        return BASE + (0.05 * n if split == "dev" else -0.01 * n)
    result = run(FakeEval(score=score), max_iterations=1)
    assert result["iterations"][0]["adopted"] and len(result["changes"]) == 1


def test_best_dev_candidate_is_selected(eval_dir):
    def score(t, split):
        return BASE + (0.1 if t.search_limit == 12 else 0.0) + (0.03 if t.synonyms else 0.0)
    fake = FakeEval(score=score, fails=lambda t, s: {"search": 5})
    result = run(fake, max_iterations=1)
    sel = result["iterations"][0]["selected"]
    assert (sel["axis"], sel["to"]) == ("search_limit", 12)
    assert result["tuned"]["search_limit"] == 12


# ---------------------------------------------------------------- 候補の作り方

STARTS = [Tuning(), Tuning(search_limit=12, synonyms=dict(improve.SYNONYMS), answer_prompt="grounded"),
          Tuning(score_threshold=6.0, extra_knowledge_words=improve.EXTRA_KNOWLEDGE_WORDS, context_chars=2500)]


@pytest.mark.parametrize("start", STARTS)
@pytest.mark.parametrize("fails", [{"route": 5}, {"search": 5}, {"answer": 5}, {}])
def test_candidates_change_one_axis_inside_space(start, fails):
    dev = fake_result(0.5, fails)
    for seed in range(5):
        cands = improve.make_candidates(start, dev, random.Random(seed))
        assert 1 <= len(cands) <= improve.CANDIDATES_PER_ITERATION
        assert len({improve.tuning_key(c.tuning) for c in cands}) == len(cands)
        for c in cands:
            assert changed_axes(c.tuning, start) == [c.axis]
            assert improve.axis_value(c.tuning, c.axis) in SPACE[c.axis]
            for f in fields(Tuning):
                if f.name != c.axis:
                    assert getattr(c.tuning, f.name) == getattr(start, f.name)


def test_whole_loop_stays_inside_space(eval_dir):
    fake = FakeEval()
    run(fake, max_iterations=5)
    for t, _ in fake.calls:
        for axis in changed_axes(t):
            assert improve.axis_value(t, axis) in SPACE[axis]


@pytest.mark.parametrize("stage", ["route", "search", "answer"])
def test_dev_failures_choose_axes(stage):
    dev = fake_result(0.5, {stage: 10, **{s: 1 for s in STAGE_AXES if s != stage}})
    assert improve.priority_axes(dev)[:2] == list(STAGE_AXES[stage])
    cands = improve.make_candidates(Tuning(), dev, random.Random(0), n=2)
    assert {c.axis for c in cands} <= set(STAGE_AXES[stage])


def test_no_answer_does_not_try_answer_axes():
    dev = fake_result(0.5, {"answer": 10}, answer=False)
    assert not set(improve.priority_axes(dev)) & set(improve.ANSWER_AXES)


def _candidate_trace(result):
    return [[(c["axis"], c["to"]) for c in it["candidates"]] for it in result["iterations"]]


def test_val_and_test_failures_do_not_change_candidates(eval_dir):
    def fails_a(t, split):
        return {"route": 3, "search": 1} if split == "dev" else {"answer": 9}

    def fails_b(t, split):
        return {"route": 3, "search": 1} if split == "dev" else {"search": 7, "route": 1}
    a, b = run(FakeEval(fails=fails_a)), run(FakeEval(fails=fails_b))
    assert _candidate_trace(a) == _candidate_trace(b) and a["tuned"] == b["tuned"]
    # dev の傾向を変えると候補が変わる
    c = run(FakeEval(fails=lambda t, split: {"answer": 9} if split == "dev" else {"route": 3}))
    assert _candidate_trace(c) != _candidate_trace(a)
    assert c["iterations"][0]["candidates"][0]["axis"] in STAGE_AXES["answer"]


def test_same_seed_same_candidates(eval_dir):
    assert _candidate_trace(run(FakeEval(), seed=3)) == _candidate_trace(run(FakeEval(), seed=3))


# ---------------------------------------------------------------- val・test の呼び出しの回数

def test_val_once_per_iteration_and_test_once_at_end(eval_dir):
    fake = FakeEval()
    result = run(fake)
    order = [s for _, s in fake.calls]
    # val: 始めの 1 度 + 各回 1 度まで
    assert order.count("val") == 1 + result["iterations_run"]
    # test: ループの後に、始めの設定と最後の設定で 1 度ずつだけ
    tests = fake.splits("test")
    assert len(tests) == 2 and tests[0] == Tuning()
    assert improve.tuning_key(tests[1]) == improve.tuning_key(improve.tuning_from_dict(result["tuned"]))
    assert order[-2:] == ["test", "test"] and "test" not in order[:-2]
    # dev: 始めの 1 度 + 各回の候補の数
    assert order.count("dev") == 1 + sum(len(it["candidates"]) for it in result["iterations"])
    assert result["evaluations"] == len(fake.calls)


def test_test_once_when_nothing_adopted(eval_dir):
    fake = FakeEval(score=lambda t, split: BASE)
    result = run(fake)
    assert fake.splits("test") == [Tuning()]
    assert result["scores"]["start"]["test"] == result["scores"]["final"]["test"]


def test_test_scores_do_not_affect_choice(eval_dir):
    a = run(FakeEval())
    b = run(FakeEval(score=lambda t, split: (0.0 if split == "test" else BASE + 0.05 * len(changed_axes(t)))))
    assert a["tuned"] == b["tuned"] and _candidate_trace(a) == _candidate_trace(b)


# ---------------------------------------------------------------- 呼び出しの見込みと上限

def test_estimate_default_is_480():
    counts = {"dev": 30, "val": 15, "test": 15}
    assert improve.estimate_llm_calls(counts, 3, 4, True) == 480
    assert improve.estimate_llm_calls(counts, 3, 4, False) == 0


def test_default_ollama_runs_to_completion_without_limit(eval_dir):
    logs = []
    fake = FakeEval()
    result = run(fake, log=logs.append)
    assert result["provider"] == "ollama" and result["iterations_run"] == 3
    assert result["llm_calls_estimate"] == 480 and result["max_llm_calls"] is None
    assert result["llm_calls"] > runner.DEFAULT_MAX_LLM_CALLS          # 上限をかけない
    assert result["llm_calls"] <= result["llm_calls_estimate"]
    assert any("見込み: 最大 480 回" in m and "上限なし" in m for m in logs)


@pytest.mark.parametrize("provider", runner.LIMITED_PROVIDERS)
def test_claude_over_limit_does_not_start(eval_dir, provider):
    fake = FakeEval()
    with pytest.raises(improve.ImproveError) as e:
        run(fake, provider=provider)
    assert "上限" in str(e.value) and "--max-iterations" in str(e.value) and "--no-answer" in str(e.value)
    assert fake.calls == []
    # 減らせば始める: 45 + 1×(1×30 + 15) + 30 = 120 回
    result = run(FakeEval(), provider=provider, max_iterations=1, candidates=1)
    assert result["llm_calls_estimate"] == 120 and result["max_llm_calls"] == 200
    # --no-answer は LLM を呼ばない（見込み 0）
    assert run(FakeEval(answer=False), provider=provider, answer=False)["llm_calls_estimate"] == 0


def test_provider_outside_allow_list_is_error(eval_dir):
    fake = FakeEval()
    with pytest.raises(improve.ImproveError):
        run(fake, provider="anthropic_api")
    assert fake.calls == []


@pytest.mark.parametrize("n", [0, 5])
def test_candidates_out_of_range_is_error(eval_dir, n):
    with pytest.raises(improve.ImproveError):
        run(FakeEval(), candidates=n)


def test_no_answer_uses_score_retrieval(eval_dir):
    result = run(FakeEval(answer=False), answer=False)
    assert result["score_key"] == "score_retrieval" and result["llm_calls"] == 0
    for it in result["iterations"]:
        assert not {c["axis"] for c in it["candidates"]} & set(improve.ANSWER_AXES)


# ---------------------------------------------------------------- 結果のファイル

def _digest(paths):
    return {p: hashlib.sha256(p.read_bytes()).hexdigest() for p in paths}


def test_result_file_and_report(eval_dir):
    result = run(FakeEval(), seed=1)
    tuned = json.loads(open(result["paths"]["tuned"], encoding="utf-8").read())
    assert result["paths"]["tuned"].startswith(str(eval_dir / "tuned"))
    assert result["paths"]["report"].endswith("-improve.md")
    assert tuned["qa_version"] == "qa-t" and tuned["provider"] == "ollama"
    assert tuned["start_tuning"] == Tuning().to_dict()
    assert improve.tuning_from_dict(tuned["tuned"]) == improve.tuning_from_dict(tuned)
    for when in ("start", "final"):
        assert set(tuned["scores"][when]) == {"dev", "val", "test"}
    assert tuned["scores"]["final"]["dev"] > tuned["scores"]["start"]["dev"]
    assert len(tuned["iterations"]) == 3
    for it in tuned["iterations"]:
        assert it["candidates"] and "reason" in it and "adopted" in it
        assert all({"axis", "from", "to", "dev", "gain"} <= set(c) for c in it["candidates"])
    assert tuned["llm_calls"] > 0 and tuned["llm_calls_estimate"] == 480
    assert tuned["settings"]["max_iterations"] == 3 and tuned["settings"]["seed"] == 1
    md = open(result["paths"]["report"], encoding="utf-8").read()
    for text in ("# 改善のループ（qa-t）", "## 前後の比較", "| test | score |", "## データソース別の変化",
                 "| obsidian |", "## 形別の変化", "## 採用した変更", "1 回目:", "## 各回の候補", "過学習の防止"):
        assert text in md
    # 2 度目は別の名前
    again = run(FakeEval())
    assert again["paths"]["tuned"] != result["paths"]["tuned"]


def test_does_not_change_production_files(eval_dir, tmp_path):
    names = ["tuning.py", "config.py", "knowledge_engine.py", "orchestrator.py",
             "evaluation/synonyms.json", "evaluation/improve.py", "evaluation/runner.py"]
    paths = [config.BASE_DIR / n for n in names]
    before = _digest(paths)
    run(FakeEval())
    assert _digest(paths) == before
    written = sorted(p.relative_to(tmp_path).parts[:2] for p in tmp_path.rglob("*") if p.is_file())
    assert {w for w in written} <= {("eval", "tuned"), ("eval", "reports")}


def test_start_setting_from_file(eval_dir, tmp_path):
    path = tmp_path / "start.json"
    words = list(improve.EXTRA_KNOWLEDGE_WORDS)
    path.write_text(json.dumps({"search_limit": 12, "extra_knowledge_words": words}), encoding="utf-8")
    start = improve.load_start(path)
    assert start == Tuning(search_limit=12, extra_knowledge_words=tuple(words))
    fake = FakeEval()
    run(fake, start=start, max_iterations=1)
    assert fake.calls[0] == (start, "dev")
    with pytest.raises(ValueError):
        improve.tuning_from_dict({"unknown": 1})


@pytest.mark.parametrize("values", [{"search_limit": 99}, {"context_chars": 2000}, {"score_threshold": 7.0},
                                    {"extra_knowledge_words": ["物件"]}, {"synonyms": {"a": ["b"]}},
                                    {"answer_prompt": "other"}])
def test_start_outside_space_is_error(eval_dir, values):
    # 開始の設定に SPACE の外の値があれば評価を始めない（外の値を候補に持ち込まない）
    fake = FakeEval()
    with pytest.raises(improve.ImproveError):
        run(fake, start=improve.tuning_from_dict(values))
    assert fake.calls == []


# ---------------------------------------------------------------- 同義語の辞書

def test_synonyms_file_shape():
    data = json.loads(improve.SYNONYMS_PATH.read_text(encoding="utf-8"))
    pairs = data["pairs"]
    assert 10 <= len(pairs) <= 20
    for pair in pairs:
        assert len(pair) == 2 and all(isinstance(w, str) and w.strip() for w in pair) and pair[0] != pair[1]
    syn = improve.load_synonyms()
    assert "所在地" in syn["住所"] and "住所" in syn["所在地"]       # 両向き
    assert SPACE["synonyms"] == ({}, syn)


def test_synonyms_with_empty_side_is_error(tmp_path):
    path = tmp_path / "bad.json"
    path.write_text(json.dumps({"pairs": [["住所", " "]]}), encoding="utf-8")
    with pytest.raises(ValueError):
        improve.load_synonyms(path)


# ---------------------------------------------------------------- CLI

QUESTION = {"question": {"type": "string", "required": True}}
INDEX = {name: {"description": desc, "status": "active", "permission": "read",
                "entry": {"repo": "repo-1003-jarvis-claude", "function": "x"}, "inputs": inputs, "phrases": phrases}
         for name, desc, inputs, phrases in (
             ("general-chat", "通常の会話に答える（検索しない）", QUESTION, []),
             ("knowledge-answer", "普通の質問に出典付きで答える", QUESTION, []),
             ("knowledge-stats", "ナレッジの件数を集計する", {"limit": {"type": "integer"}}, ["件数", "何件"]))}


@pytest.fixture
def cli_env(tmp_path, monkeypatch, db_path, eval_dir):
    path = tmp_path / "skills.json"
    path.write_text(json.dumps({"skills": INDEX}, ensure_ascii=False), encoding="utf-8")
    monkeypatch.setenv("JARVIS_SKILLS_JSON", str(path))
    monkeypatch.setattr(config, "LATENCY_LOG", tmp_path / "latency" / "latency.jsonl")
    skills.reset_cache()

    def refuse(*a, **k):
        raise AssertionError("LLM を呼ばない")
    monkeypatch.setattr(llm, "complete", refuse)
    monkeypatch.setattr(llm, "complete_stream", refuse)
    yield
    skills.reset_cache()


def test_cli_improve_in_isolated_child(cli_env, monkeypatch, capsys):
    """子のプロセスとしての動き（隔離は済んでいる前提）: 偽でない runner で --no-answer の改善を回す。"""
    monkeypatch.setenv(isolate.ISOLATED_ENV, "1")
    kids = [db.insert_node("Knowledge", category="minpaku", title=f"知識{i}", content=f"本文{i} " + "説明の文章。" * 5,
                           url=f"https://example.com/{i}") for i in range(3)]
    items = [{"id": f"q{i}", "question": f"大洗の物件の所在地は？{i}", "key_facts": ["本文"], "knowledge_id": kids[i],
              "source": "web", "style": "direct", "group": f"q{i}", "split": split}
             for i, split in enumerate(("dev", "val", "test"))]
    qa.save(items, "qa-i1", {"seed": 0})
    assert cli.main(["improve", "--qa", "qa-i1", "--no-answer", "--max-iterations", "1"]) == 0
    out = capsys.readouterr().out
    assert "見込み: 最大 0 回" in out and "1 回目: 候補" in out and "設定の提案:" in out
    tuned = sorted((evaluation.EVAL_DIR / "tuned").glob("*.json"))
    data = json.loads(tuned[0].read_text(encoding="utf-8"))
    assert data["qa_version"] == "qa-i1" and data["answer"] is False and data["llm_calls"] == 0
    assert data["source_db"] == str(config.DB_PATH)
    assert sorted((evaluation.EVAL_DIR / "reports").glob("*-improve.md"))


def test_cli_checks_arguments_before_copying(cli_env, monkeypatch, tmp_path):
    copied = []
    monkeypatch.setattr(isolate, "run_isolated", lambda *a, **k: copied.append(a) or 0)
    assert cli.main(["improve", "--qa", "qa-none"]) == 2
    qa.save([], "qa-e", {})
    assert cli.main(["improve", "--qa", "qa-e", "--max-iterations", "6"]) == 2
    assert cli.main(["improve", "--qa", "qa-e", "--candidates", "5"]) == 2
    assert cli.main(["improve", "--qa", "qa-e", "--provider", "anthropic_api"]) == 2
    assert cli.main(["improve", "--qa", "qa-e", "--start", str(tmp_path / "none.json")]) == 2
    outside = tmp_path / "outside.json"
    outside.write_text(json.dumps({"search_limit": 99}), encoding="utf-8")
    assert cli.main(["improve", "--qa", "qa-e", "--start", str(outside)]) == 2
    assert copied == []
    start = tmp_path / "s.json"
    start.write_text(json.dumps({"search_limit": 6}), encoding="utf-8")
    monkeypatch.chdir(tmp_path)
    assert cli.main(["improve", "--qa", "qa-e", "--start", "s.json"]) == 0
    args = copied[0][0]
    assert args[:3] == ["improve", "--qa", "qa-e"] and args[args.index("--start") + 1] == str(start.resolve())


def test_cli_claude_over_limit_fails_in_child(cli_env, monkeypatch, capsys):
    monkeypatch.setenv(isolate.ISOLATED_ENV, "1")
    qa.save(make_items(), "qa-big", {})
    assert cli.main(["improve", "--qa", "qa-big", "--provider", "claude_warm"]) == 1
    err = capsys.readouterr().err
    assert "480 回" in err and "上限（200 回）" in err
    assert not (evaluation.EVAL_DIR / "tuned").exists()
