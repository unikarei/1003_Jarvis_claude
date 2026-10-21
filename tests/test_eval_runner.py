"""評価部門の評価の実行・指標・レポート（T-1411、ADR-0030）と、調整できる値（tuning.py）のテスト。
DB・索引・保存先は一時のもの、LLM は偽物（本物の LLM・本番の DB は使わない）。"""

import json
from types import SimpleNamespace

import pytest

import config
import db
import evaluation
import knowledge_engine as ke
import latency
import llm
import orchestrator as orc
import skills
import tuning
from evaluation import __main__ as cli
from evaluation import isolate, qa, report, runner
from tuning import Tuning

R1003 = "repo-1003-jarvis-claude"
QUESTION = {"question": {"type": "string", "required": True}}


def _s(desc, phrases=(), inputs=None):
    return {"description": desc, "status": "active", "permission": "read", "entry": {"repo": R1003, "function": "x"},
            "inputs": inputs or {}, "phrases": list(phrases)}


INDEX = {
    "general-chat": _s("通常の会話に答える（検索しない）", inputs=QUESTION),
    "knowledge-answer": _s("普通の質問に出典付きで答える", inputs=QUESTION),
    "knowledge-stats": _s("ナレッジの件数を集計する", ["件数", "何件"],
                          {"limit": {"type": "integer"}, "by": {"type": "string"}}),
}


@pytest.fixture
def eval_dir(tmp_path, monkeypatch):
    path = tmp_path / "eval"
    monkeypatch.setattr(evaluation, "EVAL_DIR", path)
    for name in (isolate.ISOLATED_ENV, isolate.SOURCE_DB_ENV, isolate.COPIED_AT_ENV):
        monkeypatch.delenv(name, raising=False)
    return path


@pytest.fixture
def prod_latency(tmp_path, monkeypatch):
    """本番の遅延の記録（の代わりの一時の場所）。評価では書かれないことを確かめる。"""
    path = tmp_path / "latency" / "latency.jsonl"
    monkeypatch.setattr(config, "LATENCY_LOG", path)
    return path


@pytest.fixture
def env(tmp_path, monkeypatch, db_path, eval_dir, prod_latency):
    """一時の DB・索引。振り分けの LLM（llm.complete）は呼ばれたら記録する。"""
    path = tmp_path / "skills.json"
    path.write_text(json.dumps({"skills": INDEX}, ensure_ascii=False), encoding="utf-8")
    monkeypatch.setenv("JARVIS_SKILLS_JSON", str(path))
    skills.reset_cache()
    route_llm = []

    def refuse(*a, **k):
        route_llm.append(a)
        raise llm.LLMError("評価の振り分けで LLM を呼ばない")
    monkeypatch.setattr(llm, "complete", refuse)
    yield SimpleNamespace(route_llm=route_llm)
    skills.reset_cache()


@pytest.fixture
def fake_llm(monkeypatch):
    """偽の逐次の LLM。answers の質問に対応する答えを 2 回に分けて on_text に渡す。"""
    state = SimpleNamespace(prompts=[], providers=[], answers={}, fail=set())

    def stream(prompt, on_text, provider=None, timeout=None, cancel=None):
        state.prompts.append(prompt)
        state.providers.append(provider)
        text = next((a for q, a in state.answers.items() if f"# 質問\n{q}\n" in prompt), "わかりません")
        if any(f"# 質問\n{q}\n" in prompt for q in state.fail):
            raise llm.LLMError("偽の失敗")
        on_text(text[:2])
        on_text(text[2:])
        return text
    monkeypatch.setattr(llm, "complete_stream", stream)
    return state


def add(title, content, category="minpaku"):
    return db.insert_node("Knowledge", category=category, title=title, content=content, url=f"https://example.com/{title}")


def item(qid, question, kid, facts=("答え",), source="web", style="direct", split="dev"):
    return {"id": qid, "question": question, "key_facts": list(facts), "knowledge_id": kid, "title": "",
            "source": source, "style": style, "group": qid, "split": split}


@pytest.fixture
def kids(env):
    return [add(f"知識{i}", f"本文{i} " + "説明の文章。" * 5) for i in range(10)]


