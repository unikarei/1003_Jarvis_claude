"""振り分け A/B（ADR-0027、T-1403）のテスト。通常の会話は general-chat（検索せずローカル LLM）、ナレッジの質問は
knowledge-answer（検索と claude_warm）、管理・集計はルールのまま。索引・DB は一時のもの、LLM は偽物。"""

import json

import pytest

import config
import knowledge_engine as ke
import llm
import orchestrator as orc
import skills

R1003 = "repo-1003-jarvis-claude"


def _s(desc, phrases=(), inputs=None):
    return {"description": desc, "status": "active", "permission": "read", "entry": {"repo": R1003, "function": "x"},
            "inputs": inputs or {}, "phrases": list(phrases)}


QUESTION = {"question": {"type": "string", "required": True}}
INDEX = {
    "general-chat": _s("通常の会話に答える（検索しない）", inputs=QUESTION),
    "knowledge-answer": _s("普通の質問に出典付きで答える", inputs=QUESTION),
    "knowledge-stats": _s("ナレッジの件数をデータソース別・分野別に集計する",
                          ["件数", "何件", "多い順", "データソース", "上位"],
                          {"limit": {"type": "integer"}, "by": {"type": "string"}}),
}


def _write_index(tmp_path, monkeypatch, data):
    path = tmp_path / "skills.json"
    path.write_text(json.dumps({"skills": data}, ensure_ascii=False), encoding="utf-8")
    monkeypatch.setenv("JARVIS_SKILLS_JSON", str(path))
    skills.reset_cache()


@pytest.fixture
def index(tmp_path, monkeypatch, db_path):
    import db
    db.insert_node("Knowledge", category="news", title="生成 AI の新しいモデル", content="AI の各社が新しいモデルを発表した",
                   url="https://example.com/ai")
    db.insert_node("Knowledge", category="minpaku", title="大洗の補助金", content="民泊の補助金の条件",
                   url="https://example.com/a")
    _write_index(tmp_path, monkeypatch, INDEX)

    def refuse(*a, **k):
        raise AssertionError("振り分けに LLM を呼ばない")
    monkeypatch.setattr(llm, "complete", refuse)
    monkeypatch.setattr(llm, "complete_stream", refuse)
    yield
    skills.reset_cache()


@pytest.mark.parametrize("text", ["こんにちは", "今日はいい天気だね", "量子コンピュータって何？"])
def test_small_talk_goes_to_general_chat(index, text):
    assert not ke.search(text, limit=1) or ke.search(text, limit=1)[0].score < config.KNOWLEDGE_SCORE_THRESHOLD
    r = orc.route(text, orc.Context(), use_llm=False)
    assert (r.skill, r.method, r.inputs) == ("general-chat", "default", {"question": text})


@pytest.mark.parametrize("text", ["大洗の物件の補助金は？", "ナレッジにある AI ニュースを教えて",
                                  "Obsidian のメモを探して、要点を教えて"])
def test_knowledge_questions_go_to_knowledge_answer(index, text):
    r = orc.route(text, orc.Context(), use_llm=False)
    assert (r.skill, r.method) == ("knowledge-answer", "default")


@pytest.mark.parametrize("text", ["Pythonのリスト内包表記って何？", "生成AIとは何ですか", "モデルの仕組みを説明して",
                                  "AI の使い方を教えて", "新幹線と飛行機の違い"])
def test_general_knowledge_question_goes_to_general_chat_even_with_high_score(index, monkeypatch, text):
    """一般知識の質問の形は、検索の点数が高くても通常の会話（2026-10-18 Owner 指示）。"""
    from types import SimpleNamespace
    monkeypatch.setattr(ke, "search", lambda *a, **k: [SimpleNamespace(score=100.0)])   # 点数の経路なら必ず B になる
    assert not orc.is_knowledge_question(text)
    assert orc.route(text, orc.Context(), use_llm=False).skill == "general-chat"


@pytest.mark.parametrize("text", ["ああ、なんか大分、保養みたいな感じやね。", "あなたの声は自然です。", "はい、久しぶり。",
                                  "ちょっと書き換えられるかどうか調べてみるんで。"])
