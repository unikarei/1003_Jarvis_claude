"""T-2103: 実 LLM やソケットを使わない記憶評価の受け入れテスト。"""
from collections import Counter
from copy import deepcopy
import importlib
import json
import os
from pathlib import Path

import pytest

import config
import db
import llm
import orchestrator
from evaluation.memory import runner
from memory import store


def sample(kind="continue"):
    return deepcopy(next(s for s in runner.load_scenarios()["scenarios"] if s["kind"] == kind))


def test_dataset_covers_six_kinds_and_both_preference_sources():
    data = runner.load_scenarios()
    counts = Counter(s["kind"] for s in data["scenarios"])
    assert data["fictional"] and len(data["scenarios"]) >= 30
    assert set(counts) == set(runner.KINDS)
    assert all(n >= 4 for n in counts.values())
    preferences = [s for s in data["scenarios"] if s["kind"] == "preference"]
    assert any(s.get("preferences") for s in preferences)
    assert any(not s.get("preferences") for s in preferences)
    assert {len(s["questions"]) for s in data["scenarios"]} == {1, 2}


@pytest.mark.parametrize("mutate", [
    lambda d: d.update(fictional=False),
    lambda d: d["scenarios"].append(deepcopy(d["scenarios"][0])),
    lambda d: d["scenarios"][0].update(kind="unknown"),
    lambda d: d["scenarios"][0].update(history=[]),
    lambda d: d["scenarios"][0]["questions"][0].update(checks={"rubric": ["主観"]}),
    lambda d: d["scenarios"][0]["questions"][0].update(checks={"must_any": []}),
    lambda d: d["scenarios"][0]["history"][0].update(at="2027-01-01T00:00:00+09:00"),
])
def test_invalid_dataset_is_rejected(tmp_path, mutate):
    data = {"fictional": True, "scenarios": [sample()]}
    mutate(data)
    path = tmp_path / "invalid.json"
    path.write_text(json.dumps(data), encoding="utf-8")
    with pytest.raises(ValueError):
        runner.load_scenarios(path)


@pytest.mark.parametrize("response,expected", [
    ("はい、覚えています。", True),
    ("覚えている内容では青でした。", True),
    ("ええ、覚えております。", True),
    ("前に話した内容では三番です。", True),
    ("以前に話したように青です。", True),
    ("記憶にございます。", True),
    ("記憶にあります。", True),
    ("その話は記憶にありません。", False),
    ("覚えていません。", False),
    ("覚えているとは言えません。", False),
    ("「覚えている」とは言えません。", False),
    ("前に話した記憶はありません。", False),
    ("記憶にあるわけではありません。", False),
    ("覚えているかどうか確認できません。", False),
    ("記憶にございますとは申し上げられません。", False),
    ("記憶にありませんが、前に話した色は青です。", True),
    ("覚えています。ただし日付はわかりません。", True),
    ("覚えていますが詳しくは分かりません。", True),
    ("記憶にございますが日付は不明です。", True),
])
def test_absent_claim_detection(response, expected):
    assert bool(runner.hallucination_matches(response)) is expected


def test_checks_pass_fail_and_no_empty_success():
    checks = {"must_all": ["窓"], "must_any": ["次", "予定"], "must_not": ["屋根"]}
    assert runner.check_response(checks, "次は窓です", kind="continue")["passed"]
    assert not runner.check_response(checks, "次は屋根です", kind="continue")["passed"]
    assert not runner.check_response({"must_not": ["秘密"]}, "", kind="irrelevant")["passed"]
    assert not runner.check_response({}, "返答", kind="decision", ok=False)["passed"]
    assert runner.check_response({}, "覚えています", kind="absent")["hallucination"]
    assert not runner.check_response({}, "覚えています", kind="decision")["hallucination"]