def fake_search(monkeypatch, order: dict, calls=None):
    """質問 → 知識 ID の並び、の偽の検索（limit で切る）。"""
    def search(query, projects=None, limit=10, synonyms=None):
        if calls is not None:
            calls.append((query, limit))
        if order.get(query) == "error":
            raise RuntimeError("偽の検索の失敗")
        return [ke.Hit(kid, kid, 10.0 - i, [], None, "2026-10-01", "") for i, kid in enumerate(order.get(query, []))][:limit]
    monkeypatch.setattr(ke, "search", search)


# ---------------------------------------------------------------- tuning.py

def test_default_tuning_is_todays_behavior():
    t = tuning.current()
    assert t == Tuning() == tuning.DEFAULT
    assert (t.search_limit, t.context_chars, t.score_threshold, t.extra_knowledge_words, t.synonyms,
            t.answer_prompt) == (8, 1500, None, (), {}, "default")
    assert ke.ANSWER_PROMPTS["default"] is ke.ANSWER_PROMPT


def test_use_restores_value_even_on_error():
    t = Tuning(search_limit=3)
    with tuning.use(t):
        assert tuning.current() is t
        with tuning.use(Tuning(search_limit=5)):
            assert tuning.current().search_limit == 5
        assert tuning.current() is t
    assert tuning.current() == Tuning()
    with pytest.raises(ZeroDivisionError):
        with tuning.use(t):
            1 / 0
    assert tuning.current() == Tuning()


def test_tuning_reaches_answer_prompt_and_search(env, fake_llm, monkeypatch):
    for i in range(10):
        add(f"大洗の物件{i}", "あ" * 2000)       # 題名で当たる
    limits = []
    real = ke.search
    monkeypatch.setattr(ke, "search", lambda q, **k: limits.append(k.get("limit")) or real(q, **k))
    ke.answer("大洗の物件は？", on_text=lambda c: None)
    prompt = fake_llm.prompts[-1]
    assert limits[-1] == 8 and "[8]" in prompt and "[9]" not in prompt
    assert prompt.startswith(ke.ANSWER_PROMPT.split("\n")[0]) and "あ" * 1500 in prompt and "あ" * 1501 not in prompt
    with tuning.use(Tuning(search_limit=2, context_chars=30, answer_prompt="grounded")):
        ke.answer("大洗の物件は？", on_text=lambda c: None)
    prompt = fake_llm.prompts[-1]
    assert limits[-1] == 2 and "[2]" in prompt and "[3]" not in prompt
    assert "必ず資料から答える" in prompt and "あ" * 30 in prompt and "あ" * 31 not in prompt
    # 引数で渡せば tuning より優先
    ke.answer("大洗の物件は？", on_text=lambda c: None, limit=1, prompt="grounded")
    assert limits[-1] == 1 and "必ず資料から答える" in fake_llm.prompts[-1]
    with pytest.raises(ValueError):
        ke.answer("大洗の物件は？", prompt="nope")


def test_tuning_reaches_is_knowledge_question(env, monkeypatch):
    monkeypatch.setattr(config, "KNOWLEDGE_SCORE_THRESHOLD", 8.0)
    monkeypatch.setattr(ke, "search", lambda *a, **k: [])
    assert not orc.is_knowledge_question("ゴルフの予定は？")
    with tuning.use(Tuning(extra_knowledge_words=("ゴルフ",))):
        assert orc.is_knowledge_question("ゴルフの予定は？")
    monkeypatch.setattr(ke, "search", lambda *a, **k: [SimpleNamespace(score=5.0)])
    assert not orc.is_knowledge_question("水戸の様子は？")
    with tuning.use(Tuning(score_threshold=5.0)):
        assert orc.is_knowledge_question("水戸の様子は？")
        assert orc.route("水戸の様子は？", orc.Context(), use_llm=False).skill == "knowledge-answer"
    assert orc.route("水戸の様子は？", orc.Context(), use_llm=False).skill == "general-chat"


def test_synonyms_reach_is_knowledge_question(env):
    add("物件の情報", "所在地は茨城県の水戸市です。")
    with tuning.use(Tuning(score_threshold=0.01)):
        assert not orc.is_knowledge_question("住所を教えて")
    with tuning.use(Tuning(score_threshold=0.01, synonyms={"住所": ["所在地"]})):
        assert orc.is_knowledge_question("住所を教えて")