def test_chat_without_question_form_goes_to_general_chat_even_with_high_score(index, monkeypatch, text):
    """ナレッジ・プロジェクトの語がなく、質問・依頼の形でない雑談は、検索の点数が高くても通常の会話（2026-11-04 Owner 指示）。"""
    from types import SimpleNamespace
    monkeypatch.setattr(ke, "search", lambda *a, **k: [SimpleNamespace(score=100.0)])
    assert not orc.is_knowledge_question(text)
    assert orc.route(text, orc.Context(), use_llm=False).skill == "general-chat"


@pytest.mark.parametrize("text", ["プールはどうなってる", "新しいAI関係のやつがあったら教えて", "あれ住所なんだっけ",
                                  "講演のことを聞きたい", "料金の一覧を見せて", "倒産件数を調べて"])
def test_question_or_request_form_is_left_to_the_score(index, monkeypatch, text):
    """質問・依頼の形なら、従来どおり検索の点数で決める。"""
    from types import SimpleNamespace
    monkeypatch.setattr(ke, "search", lambda *a, **k: [SimpleNamespace(score=100.0)])
    assert orc.is_knowledge_question(text)
    monkeypatch.setattr(ke, "search", lambda *a, **k: [])
    assert not orc.is_knowledge_question(text)


@pytest.mark.parametrize("text", ["新しい趣味を始めたい", "この文章をまとめて", "最近疲れてる"])
def test_broad_words_are_left_to_the_score(index, monkeypatch, text):
    """「新しい」「まとめて」「最近」だけではナレッジの質問にしない（点数が低ければ通常の会話）。"""
    monkeypatch.setattr(ke, "search", lambda *a, **k: [])
    assert orc.route(text, orc.Context(), use_llm=False).skill == "general-chat"


@pytest.mark.parametrize("text", ["各社のモデルについて分かったことは？", "あとはどうなった？"])
def test_koto_wa_is_not_a_general_question_form(index, monkeypatch, text):
    """「ことは」「あとは」は一般知識の質問の形にしない（点数の判定に進む）。"""
    from types import SimpleNamespace
    monkeypatch.setattr(ke, "search", lambda *a, **k: [SimpleNamespace(score=100.0)])
    assert orc.is_knowledge_question(text)


@pytest.mark.parametrize("text", ["ナレッジにある AI ニュースとは", "大洗の補助金とは", "民泊の仕組みを説明して"])
def test_knowledge_or_project_words_win_over_general_question_form(index, monkeypatch, text):
    """ナレッジ・プロジェクトの語があれば、質問の形にかかわらずナレッジの質問。"""
    monkeypatch.setattr(ke, "search", lambda *a, **k: [])   # 検索の点数では当てない
    assert orc.route(text, orc.Context(), use_llm=False).skill == "knowledge-answer"


@pytest.mark.parametrize("text", ["発表の仕方って何？", "昨日食べた料理の作り方って何？", "トレンドって何？", "最新のモデルって何？"])
def test_recent_words_do_not_override_general_question_form(index, monkeypatch, text):
    """最近の出来事を聞く語があっても、一般知識の質問の形なら通常の会話（語だけでは決めない）。"""
    from types import SimpleNamespace
    monkeypatch.setattr(ke, "search", lambda *a, **k: [SimpleNamespace(score=100.0)])
    assert orc.route(text, orc.Context(), use_llm=False).skill == "general-chat"


@pytest.mark.parametrize("text,score,expected", [
    ("OpenAI の最近の動向は？", 2.0, "knowledge-answer"),     # 最近の語: しきい値 8.0 × 0.25
    ("OpenAI の最近の動向は？", 1.9, "general-chat"),
    ("最近疲れてるんだよね", 1.0, "general-chat"),               # 語だけでは決めない
    ("OpenAI の様子は？", 7.9, "general-chat"),                 # 最近の語がなければ 8.0
])
def test_recent_words_lower_the_score_threshold(index, monkeypatch, text, score, expected):
    from types import SimpleNamespace
    monkeypatch.setattr(config, "KNOWLEDGE_SCORE_THRESHOLD", 8.0)
    monkeypatch.setattr(ke, "search", lambda *a, **k: [SimpleNamespace(score=score)])
    assert orc.route(text, orc.Context(), use_llm=False).skill == expected