def test_seed_dates_match_jsonl_and_index_and_restore(tmp_path):
    item = sample("temporal")
    before = (config.DB_PATH, db.DB_PATH, config.MEMORY_DIR, os.environ.get("JARVIS_MEMORY"))
    with runner.isolated_scenario(tmp_path, "fake-model") as paths:
        assert runner.seed_history(item, no_summary=True) == 0
        sessions = store.list_sessions()
        assert len(sessions) == 2
        for session, history in zip(sessions, item["history"]):
            assert session["started_at"] == session["last_at"] == session["ended_at"] == history["at"]
            records = [json.loads(line) for line in store._path(session["session_id"]).read_text().splitlines()]
            assert all(record["at"] == history["at"] for record in records)
        assert set(paths) >= {"DB_PATH", "MEMORY_DIR", "VAULT_ROOT", "CKC_DATA_DIR"}
        assert all(path.is_relative_to(tmp_path) for path in paths.values())
        store.rebuild()
        assert store.list_sessions() == sessions
    assert (config.DB_PATH, db.DB_PATH, config.MEMORY_DIR, os.environ.get("JARVIS_MEMORY")) == before


def test_isolation_restores_settings_on_error(tmp_path):
    import registry
    import skills
    before = (config.DB_PATH, db.DB_PATH, registry._cache, registry._loaded, skills._cache)
    with pytest.raises(RuntimeError):
        with runner.isolated_scenario(tmp_path, "fake-model"):
            raise RuntimeError("中断")
    assert (config.DB_PATH, db.DB_PATH, registry._cache, registry._loaded, skills._cache) == before


def fake_ollama(monkeypatch, responses=None):
    prompts = []
    def post(path, payload, timeout):
        assert path == "/api/chat"
        assert llm.OLLAMA_URL == "http://127.0.0.1:11434"
        assert payload["model"] == "fake-model"
        prompt = payload["messages"][0]["content"]
        prompts.append(prompt)
        if "<会話>" in prompt:
            text = "話題: 空色模型\n決まったこと: 屋根は六角形\n残っていること: 次は窓の数\nOwner の好みや事情: なし"
        elif responses is not None:
            text = responses[(len([p for p in prompts if "<会話>" not in p]) - 1) % len(responses)]
        elif "次は窓の数" in prompt:
            text = "次は窓の数です。" if "検討する予定" in prompt.rsplit("# Owner の発言", 1)[-1] else "六角形です。"
        else:
            text = "その話は記憶にありません。"
        return {"message": {"content": text}}
    monkeypatch.setattr(llm, "_ollama_post", post)
    return prompts


def test_real_handle_fake_llm_compares_memory_on_off(monkeypatch):
    prompts = fake_ollama(monkeypatch)
    item = sample()
    before = (config.DB_PATH, db.DB_PATH, config.MEMORY_DIR)
    yes = runner.run_scenario(item, True, model="fake-model")
    no = runner.run_scenario(item, False, model="fake-model")
    assert yes["passed"] and not no["passed"]
    assert all(t["recall_calls"] == 1 and t["recall_seconds"] >= 0 for t in yes["turns"])
    assert all(t["recall_calls"] == 1 for t in no["turns"])
    assert len(prompts) == 6  # 要約1+質問2 を各モードで実行
    assert "次は窓の数" in prompts[1]
    assert "次は窓の数" not in prompts[4]
    assert (config.DB_PATH, db.DB_PATH, config.MEMORY_DIR) == before


def test_no_summary_retrieves_last_turns_without_summary_llm(monkeypatch):
    prompts = fake_ollama(monkeypatch, ["次は窓の数です。", "六角形です。"])
    result = runner.run_scenario(sample(), True, model="fake-model", no_summary=True)
    assert result["passed"]
    assert len(prompts) == 2 and all("<会話>" not in p for p in prompts)
    assert "Owner: 空色模型" in prompts[0]