def test_synonym_expansion_finds_paraphrased_knowledge(env):
    kid = add("物件の情報", "所在地は茨城県の水戸市です。")
    add("別の話", "ピアノの練習の記録。")
    assert ke.search("住所はどこ？") == []
    assert [h.knowledge_id for h in ke.search("住所はどこ？", synonyms={"住所": ["所在地"]})] == [kid]
    with tuning.use(Tuning(synonyms={"住所": ["所在地"]})):
        assert [h.knowledge_id for h in ke.search("住所はどこ？")] == [kid]
    assert ke.search("所在地", synonyms={"住所": ["所在地"]})[0].knowledge_id == kid   # 逆向きは辞書に書いたときだけ
    assert ke.search("住所はどこ？", synonyms={"所在地": ["住所"]}) == []


# ---------------------------------------------------------------- 指標

def test_facts_hit_and_said_none_are_normalized():
    assert runner.facts_hit("所在地は　ＡＢＣ－１２３、です。", ["abc123", "所在地"]) == 1.0
    assert runner.facts_hit("水戸 市", ["水戸市", "大洗町"]) == 0.5
    assert runner.facts_hit("x", []) == 0.0
    assert runner.said_none("資料には見つかりません。") and runner.said_none("それは 分かりません")
    assert runner.said_none("資料に ない ようです") and not runner.said_none("水戸市です")


def test_run_metrics_and_missing_values(env, kids, fake_llm, monkeypatch):
    k = kids
    called = []
    monkeypatch.setitem(orc.HANDLERS, "knowledge-stats", lambda *a: called.append(a))
    monkeypatch.setitem(orc.HANDLERS, "knowledge-answer", lambda *a: called.append(a))
    q1, q2, q3, q4 = "大洗の物件Aの所在地は？", "大洗の物件Bの広さは？", "大洗の物件Cの価格は？", "知識の件数は？"
    fake_search(monkeypatch, {q1: [k[0], k[1]], q2: [k[1], k[2], k[0]], q3: k[1:9] + [k[0]]})
    fake_llm.answers = {q1: "所在地は　水戸市ＡＢＣ です [1]", q2: "広さは 50 平米です", q3: "資料には見つかりません"}
    items = [item("q1", q1, k[0], ["水戸市abc"]), item("q2", q2, k[0], ["50平米", "南向き"]),
             item("q3", q3, k[0], ["1000万円"]), item("q4", q4, k[0], ["10件"])]
    result = runner.run(items, provider="ollama", log=lambda m: None)
    r1, r2, r3, r4 = result["items"]
    assert (r1["routed"], r1["rank"], r1["recall@1"], r1["recall_eff"], r1["facts_hit"], r1["said_none"],
            r1["failed_at"]) == (True, 1, 1, 1, 1.0, False, None)
    assert (r2["rank"], r2["recall@1"], r2["recall@3"], r2["recall@8"], r2["facts_hit"], r2["failed_at"]) == \
        (3, 0, 1, 1, 0.5, "answer")
    # 根拠が 8 位以内にない問も答えは作って要点を測る
    assert (r3["rank"], r3["recall@8"], r3["recall_eff"], r3["facts_hit"], r3["said_none"], r3["failed_at"]) == \
        (None, 0, 0, 0.0, True, "search")
    # ほかのスキルに振り分けられた問: ハンドラーも LLM も呼ばない
    assert (r4["skill"], r4["routed"], r4["rank"], r4["recall@8"], r4["facts_hit"], r4["said_none"],
            r4["failed_at"]) == ("knowledge-stats", False, None, 0, 0.0, False, "route")
    assert called == [] and result["llm_calls"] == 3 and len(fake_llm.prompts) == 3
    assert set(fake_llm.providers) == {"ollama"}
    o = result["summary"]["overall"]
    assert o["route_acc"] == 0.75 and o["recall@1"] == 0.25 and o["recall@3"] == 0.5 and o["recall_eff"] == 0.5
    assert o["mrr"] == round((1 + 1 / 3) / 4, 4)          # 見つからない問は 0 として全問で平均
    assert o["facts_hit"] == round(1.5 / 4, 4) and o["none_rate"] == 0.25
    assert o["score"] == round(0.2 * 0.75 + 0.3 * 0.5 + 0.5 * 0.375, 4)
    assert o["score_retrieval"] == round(0.4 * 0.75 + 0.6 * 0.5, 4)