def test_recent_words_are_removed_from_the_search_query(index, monkeypatch):
    """「最近」自体の点数で当てないよう、最近の出来事を聞く語を除いた話題で検索する。話題がなければ検索しない。"""
    queries = []
    monkeypatch.setattr(ke, "search", lambda q, **k: queries.append(q) or [])
    assert not orc.is_knowledge_question("OpenAI の最近の動向は？")
    assert "最近" not in queries[0] and "動向" not in queries[0] and "OpenAI" in queries[0]
    queries.clear()
    assert not orc.is_knowledge_question("最近")
    assert queries == []


def test_recent_small_talk_stays_general_with_real_search(index):
    """実際の検索で、「最近」だけの雑談は通常の会話（索引に「最近」を含む知識があっても）。"""
    import db
    db.insert_node("Knowledge", category="news", title="最近の話題", content="最近 最近 最近", url="https://example.com/r")
    assert orc.route("最近疲れてるんだよね", orc.Context(), use_llm=False).skill == "general-chat"


def test_stats_rule_is_unchanged(index):
    r = orc.route("登録件数の多い順に3つ", orc.Context(), use_llm=False)
    assert (r.skill, r.method) == ("knowledge-stats", "rule")


def test_high_search_score_counts_as_knowledge_question(index, monkeypatch):
    """語に当たらなくても、検索の最上位の点数がしきい値以上ならナレッジの質問（c）。"""
    text = "各社のモデルはどうだった？"
    assert not any(w in text for w in orc.KNOWLEDGE_WORDS + orc.PROJECT_WORDS + orc.RECENT_WORDS)
    score = ke.search(text, limit=1)[0].score
    monkeypatch.setattr(config, "KNOWLEDGE_SCORE_THRESHOLD", score)
    assert orc.route(text, orc.Context(), use_llm=False).skill == "knowledge-answer"
    monkeypatch.setattr(config, "KNOWLEDGE_SCORE_THRESHOLD", score + 0.001)
    assert orc.route(text, orc.Context(), use_llm=False).skill == "general-chat"


@pytest.mark.parametrize("text", ["ai-news はどうなってる？", "trading の状況は？", "Realestate について",
                                  "personal のまとめ", "physical-ai は？"])
def test_registry_project_id_counts_as_knowledge_word(index, monkeypatch, text):
    """ハイフンのない ID（trading・realestate・personal）も含め、Registry の ID はすべて判定に使う。"""
    import registry
    monkeypatch.setattr(registry, "projects", lambda: dict(config.FALLBACK_PROJECTS))
    monkeypatch.setattr(ke, "search", lambda *a, **k: [])   # 検索の点数では当てない
    assert orc.is_knowledge_question(text)
    assert orc.route(text, orc.Context(), use_llm=False).skill == "knowledge-answer"


def test_project_id_is_matched_as_a_word(index, monkeypatch):
    import registry
    monkeypatch.setattr(registry, "projects", lambda: dict(config.FALLBACK_PROJECTS))
    monkeypatch.setattr(ke, "search", lambda *a, **k: [])
    assert not orc.is_knowledge_question("jarvis、おはよう")      # 呼びかけは通常の会話
    assert not orc.is_knowledge_question("JARVIS、元気？")
    assert not orc.is_knowledge_question("personality って何？")  # 別の語の一部には当てない


def test_without_general_chat_in_index_falls_back_to_knowledge_answer(index, tmp_path, monkeypatch):
    _write_index(tmp_path, monkeypatch, {k: v for k, v in INDEX.items() if k != "general-chat"})
    r = orc.route("こんにちは", orc.Context(), use_llm=False)
    assert (r.skill, r.method) == ("knowledge-answer", "default")