def test_absent_fabrication_with_fake_llm_is_counted(monkeypatch):
    fake_ollama(monkeypatch, ["覚えています。記憶にありませんが青でした。"])
    result = runner.run_scenario(sample("absent"), True, model="fake-model", no_summary=True)
    assert not result["passed"] and result["turns"][0]["hallucination"]
    summary = runner.summarize_results([result])["with_memory"]
    assert summary["hallucination_count"] == summary["hallucination_scenarios"] == 1
    assert summary["pass_rate"] == 0
    assert summary["failures"][0]["failed_checks"] == ["no_fabricated_memory"]


def test_summary_failure_is_not_silent_fallback(monkeypatch):
    monkeypatch.setattr(llm, "_ollama_post", lambda *a, **k: {"message": {"content": "不正な要約"}})
    result = runner.run_scenario(sample(), True, model="fake-model")
    assert not result["passed"] and result["setup_error"]
    assert all(not turn["checks"]["execution_ok"] for turn in result["turns"])


def test_generation_failure_counts_as_failure(monkeypatch):
    def fail(*args, **kwargs):
        raise llm.LLMError("偽LLMの障害")
    monkeypatch.setattr(llm, "_ollama_post", fail)
    result = runner.run_scenario(sample("absent"), False, model="fake-model", no_summary=True)
    assert not result["passed"]
    assert not result["turns"][0]["checks"]["execution_ok"]


def test_cloud_providers_and_unsafe_handlers_are_blocked(tmp_path):
    from evaluation.conversation.runner import ollama_only
    with runner.isolated_scenario(tmp_path, "fake-model"), ollama_only([0], True):
        assert orchestrator.harness_order(orchestrator.Context(), "answer") == ("ollama",)
        for provider in ("claude_cli", "codex", "anthropic_api"):
            with pytest.raises(llm.LLMError):
                llm.complete("送信禁止", provider=provider)
        with pytest.raises(llm.LLMError):
            llm.ask_claude("送信禁止")
        with pytest.raises(orchestrator.SkillError):
            orchestrator.HANDLERS["code-task"]({}, orchestrator.Context(), "実行禁止")


def test_cli_filters_both_modes_and_writes_json_markdown(tmp_path, monkeypatch):
    fake_ollama(monkeypatch, ["その話は記憶にありません。"])
    assert runner.main(["--kind", "absent", "--limit", "1", "--no-summary", "--model", "fake-model",
                        "--output-dir", str(tmp_path)]) == 0
    result = json.loads(next(tmp_path.glob("*.json")).read_text())
    assert result["partial"] and result["selected"] == 1 and result["total"] == 44
    assert [row["memory"] for row in result["rows"]] == [True, False]
    assert all(row["kind"] == "absent" for row in result["rows"])
    assert all(s["pass_rate"] == 1 for s in result["summary"].values())
    report = next(tmp_path.glob("*.md")).read_text()
    assert "部分実行: True" in report and "作り話 0 件" in report and "想起" in report


def test_output_contains_all_failures_and_scenario_aggregation(tmp_path, monkeypatch):
    fake_ollama(monkeypatch, ["窓です。", "不明です。"])
    row = runner.run_scenario(sample(), True, model="fake-model", no_summary=True)
    assert row["turns"][0]["passed"] and not row["turns"][1]["passed"]
    summary = runner.summarize_results([row])
    assert summary["with_memory"]["pass_rate"] == 0  # 質問平均の0.5ではない
    result = {"stamp": "fake", "model": "fake-model", "summary_enabled": False, "partial": True,
              "selected": 1, "total": 44, "summary": summary, "rows": [row]}
    json_path, md_path = runner.write_results(result, tmp_path)
    assert json.loads(json_path.read_text())["summary"]["with_memory"]["failures"][0]["turn"] == 2
    assert "不合格 continue-01 / 質問 2" in md_path.read_text()


def test_limit_must_be_positive():
    with pytest.raises(SystemExit) as error:
        runner.main(["--limit", "0"])
    assert error.value.code == 2
