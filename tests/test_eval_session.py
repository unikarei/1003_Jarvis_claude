"""セッションの評価（T-2401、evaluation.session）と、評価のハーネスの指定（--harness）。本物の LLM は呼ばない。"""

import json

import pytest

import llm
import orchestrator as orc
from evaluation.conversation import runner as conversation_runner
from evaluation.memory import runner as memory_runner
from evaluation.session import runner


def test_scenarios_are_valid_and_cover_all_kinds():
    data = runner.load_scenarios()
    assert {s["kind"] for s in data["scenarios"]} == set(runner.KINDS)
    switch = [s for s in data["scenarios"] if s["kind"] == "switch"]
    assert switch and all(s["order"] == ["claude", "codex", "ollama"] for s in switch)
    # long: 判定の発言は、参照する話から 4 往復以上あと
    for s in data["scenarios"]:
        if s["kind"] == "long":
            turns = s["conversations"][0]["turns"]
            first_probe = next(i for i, t in enumerate(turns) if t.get("checks"))
            assert first_probe >= 6


def test_load_rejects_unknown_check(tmp_path):
    data = runner.load_scenarios()
    data["scenarios"][0]["conversations"][0]["turns"][-1]["checks"] = {"regex": ["x"]}
    path = tmp_path / "s.json"
    path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    with pytest.raises(ValueError):
        runner.load_scenarios(path)


def test_forced_failures_skip_failed_harness(monkeypatch):
    seen = []

    def fake_real(run, ctx, kind):
        for provider in ("claude_warm", "codex", "ollama"):
            try:
                return run(provider, None)
            except llm.LLMUnavailable:
                seen.append(provider)
        raise AssertionError("どれも答えなかった")
    monkeypatch.setattr(orc, "run_with_harnesses", fake_real)
    with runner.forced_failures({"claude"}):
        assert orc.run_with_harnesses(lambda p, s: p, orc.Context(), "chat") == "codex"
    assert seen == ["claude_warm"]


def test_harness_only_fixes_order_and_counts(monkeypatch):
    counter = [0]
    with conversation_runner.harness_only(counter, "claude"):
        assert orc.harness_order(orc.Context(), "chat") == ("claude",)
    with conversation_runner.harness_only(counter, "claude", ("claude", "codex", "ollama")):
        assert orc.harness_order(orc.Context(), "answer") == ("claude", "codex", "ollama")
        with pytest.raises(llm.LLMError):
            llm.ask_claude("x")                  # 有料の API は使わない


def test_answer_llm_ollama_keeps_old_restriction():
    counter = [0]
    with conversation_runner.answer_llm(counter, True, "ollama"):
        assert orc.harness_order(orc.Context(), "chat") == ("ollama",)


def test_parsers_accept_harness():
    assert conversation_runner.parser().parse_args(["--split", "dev", "--harness", "claude"]).harness == "claude"
    args = memory_runner.parser().parse_args(["--harness", "codex", "--with-memory-only"])
    assert args.harness == "codex" and args.with_memory_only
    assert runner.parser().parse_args(["--harness", "claude"]).harness == "claude"


def test_summarize_results_counts_kinds():
    rows = [{"kind": "long", "passed": True, "turns": [
                {"judged": True, "passed": True, "error": None, "first_text_ms": 900, "seconds": 2.0}]},
            {"kind": "note", "passed": False, "id": "note-01", "turns": [
                {"judged": True, "passed": False, "error": None, "first_text_ms": 1500, "seconds": 3.0,
                 "conversation": 2, "turn": 1, "response": "x", "harness": "claude", "checks": {"must_all": False}}]}]
    s = runner.summarize_results(rows)
    assert s["passed"] == 1 and s["by_kind"]["note"]["pass_rate"] == 0
    assert s["first_text_ms_median"] == 1200 and s["failures"][0]["failed_checks"] == ["must_all"]