def test_threshold_default_and_env(monkeypatch):
    import importlib
    assert config.KNOWLEDGE_SCORE_THRESHOLD == 8.0
    monkeypatch.setenv("JARVIS_KNOWLEDGE_SCORE_THRESHOLD", "3.5")
    try:
        assert importlib.reload(config).KNOWLEDGE_SCORE_THRESHOLD == 3.5
    finally:
        monkeypatch.delenv("JARVIS_KNOWLEDGE_SCORE_THRESHOLD")
        importlib.reload(config)


# ---------------------------------------------------------------- general-chat のハンドラー

class _Turn:
    def __init__(self, speaker, text):
        self.speaker, self.text = speaker, text


@pytest.fixture
def no_search(monkeypatch):
    def fail(*a, **k):
        raise AssertionError("general-chat は検索しない")
    monkeypatch.setattr(ke, "search", fail)
    monkeypatch.setattr(ke, "answer", fail)


def _fake_stream(monkeypatch, fail=None):
    """provider ごとの偽の complete_stream。fail に provider 名 → 例外を渡すと、その provider で失敗する。"""
    calls = []

    def fake(prompt, on_text, provider=None, timeout=None, cancel=None):
        calls.append({"prompt": prompt, "provider": provider, "cancel": cancel})
        if fail and provider in fail:
            raise fail[provider]
        on_text("こんにちは、")
        on_text("JARVIS です。")
        return "こんにちは、JARVIS です。"
    monkeypatch.setattr(llm, "complete_stream", fake)
    return calls


def test_general_chat_uses_ollama_without_search(no_search, monkeypatch):
    calls = _fake_stream(monkeypatch)
    shown = []
    cancel = lambda: False   # noqa: E731
    ctx = orc.Context(stream=shown.append, voice=True, cancel=cancel,
                      history=[_Turn("owner", "前の質問です"), _Turn("assistant", "前の答えです")])
    text, items = orc._chat({"question": "あなたの名前は？"}, ctx, "あなたの名前は？")
    assert (text, items) == ("こんにちは、JARVIS です。", [])
    assert ctx.shown == "こんにちは、JARVIS です。" and shown == ["こんにちは、", "JARVIS です。"]
    assert [c["provider"] for c in calls] == ["ollama"] and calls[0]["cancel"] is cancel
    assert ctx.last_provider == "ollama"
    prompt = calls[0]["prompt"]
    assert "JARVIS" in prompt and "モデルの名前" in prompt and "確かでないこと" in prompt
    assert "Owner: 前の質問です" in prompt and "JARVIS: 前の答えです" in prompt
    assert ke.VOICE_STYLE.strip() in prompt and "あなたの名前は？" in prompt
    assert "出典" not in text


def test_general_chat_text_has_no_voice_style(no_search, monkeypatch):
    calls = _fake_stream(monkeypatch)
    orc._chat({"question": "やあ"}, orc.Context(stream=lambda t: None), "やあ")
    assert ke.VOICE_STYLE.strip() not in calls[0]["prompt"] and "（なし）" in calls[0]["prompt"]


def test_general_chat_without_stream_uses_complete(no_search, monkeypatch):
    seen = []
    monkeypatch.setattr(llm, "complete", lambda prompt, provider=None, timeout=None: seen.append(provider) or " 答え ")
    ctx = orc.Context()
    assert orc._chat({"question": "やあ"}, ctx, "やあ") == ("答え", []) and seen == ["ollama"] and ctx.shown == ""


def test_general_chat_falls_back_to_codex_with_notice(no_search, monkeypatch):
    """おまかせの経路 A は Ollama → Codex → Claude。切り替えたら答えの前に一言添える（ADR-0035）。"""
    calls = _fake_stream(monkeypatch, fail={"ollama": llm.LLMError("Ollama に接続できません")})
    shown = []
    ctx = orc.Context(stream=shown.append)
    text, _ = orc._chat({"question": "やあ"}, ctx, "やあ")
    assert text == "Ollama が応答しなかったため、Codex で答えます。\nこんにちは、JARVIS です。"
    assert shown[0] == "Ollama が応答しなかったため、Codex で答えます。\n" and ctx.shown == "".join(shown)
    assert [c["provider"] for c in calls] == ["ollama", "codex"]
    assert calls[0]["prompt"] == calls[1]["prompt"] and ctx.last_provider == "codex"
    assert ctx.resting == {}                     # 一時的な失敗は休止にしない