def test_exceptions_set_failed_stage_and_zero_values(env, kids, fake_llm, monkeypatch):
    k = kids
    q1, q2 = "大洗の検索が壊れる問", "大洗の答えが壊れる問"
    fake_search(monkeypatch, {q1: "error", q2: [k[0]]})
    fake_llm.fail = {q2}
    result = runner.run([item("q1", q1, k[0]), item("q2", q2, k[0])], log=lambda m: None)
    r1, r2 = result["items"]
    assert (r1["failed_at"], r1["error"], r1["routed"], r1["rank"], r1["recall_eff"], r1["facts_hit"],
            r1["said_none"]) == ("search", "RuntimeError", True, None, 0, 0.0, False)
    assert (r2["failed_at"], r2["error"], r2["rank"], r2["recall_eff"], r2["facts_hit"], r2["said_none"]) == \
        ("answer", "LLMError", 1, 1, 0.0, False)
    no_answer = runner.run([item("q1", q1, k[0])], answer=False, log=lambda m: None)["items"][0]
    assert (no_answer["failed_at"], no_answer["facts_hit"], no_answer["said_none"]) == ("search", None, None)


def test_no_answer_does_not_call_llm(env, kids, monkeypatch):
    def refuse(*a, **k):
        raise AssertionError("--no-answer で LLM を呼ばない")
    monkeypatch.setattr(llm, "complete_stream", refuse)
    q = "大洗の物件の所在地は？"
    fake_search(monkeypatch, {q: [kids[1], kids[0]]})
    result = runner.run([item("q1", q, kids[0])], answer=False, log=lambda m: None)
    r, o = result["items"][0], result["summary"]["overall"]
    assert (r["rank"], r["facts_hit"], r["said_none"], r["answer"], r["failed_at"]) == (2, None, None, None, None)
    assert result["llm_calls"] == 0 and result["llm_calls_estimate"] == 0
    assert o["facts_hit"] is None and o["none_rate"] is None and o["score"] is None
    assert o["score_retrieval"] == 1.0


def test_provider_allow_list(env, kids, fake_llm):
    for bad in ("anthropic_api", "claude", ""):
        with pytest.raises(runner.EvalError):
            runner.run([item("q1", "大洗の問", kids[0])], provider=bad, log=lambda m: None)
    assert fake_llm.prompts == []


def test_claude_estimate_over_limit_does_not_start(env, kids, fake_llm, monkeypatch):
    fake_search(monkeypatch, {})
    items = [item(f"q{i}", f"大洗の問{i}", kids[0]) for i in range(3)]
    logs = []
    for provider in ("claude_warm", "claude_cli"):
        with pytest.raises(runner.EvalError):
            runner.run(items, provider=provider, max_llm_calls=2, log=logs.append)
    assert fake_llm.prompts == [] and "見込み: 3 回" in logs[0]
    # ollama は見込みを表示するだけで上限をかけない
    result = runner.run(items, provider="ollama", max_llm_calls=2, log=logs.append)
    assert result["llm_calls"] == 3 and result["stopped"] is None and result["max_llm_calls"] is None
    assert "見込み: 3 回" in logs[-1]
    # 上限ちょうどなら始める
    assert runner.run(items, provider="claude_warm", max_llm_calls=3, log=logs.append)["llm_calls"] == 3


def test_claude_stops_at_limit_during_run(env, kids, fake_llm, monkeypatch):
    fake_search(monkeypatch, {})
    monkeypatch.setattr(runner, "estimate_llm_calls", lambda items, answer: 0)   # 見込みを外して実行中の上限を試す
    items = [item(f"q{i}", f"大洗の問{i}", kids[0]) for i in range(4)]
    result = runner.run(items, provider="claude_cli", max_llm_calls=2, log=lambda m: None)
    assert result["stopped"] == "max_llm_calls" and result["llm_calls"] == 2
    assert len(result["items"]) == 2 and len(fake_llm.prompts) == 2
    assert latency.current() is None


