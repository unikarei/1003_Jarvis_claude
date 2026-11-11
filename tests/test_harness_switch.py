"""会話の LLM（ハーネス）の切り替えと自動の切り替え（ADR-0035、../docs/spec.md §3.4.12、T-2002）。
本物の LLM は呼ばない（偽の complete_stream）。"""

import pytest

import config
import knowledge_engine as ke
import llm
import orchestrator as orc
from tests.test_route_chat import index  # noqa: F401  索引と DB（振り分けに LLM を呼ばない）

LIMIT = llm.LLMUnavailable("You've hit your usage limit.", "limit")


@pytest.fixture
def fake_llm(monkeypatch):
    """provider ごとに失敗を決められる偽の complete_stream。calls に呼んだ provider を残す。"""
    state = {"fail": {}, "calls": []}

    def fake(prompt, on_text, provider=None, timeout=None, cancel=None):
        state["calls"].append(provider)
        error = state["fail"].get(provider)
        if error is not None:
            raise error
        on_text(f"{provider}の答え")
        return f"{provider}の答え"
    monkeypatch.setattr(llm, "complete_stream", fake)
    monkeypatch.setattr(ke, "search", lambda *a, **k: [])
    return state


@pytest.fixture
def now(monkeypatch):
    t = {"v": 1000.0}
    monkeypatch.setattr(orc, "clock", lambda: t["v"])
    return t


def _say(text, ctx):
    return orc.handle(text, ctx, use_llm=False)


# ---------------------------------------------------------------- 発言の判定

@pytest.mark.parametrize("text, expected", [
    ("Codex に切り替えて", "codex"), ("コーデックスにして", "codex"), ("コレックスにして", "codex"),
    ("モデルを Codex にして", "codex"), ("Jarvis、Codex にして", "codex"),
    ("Claude にして", "claude"), ("クロードに切り替えてください", "claude"),
    ("Ollama にして", "ollama"), ("オーラマにして", "ollama"), ("ローカルにして", "ollama"), ("大山にして", "ollama"),
    ("おまかせにして", "auto"), ("自動に戻して", "auto"), ("元に戻して", "auto"), ("お任せで", "auto"),
    ("今どれ？", "status"), ("どのモデル？", "status"), ("ハーネスは？", "status"),
])
def test_harness_command_matches(text, expected):
    assert orc.harness_command(text) == expected


@pytest.mark.parametrize("text", [
    "Codex にして何ができる？", "クロードについて教えて", "自動にしてほしいことがある", "Ollama の使い方は？",
    "こんにちは", "", "大山の天気は？",
])
def test_harness_command_ignores_other_utterances(text):
    assert orc.harness_command(text) is None


# ---------------------------------------------------------------- 既定（おまかせ）

def test_auto_chat_uses_ollama_and_answer_uses_codex(index, fake_llm):  # noqa: F811
    ctx = orc.Context(stream=lambda t: None)
    assert ctx.harness == "auto"
    assert _say("こんにちは", ctx).text == "ollamaの答え"
    assert _say("大洗の補助金について教えて", ctx).skill == "knowledge-answer"
    assert fake_llm["calls"] == ["ollama", "codex"]


def test_initial_harness_from_setting(monkeypatch):
    monkeypatch.setattr(config, "HARNESS", "claude")
    assert orc.Context().harness == "claude"
    monkeypatch.setattr(config, "HARNESS", "nonsense")
    assert orc.Context().harness == "auto"


def test_heavy_harness_setting(monkeypatch):
    monkeypatch.setattr(config, "HEAVY_HARNESS", "claude")
    assert orc.answer_order() == ("claude", "codex", "ollama")
    monkeypatch.setattr(config, "HEAVY_HARNESS", "codex")
    assert orc.answer_order() == ("codex", "claude", "ollama")
    monkeypatch.setattr(config, "HEAVY_HARNESS", "ollama")       # A・B を逆にしない（不正な値は既定）
    assert orc.answer_order() == ("codex", "claude", "ollama")


# ---------------------------------------------------------------- 会話の途中の切り替え