def test_general_chat_does_not_fall_back_when_cancelled(no_search, monkeypatch):
    calls = _fake_stream(monkeypatch, fail={"ollama": llm.LLMCancelled("取り消されました")})
    with pytest.raises(llm.LLMCancelled):
        orc._chat({"question": "やあ"}, orc.Context(stream=lambda t: None), "やあ")
    assert [c["provider"] for c in calls] == ["ollama"]


def _fake_stream_partial(monkeypatch, failing):
    """failing の provider は一部を送ってから LLMError を出す偽の complete_stream。"""
    calls = []

    def fake(prompt, on_text, provider=None, timeout=None, cancel=None):
        calls.append(provider)
        if provider == failing:
            on_text("途中まで")
            raise llm.LLMError("応答が途中で切れました")
        on_text("別の答え")
        return "別の答え"
    monkeypatch.setattr(llm, "complete_stream", fake)
    return calls


def test_general_chat_does_not_switch_after_partial_output(no_search, monkeypatch):
    """Ollama が一部を表示した後に失敗したら claude_warm に切り替えない（表示と最後の答えが混ざらない）。"""
    calls = _fake_stream_partial(monkeypatch, "ollama")
    shown = []
    ctx = orc.Context(stream=shown.append)
    with pytest.raises(llm.LLMError):
        orc._chat({"question": "やあ"}, ctx, "やあ")
    assert calls == ["ollama"] and shown == ["途中まで"] and ctx.shown == ""


def test_handle_reports_error_after_partial_output(index, monkeypatch):
    calls = _fake_stream_partial(monkeypatch, "ollama")
    monkeypatch.setattr(ke, "search", lambda *a, **k: [])
    shown = []
    ctx = orc.Context(stream=shown.append)
    out = orc.handle("こんにちは", ctx, use_llm=False)
    assert not out.ok and "別の答え" not in out.text and "別の答え" not in shown
    assert calls == ["ollama"] and ctx.shown == ""


def test_knowledge_answer_does_not_retry_after_partial_output(db_path, monkeypatch):
    calls = _fake_stream_partial(monkeypatch, "codex")
    shown = []
    with pytest.raises(llm.LLMError):
        orc._answer({"question": "補助金は？"}, orc.Context(stream=shown.append), "補助金は？")
    assert calls == ["codex"] and shown == ["途中まで"]


def test_general_chat_never_uses_paid_api(no_search, monkeypatch):
    calls = _fake_stream(monkeypatch, fail={p: llm.LLMError("x") for p in ("ollama", "codex", "claude_warm", "claude_cli")})
    with pytest.raises(orc.AllHarnessesFailed) as e:
        orc._chat({"question": "やあ"}, orc.Context(stream=lambda t: None), "やあ")
    assert [c["provider"] for c in calls] == ["ollama", "codex", "claude_warm", "claude_cli"]   # anthropic_api はない
    assert e.value.text == "今はどのモデルも使えません（Ollama: 応答なし、Codex: 応答なし、Claude: 応答なし）。"


def test_handle_runs_general_chat_and_records_provider(index, monkeypatch):
    calls = _fake_stream(monkeypatch, fail={"ollama": llm.LLMError("x")})
    monkeypatch.setattr(ke, "search", lambda *a, **k: [])   # 振り分けの検索だけ（ハンドラーは検索しない）
    ctx = orc.Context(stream=lambda t: None)
    out = orc.handle("こんにちは", ctx, use_llm=False)
    assert out.ok and out.skill == "general-chat" and out.text.endswith("こんにちは、JARVIS です。")
    assert ctx.last_provider == "codex" and [c["provider"] for c in calls] == ["ollama", "codex"]
    assert out.llm == {"harness": "codex", "provider": "codex", "skipped": [{"harness": "ollama", "reason": "error"}]}