def test_route_with_llm_setting_is_ignored_in_evaluation(env, kids, fake_llm, monkeypatch):
    monkeypatch.setattr(config, "ROUTE_WITH_LLM", True)
    monkeypatch.setattr(config, "KNOWLEDGE_SCORE_THRESHOLD", 8.0)
    fake_search(monkeypatch, {})
    result = runner.run([item("q1", "こんにちは", kids[0]), item("q2", "大洗の問", kids[0])], log=lambda m: None)
    assert env.route_llm == []
    assert [r["skill"] for r in result["items"]] == ["general-chat", "knowledge-answer"]
    assert all(r["error"] is None for r in result["items"])


def test_search_limit_decides_recall_eff_and_latency_file(env, kids, fake_llm, monkeypatch, prod_latency):
    q = "大洗の物件の所在地は？"
    calls = []
    fake_search(monkeypatch, {q: kids[1:7] + [kids[0]] + kids[7:]}, calls)   # 根拠は 7 位
    fake_llm.answers = {q: "答えです"}
    path = evaluation.EVAL_DIR / "latency" / "t.jsonl"
    result = runner.run([item("q1", q, kids[0])], tuning=Tuning(search_limit=6), log=lambda m: None,
                        latency_path=path)
    r = result["items"][0]
    assert (r["rank"], r["recall@8"], r["recall_eff"], r["failed_at"]) == (7, 1, 0, "search")
    assert (q, 8) in calls and (q, 6) in calls       # 順位は max(8, search_limit)、答えは search_limit で検索
    assert result["tuning"]["search_limit"] == 6 and tuning.current() == Tuning()
    records = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]
    assert len(records) == 1 and {"route", "search", "first_text", "done"} <= set(records[0]["stages"])
    assert records[0]["skill"] == "knowledge-answer" and records[0]["provider"] == "ollama"
    assert r["stages"] == records[0]["stages"]
    assert not prod_latency.exists()               # 本番の遅延の記録には書かない
    assert result["summary"]["overall"]["stages"]["first_text"]["p50"] is not None


def test_default_latency_file_is_under_eval_dir(env, kids, fake_llm, monkeypatch, prod_latency):
    fake_search(monkeypatch, {})
    result = runner.run([item("q1", "大洗の問", kids[0])], log=lambda m: None)
    path = evaluation.EVAL_DIR / "latency"
    assert result["latency_log"].startswith(str(path)) and len(list(path.glob("*.jsonl"))) == 1
    assert not prod_latency.exists()


def test_summary_groups_and_report(env, kids, fake_llm, monkeypatch, tmp_path):
    k = kids
    qs = [f"大洗の問{i}" for i in range(4)]
    fake_search(monkeypatch, {qs[0]: [k[0]], qs[1]: [k[1]], qs[2]: [k[1], k[2]], qs[3]: []})
    fake_llm.answers = {qs[0]: "答え" + "長" * 400, qs[1]: "答え", qs[2]: "ちがう", qs[3]: "ありません"}
    items = [item("q1", qs[0], k[0], source="web", style="direct", split="dev"),
             item("q2", qs[1], k[1], source="web", style="asr", split="val"),
             item("q3", qs[2], k[2], source="vault-x", style="asr", split="test"),
             item("q4", qs[3], k[3], source="vault-x", style="direct", split="dev")]
    result = runner.run(items, log=lambda m: None)
    s = result["summary"]
    assert list(s["by_split"]) == ["dev", "val", "test"]
    assert s["by_source"]["web"]["count"] == 2 and s["by_source"]["web"]["recall@1"] == 1.0
    assert s["by_source"]["vault-x"]["recall@3"] == 0.5 and s["by_source"]["vault-x"]["mrr"] == 0.25
    assert s["by_style"]["asr"]["facts_hit"] == 0.5 and s["by_style"]["direct"]["none_rate"] == 0.5
    assert s["by_split"]["dev"]["score"] == round(0.2 * 1 + 0.3 * 0.5 + 0.5 * 0.5, 4)
    result.update({"qa_version": "qa-test", "split": "all"})
    json_path, md_path = report.write(result, stamp="20261022-120000")
    assert json_path.parent == evaluation.EVAL_DIR / "reports"
    data = json.loads(json_path.read_text(encoding="utf-8"))
    assert len(data["items"][0]["answer"]) == 300 and len(result["items"][0]["answer"]) > 300
    assert data["summary"] == json.loads(json.dumps(s)) and data["tuning"] == Tuning().to_dict()
    md = md_path.read_text(encoding="utf-8")
    for text in ("# 評価レポート（qa-test）", "### 組別", "### データソース別", "### 形別", "## 失敗の多いデータソース",
                 "## 失敗の多い形", "## 失敗した問（2 問）", "| q3 | 答え |", "| q4 | 検索 |", "## 設定の値",
                 '"search_limit": 8', "段階ごとの時間"):
        assert text in md, text
    with pytest.raises(FileExistsError):
        report.write(result, stamp="20261022-120000")      # 上書きしない


