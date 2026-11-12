"""発言の受け付けの改善（testing §18、T-2201・T-2202。2026-11-12 の実機の会話で見つかったもの）。"""

import json

import pytest

import obsidian_note as on
import orchestrator as orc
import knowledge_engine as ke
import skills
from tests.test_obsidian_note import INDEX, _s  # noqa: F401  同じ索引の形を使う

CAPTURE = {"knowledge-capture": _s("ナレッジ化", "local_write", ["ナレッジ化して"], confirm=True)}


@pytest.fixture
def index(tmp_path, monkeypatch):
    def use(extra):
        path = tmp_path / "skills.json"
        path.write_text(json.dumps({"skills": {**INDEX, **extra}}, ensure_ascii=False), encoding="utf-8")
        monkeypatch.setenv("JARVIS_SKILLS_JSON", str(path))
        skills.reset_cache()
    yield use
    skills.reset_cache()


# ---------------------------------------------------------------- T-2201 LLM の切り替えの言い方

@pytest.mark.parametrize("text, expected", [
    ("じゃあ、コデックスに切り替えて音声。", "codex"),     # 2026-11-12 20:29 の実際の発言（声の認識の結果）
    ("えっと Claude にして", "claude"),
    ("それじゃあオラマに切り替えて", "ollama"),
    ("ちょっと、おまかせにして", "auto"),
])
def test_switch_accepts_fillers_and_short_tail(text, expected):
    assert orc.harness_command(text) == expected


@pytest.mark.parametrize("text", ["Codex に切り替えたらどうなる？", "Codex にして何ができるの", "Claude に変えて欲しいか聞いて",
                                  "Codexにしてない", "今Codexにしてる?", "Codexにして大丈夫かな"])
def test_switch_still_ignores_questions_and_negatives(text):
    assert orc.harness_command(text) is None


def test_prompts_guide_how_to_switch():
    for prompt in [orc.CHAT_PROMPT, *ke.ANSWER_PROMPTS.values()]:
        assert "『Codex にして』" in prompt and "できないとは言わず" in prompt


# ---------------------------------------------------------------- T-2202 Obsidian にナレッジを入れて

@pytest.mark.parametrize("text", ["大洗のObsidianにナレッジを入れて", "Obsidian に知識として保存して", "オブシディアンにナレッジを追加して"])
def test_knowledge_request_goes_to_capture(index, text):
    index(CAPTURE)
    assert orc.route(text, orc.Context(), use_llm=False).skill == "knowledge-capture"


@pytest.mark.parametrize("text", ["Obsidian の大洗メモにナレッジを追加して", "Obsidian の「物件概要」に知識を追記して",
                                  "Obsidian のノートにナレッジを書いて", "Obsidian の大洗メモに駐車場ありを追加して"])
def test_note_requests_stay_with_obsidian_note(index, text):
    index(CAPTURE)
    assert orc.route(text, orc.Context(), use_llm=False).skill == "obsidian-note"


def test_without_capture_skill_falls_back_to_obsidian_note(index):
    index({})
    assert orc.route("大洗のObsidianにナレッジを入れて", orc.Context(), use_llm=False).skill == "obsidian-note"


def test_vault_word_knowledge_no_longer_means_generic_vault():
    assert on._vault_from_text("大洗のObsidianにナレッジを入れて") == "vault-oarai-obsidian"
    assert on._vault_from_text("ジャービスの Obsidian に追加") == "vault-jarvis-knowledge"
    assert on._vault_from_text("Obsidian にナレッジを入れて") is None
