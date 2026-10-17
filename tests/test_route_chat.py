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


def test_stats_rule_is_unchanged(index):
    r = orc.route("登録件数の多い順に3つ", orc.Context(), use_llm=False)
    assert (r.skill, r.method) == ("knowledge-stats", "rule")


def test_high_search_score_counts_as_knowledge_question(index, monkeypatch):
    """語に当たらなくても、検索の最上位の点数がしきい値以上ならナレッジの質問（c）。"""
    text = "新しいモデルの発表はどうだった？"
    assert not any(w in text for w in orc.KNOWLEDGE_WORDS + orc.PROJECT_WORDS)
    score = ke.search(text, limit=1)[0].score
    monkeypatch.setattr(config, "KNOWLEDGE_SCORE_THRESHOLD", score)
    assert orc.route(text, orc.Context(), use_llm=False).skill == "knowledge-answer"
    monkeypatch.setattr(config, "KNOWLEDGE_SCORE_THRESHOLD", score + 0.001)
    assert orc.route(text, orc.Context(), use_llm=False).skill == "general-chat"


def test_registry_project_id_counts_as_knowledge_word(index):
    assert orc.is_knowledge_question("ai-news はどうなってる？")
    assert not orc.is_knowledge_question("jarvis、おはよう")   # 呼びかけは通常の会話


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


def test_general_chat_falls_back_to_claude_warm(no_search, monkeypatch):
    calls = _fake_stream(monkeypatch, fail={"ollama": llm.LLMError("Ollama に接続できません")})
    ctx = orc.Context(stream=lambda t: None)
    text, _ = orc._chat({"question": "やあ"}, ctx, "やあ")
    assert text == "こんにちは、JARVIS です。"
    assert [c["provider"] for c in calls] == ["ollama", "claude_warm"]
    assert calls[0]["prompt"] == calls[1]["prompt"] and ctx.last_provider == "claude_warm"


def test_general_chat_does_not_fall_back_when_cancelled(no_search, monkeypatch):
    calls = _fake_stream(monkeypatch, fail={"ollama": llm.LLMCancelled("取り消されました")})
    with pytest.raises(llm.LLMCancelled):
        orc._chat({"question": "やあ"}, orc.Context(stream=lambda t: None), "やあ")
    assert [c["provider"] for c in calls] == ["ollama"]


def test_general_chat_never_uses_paid_api(no_search, monkeypatch):
    calls = _fake_stream(monkeypatch, fail={"ollama": llm.LLMError("x"), "claude_warm": llm.LLMError("y")})
    with pytest.raises(llm.LLMError):
        orc._chat({"question": "やあ"}, orc.Context(stream=lambda t: None), "やあ")
    assert [c["provider"] for c in calls] == ["ollama", "claude_warm"]


def test_handle_runs_general_chat_and_records_provider(index, monkeypatch):
    calls = _fake_stream(monkeypatch, fail={"ollama": llm.LLMError("x")})
    monkeypatch.setattr(ke, "search", lambda *a, **k: [])   # 振り分けの検索だけ（ハンドラーは検索しない）
    ctx = orc.Context(stream=lambda t: None)
    out = orc.handle("こんにちは", ctx, use_llm=False)
    assert out.ok and out.skill == "general-chat" and out.text == "こんにちは、JARVIS です。"
    assert ctx.last_provider == "claude_warm" and [c["provider"] for c in calls] == ["ollama", "claude_warm"]


# ---------------------------------------------------------------- knowledge-answer の LLM

def test_knowledge_answer_uses_claude_warm(db_path, monkeypatch):
    calls = _fake_stream(monkeypatch)
    ctx = orc.Context(stream=lambda t: None)
    orc._answer({"question": "補助金は？"}, ctx, "補助金は？")
    assert [c["provider"] for c in calls] == ["claude_warm"] and ctx.last_provider == "claude_warm"


def test_knowledge_answer_retries_once_with_claude_cli(db_path, monkeypatch):
    calls = _fake_stream(monkeypatch, fail={"claude_warm": llm.LLMError("待機中の claude が終了しました")})
    ctx = orc.Context(stream=lambda t: None)
    text, _ = orc._answer({"question": "補助金は？"}, ctx, "補助金は？")
    assert text == "こんにちは、JARVIS です。"
    assert [c["provider"] for c in calls] == ["claude_warm", "claude_cli"] and ctx.last_provider == "claude_cli"


def test_knowledge_answer_retries_only_once(db_path, monkeypatch):
    calls = _fake_stream(monkeypatch, fail={"claude_warm": llm.LLMError("x"), "claude_cli": llm.LLMError("y")})
    with pytest.raises(llm.LLMError):
        orc._answer({"question": "補助金は？"}, orc.Context(stream=lambda t: None), "補助金は？")
    assert [c["provider"] for c in calls] == ["claude_warm", "claude_cli"]


def test_knowledge_answer_does_not_retry_when_cancelled(db_path, monkeypatch):
    calls = _fake_stream(monkeypatch, fail={"claude_warm": llm.LLMCancelled("取り消されました")})
    with pytest.raises(llm.LLMCancelled):
        orc._answer({"question": "補助金は？"}, orc.Context(stream=lambda t: None, cancel=lambda: False), "補助金は？")
    assert [c["provider"] for c in calls] == ["claude_warm"]


def test_knowledge_answer_without_stream_uses_claude_warm(db_path, monkeypatch):
    seen = []
    monkeypatch.setattr(llm, "complete", lambda prompt, provider=None, timeout=None: seen.append(provider) or "答え")
    orc._answer({"question": "補助金は？"}, orc.Context(), "補助金は？")
    assert seen == ["claude_warm"]


def test_engine_answer_default_provider_is_unchanged(db_path, monkeypatch):
    seen = []
    monkeypatch.setattr(llm, "complete", lambda prompt, provider=None, timeout=None: seen.append(provider) or "答え")
    ke.answer("補助金は？")
    assert seen == [None]      # 省くと設定（JARVIS_LLM_PROVIDER）のまま