def test_switch_without_llm_and_applies_to_both_routes(index, fake_llm):  # noqa: F811
    ctx = orc.Context(stream=lambda t: None)
    out = _say("Claude にして", ctx)
    assert out.ok and out.skill == orc.HARNESS_SKILL and out.text == "以後は Claude で答えます。"
    assert fake_llm["calls"] == [] and ctx.harness == "claude"     # LLM を呼ばない
    _say("こんにちは", ctx)
    _say("大洗の補助金について教えて", ctx)
    assert fake_llm["calls"] == ["claude_warm", "claude_warm"]


def test_switch_to_ollama_uses_ollama_for_knowledge(index, fake_llm):  # noqa: F811
    ctx = orc.Context(stream=lambda t: None)
    _say("Ollama にして", ctx)
    _say("大洗の補助金について教えて", ctx)
    assert fake_llm["calls"] == ["ollama"]


def test_switch_back_to_auto(index, fake_llm):  # noqa: F811
    ctx = orc.Context(stream=lambda t: None)
    _say("Codex にして", ctx)
    out = _say("おまかせにして", ctx)
    assert ctx.harness == "auto" and out.text.startswith("おまかせに戻します。")
    _say("こんにちは", ctx)
    assert fake_llm["calls"] == ["ollama"]


def test_chosen_harness_falls_back_in_chat_order(index, fake_llm):  # noqa: F811
    fake_llm["fail"] = {"claude_warm": LIMIT}
    ctx = orc.Context(stream=lambda t: None)
    _say("Claude にして", ctx)
    out = _say("こんにちは", ctx)
    assert fake_llm["calls"] == ["claude_warm", "ollama"]        # 上限なら claude_cli は試さない。次は A の順番
    assert out.text == "Claude の利用枠が上限のため、Ollama で答えます。\nollamaの答え"


def test_switch_is_not_taken_while_pending(index, fake_llm, monkeypatch):  # noqa: F811
    """聞き返しの答えを待っている間は、切り替えの言い方でもそのスキルに渡す（ノートに書く内容かもしれない）。"""
    seen = []
    monkeypatch.setattr(orc, "execute", lambda r, ctx, text, ask=None: seen.append(r.skill) or orc.Outcome(
        r.skill, r.method, {}, "続き", True, 0.0))
    ctx = orc.Context(stream=lambda t: None)
    ctx.pending = orc.Pending("obsidian-note", {}, orc.clock())
    _say("Codex にして", ctx)
    assert seen == ["obsidian-note"] and ctx.harness == "auto"


def test_switch_is_recorded_without_llm_info(index, fake_llm):  # noqa: F811
    from run_task import record
    import db
    ctx = orc.Context(stream=lambda t: None)
    out = _say("Codex にして", ctx)
    record("Codex にして", out)
    _say("こんにちは", ctx)
    out = _say("こんにちは", ctx)
    task_id = record("こんにちは", out)
    import json
    meta = json.loads(db.get_node(task_id)["meta"])
    assert meta["llm"] == {"harness": "codex", "provider": "codex", "skipped": []}


# ---------------------------------------------------------------- 自動の切り替えと休止

def test_limit_rests_harness_and_notice_once(index, fake_llm, now):  # noqa: F811
    fake_llm["fail"] = {"codex": LIMIT}
    ctx = orc.Context(stream=lambda t: None)
    first = _say("大洗の補助金について教えて", ctx)
    assert first.text.startswith("Codex の利用枠が上限のため、Claude で答えます。\nclaude_warmの答え")
    assert first.llm["harness"] == "claude" and first.llm["skipped"] == [{"harness": "codex", "reason": "limit"}]
    assert ctx.resting["codex"] == (now["v"] + 30 * 60, "limit")
    second = _say("大洗の補助金について教えて", ctx)
    assert second.text.startswith("claude_warmの答え")            # 休止中は飛ばし、知らせは繰り返さない
    assert fake_llm["calls"] == ["codex", "claude_warm", "claude_warm"]
    status = _say("今どれ？", ctx)
    assert status.text == ("今はおまかせ（簡単な会話は Ollama、ナレッジの質問は Codex から）です。"
                           "休止中: Codex（上限、あと 30 分）。")
    _say("Claude にして", ctx)
    assert _say("今どれ？", ctx).text.startswith("今は Claude（使えないときは自動で切り替えます）です。休止中: Codex")
    _say("おまかせにして", ctx)
    now["v"] += 31 * 60                                           # 休止の期限が過ぎたら、もう一度試す
    fake_llm["fail"] = {}
    third = _say("大洗の補助金について教えて", ctx)
    assert third.text.startswith("codexの答え") and "codex" not in ctx.resting