# ---------------------------------------------------------------- knowledge-answer の LLM

def test_knowledge_answer_uses_codex_by_default(db_path, monkeypatch):
    calls = _fake_stream(monkeypatch)
    ctx = orc.Context(stream=lambda t: None)
    orc._answer({"question": "補助金は？"}, ctx, "補助金は？")
    assert [c["provider"] for c in calls] == ["codex"] and ctx.last_provider == "codex"


def test_knowledge_answer_uses_claude_warm_when_heavy_is_claude(db_path, monkeypatch):
    monkeypatch.setattr(config, "HEAVY_HARNESS", "claude")
    calls = _fake_stream(monkeypatch)
    ctx = orc.Context(stream=lambda t: None)
    orc._answer({"question": "補助金は？"}, ctx, "補助金は？")
    assert [c["provider"] for c in calls] == ["claude_warm"] and ctx.last_provider == "claude_warm"


def test_knowledge_answer_retries_once_with_claude_cli(db_path, monkeypatch):
    monkeypatch.setattr(config, "HEAVY_HARNESS", "claude")
    calls = _fake_stream(monkeypatch, fail={"claude_warm": llm.LLMError("待機中の claude が終了しました")})
    ctx = orc.Context(stream=lambda t: None)
    text, _ = orc._answer({"question": "補助金は？"}, ctx, "補助金は？")
    assert text == "こんにちは、JARVIS です。"
    assert [c["provider"] for c in calls] == ["claude_warm", "claude_cli"] and ctx.last_provider == "claude_cli"


def test_knowledge_answer_claude_retries_once_then_next_harness(db_path, monkeypatch):
    """Claude の中では claude_warm → claude_cli を一度だけ。だめなら次のハーネス（Codex → Ollama）。"""
    monkeypatch.setattr(config, "HEAVY_HARNESS", "claude")
    calls = _fake_stream(monkeypatch, fail={"claude_warm": llm.LLMError("x"), "claude_cli": llm.LLMError("y")})
    ctx = orc.Context(stream=lambda t: None)
    text, _ = orc._answer({"question": "補助金は？"}, ctx, "補助金は？")
    assert [c["provider"] for c in calls] == ["claude_warm", "claude_cli", "codex"]
    assert text.startswith("Claude が応答しなかったため、Codex で答えます。")


def test_knowledge_answer_does_not_retry_when_cancelled(db_path, monkeypatch):
    calls = _fake_stream(monkeypatch, fail={"codex": llm.LLMCancelled("取り消されました")})
    with pytest.raises(llm.LLMCancelled):
        orc._answer({"question": "補助金は？"}, orc.Context(stream=lambda t: None, cancel=lambda: False), "補助金は？")
    assert [c["provider"] for c in calls] == ["codex"]


def test_knowledge_answer_without_stream_uses_codex(db_path, monkeypatch):
    seen = []
    monkeypatch.setattr(llm, "complete", lambda prompt, provider=None, timeout=None: seen.append(provider) or "答え")
    orc._answer({"question": "補助金は？"}, orc.Context(), "補助金は？")
    assert seen == ["codex"]


def test_engine_answer_default_provider_is_unchanged(db_path, monkeypatch):
    seen = []
    monkeypatch.setattr(llm, "complete", lambda prompt, provider=None, timeout=None: seen.append(provider) or "答え")
    ke.answer("補助金は？")
    assert seen == [None]      # 省くと設定（JARVIS_LLM_PROVIDER）のまま


def test_chat_prompt_forbids_claiming_actions():
    """雑談は何も操作しないので、操作を引き受けたように答えないよう指示する（2026-11-05 Owner の実機）。"""
    import orchestrator
    assert "操作を引き受けたように言わない" in orchestrator.CHAT_PROMPT
    assert "この会話では" in orchestrator.CHAT_PROMPT