def test_two_runs_give_same_retrieval_metrics(env):
    k1 = add("大洗の物件の所在地", "所在地は大洗町の海の近く。")
    k2 = add("大洗の補助金", "民泊の補助金の条件。")
    add("ピアノ教室", "ピアノ教室の集客。")
    items = [item("q1", "大洗の物件の所在地は？", k1), item("q2", "民泊の補助金は？", k2), item("q3", "こんにちは", k1),
             item("q4", "ピアノの件数は？", k1)]
    runs = [runner.run(items, answer=False, log=lambda m: None) for _ in range(2)]
    keys = ("routed", "skill", "rank", "recall@1", "recall@3", "recall@8", "recall_eff", "failed_at")
    a, b = ([{key: r[key] for key in keys} for r in run["items"]] for run in runs)
    assert a == b
    metric = ("route_acc", "recall@1", "recall@3", "recall@8", "recall_eff", "mrr", "score_retrieval")
    assert {m: runs[0]["summary"]["overall"][m] for m in metric} == {m: runs[1]["summary"]["overall"][m] for m in metric}
    assert runs[0]["items"][0]["rank"] == 1 and runs[0]["items"][3]["skill"] == "knowledge-stats"


# ---------------------------------------------------------------- CLI

def test_cli_run_in_isolated_child(env, kids, monkeypatch, capsys):
    """子のプロセスとしての動き（隔離は済んでいる前提）: 評価セットを読み、組で絞り、レポートを書く。"""
    monkeypatch.setenv(isolate.ISOLATED_ENV, "1")
    items = [item("q1", "大洗の物件の所在地は？", kids[0], split="dev"),
             item("q2", "大洗の補助金は？", kids[1], split="val")]
    qa.save(items, "qa-t1", {"seed": 0})

    def refuse(*a, **k):
        raise AssertionError("--no-answer で LLM を呼ばない")
    monkeypatch.setattr(llm, "complete_stream", refuse)
    assert cli.main(["run", "--qa", "qa-t1", "--split", "dev", "--no-answer"]) == 0
    out = capsys.readouterr().out
    assert "見込み: 0 回" in out and "評価: 1 問" in out and "score_retrieval" in out
    reports = sorted((evaluation.EVAL_DIR / "reports").glob("*.json"))
    data = json.loads(reports[0].read_text(encoding="utf-8"))
    assert (data["qa_version"], data["split"], len(data["items"])) == ("qa-t1", "dev", 1)
    assert data["source_db"] == str(config.DB_PATH)


def test_cli_checks_arguments_before_copying(env, monkeypatch, capsys):
    copied = []
    monkeypatch.setattr(isolate, "run_isolated", lambda *a, **k: copied.append(a) or 0)
    assert cli.main(["run", "--qa", "qa-none"]) == 2
    assert cli.main(["run", "--qa", "../x"]) == 2
    with pytest.raises(SystemExit):
        cli.main(["run", "--qa", "qa-none", "--provider", "anthropic_api"])
    assert copied == []
    qa.save([], "qa-empty", {})
    assert cli.main(["run", "--qa", "qa-empty", "--provider", "claude_warm", "--max-llm-calls", "5"]) == 0
    assert copied and copied[0][0][:2] == ["run", "--qa"]


def test_cli_claude_over_limit_fails_in_child(env, kids, fake_llm, monkeypatch, capsys):
    monkeypatch.setenv(isolate.ISOLATED_ENV, "1")
    qa.save([item(f"q{i}", f"大洗の問{i}", kids[0]) for i in range(3)], "qa-t2", {})
    assert cli.main(["run", "--qa", "qa-t2", "--provider", "claude_warm", "--max-llm-calls", "2"]) == 1
    assert "上限" in capsys.readouterr().err and fake_llm.prompts == []