def test_cooldown_setting(index, fake_llm, now, monkeypatch):  # noqa: F811
    monkeypatch.setattr(config, "HARNESS_COOLDOWN_MINUTES", 5)
    fake_llm["fail"] = {"codex": LIMIT}
    ctx = orc.Context(stream=lambda t: None)
    _say("大洗の補助金について教えて", ctx)
    assert ctx.resting["codex"][0] == now["v"] + 5 * 60


def test_auth_and_missing_rest_but_transient_does_not(index, fake_llm, now):  # noqa: F811
    fake_llm["fail"] = {"codex": llm.LLMError("Error: Not logged in"),
                        "claude_warm": llm.LLMUnavailable("Claude Code CLI が見つかりません", "missing"),
                        "ollama": llm.LLMError("Ollama が 120 秒でタイムアウトしました")}
    ctx = orc.Context(stream=lambda t: None)
    out = _say("大洗の補助金について教えて", ctx)
    assert not out.ok and out.error == "no_llm"
    assert out.text == "今はどのモデルも使えません（Codex: ログインなし、Claude: 見つからない、Ollama: 応答なし）。"
    assert set(ctx.resting) == {"codex", "claude"}
    assert fake_llm["calls"] == ["codex", "claude_warm", "ollama"]


def test_all_resting_answers_list_and_continues(index, fake_llm, now):  # noqa: F811
    ctx = orc.Context(stream=lambda t: None)
    ctx.resting = {"codex": (now["v"] + 600, "limit"), "claude": (now["v"] + 600, "limit")}
    fake_llm["fail"] = {"ollama": llm.LLMError("Ollama に接続できません")}
    out = _say("大洗の補助金について教えて", ctx)
    assert not out.ok and fake_llm["calls"] == ["ollama"]
    assert out.text == "今はどのモデルも使えません（Codex: 上限、Claude: 上限、Ollama: 応答なし）。"
    fake_llm["fail"] = {}
    assert _say("こんにちは", ctx).ok                              # 会話は続く


def test_explicit_switch_clears_rest(index, fake_llm, now):  # noqa: F811
    ctx = orc.Context(stream=lambda t: None)
    ctx.resting = {"codex": (now["v"] + 600, "limit")}
    _say("Codex にして", ctx)
    assert "codex" not in ctx.resting
    _say("こんにちは", ctx)
    assert fake_llm["calls"] == ["codex"]


def test_partial_output_failure_does_not_switch(index, fake_llm, monkeypatch):  # noqa: F811
    calls = []

    def fake(prompt, on_text, provider=None, timeout=None, cancel=None):
        calls.append(provider)
        on_text("途中")
        raise LIMIT
    monkeypatch.setattr(llm, "complete_stream", fake)
    shown = []
    ctx = orc.Context(stream=shown.append)
    out = _say("大洗の補助金について教えて", ctx)
    assert not out.ok and calls == ["codex"] and shown == ["途中"]


def test_cancel_is_not_switched(index, fake_llm):  # noqa: F811
    fake_llm["fail"] = {"ollama": llm.LLMCancelled("取り消されました")}
    ctx = orc.Context(stream=lambda t: None, cancel=lambda: False)
    _say("こんにちは", ctx)
    assert fake_llm["calls"] == ["ollama"] and ctx.resting == {}


def test_notice_is_sent_only_with_answer(index, fake_llm):  # noqa: F811
    fake_llm["fail"] = {"codex": LIMIT}
    shown = []
    ctx = orc.Context(stream=shown.append)
    _say("大洗の補助金について教えて", ctx)
    assert shown == ["Codex の利用枠が上限のため、Claude で答えます。\n", "claude_warmの答え"]
