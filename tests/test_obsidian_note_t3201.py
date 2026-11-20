"""Obsidian のノートの編集を会話から使う（T-3201、ワークスペースの testing §32）。一時 Vault・偽の Ollama で動かす。"""
import pytest

import obsidian_note as on
import orchestrator as orc
from ckc.models import Speaker, Turn
from tests.test_obsidian_note import Ask, env, put, read, say  # noqa: F401  env は fixture

TODO = "民泊@大洗/00_管理/大洗_最優先ToDo.md"


@pytest.fixture
def todo(env):
    put(env["oarai"], TODO, "# 大洗 最優先ToDo\n\n- 物件の価格交渉\n")
    put(env["oarai"], "民泊@大洗/04_設備/大洗_空調.md", "# 空調\n")
    return env


@pytest.mark.parametrize("name", ["大洗のToDoリスト", "トゥールリスト", "大洗のトゥードゥー", "ToDo", "最優先ToDo",
                                  "to do リスト"])
def test_todo_variants_resolve_to_the_todo_note(todo, name):
    assert on.find_notes(name) == [("vault-oarai-obsidian", TODO)]


def test_unrelated_name_is_not_a_candidate(todo):
    assert on.find_notes("議事録") == []


def test_similar_names_become_numbered_candidates(todo):
    put(todo["oarai"], "民泊@大洗/00_管理/大洗_ToDo_旧.md", "古い\n")
    found = on.find_notes("大洗のToDoリスト")
    assert len(found) == 2 and TODO in [rel for _, rel in found]


def test_not_found_question_lists_near_names(todo):
    todo["llm"].reply = {"note": "空調の点検表", "content": "点検"}
    out = say("Obsidian の空調の点検表に点検を追加して", orc.Context(), Ask())
    assert "見つかりません" in out.text and "近い名前" in out.text and "大洗_空調" in out.text


def test_close_name_resolves_to_the_single_note(todo):
    assert on.find_notes("大洗の空調メモ帳") == [("vault-oarai-obsidian", "民泊@大洗/04_設備/大洗_空調.md")]


@pytest.mark.parametrize("text", ["大洗のToDoに3社面談を追加して", "ToDoリストに入れておいて", "トゥールリストに3社面談を入れて",
                                  "それをToDoに残しておいて"])
def test_todo_requests_route_to_obsidian_note(todo, text):
    assert orc.route(text, orc.Context(), use_llm=False).skill == "obsidian-note"


@pytest.mark.parametrize("text", ["ToDoに入れられる？", "ToDo リストってこの会話から入れられないんだっけ",
                                  "ToDoって何？"])
def test_todo_questions_are_not_routed(todo, text):
    assert orc.route(text, orc.Context(), use_llm=False).skill != "obsidian-note"


def test_content_from_previous_conversation_is_written_after_confirmation(todo):
    ctx = orc.Context()
    ctx.history = [Turn(Speaker.OWNER, "近日中に買い付け側と売り側の不動産屋を含めた3社で面談したい"),
                   Turn(Speaker.AI, "覚えておきます。")]
    todo["llm"].reply = {"op": "append", "note": "大洗のToDo", "content": "3社（買い付け側・売り側の不動産屋）で近日中に面談する"}
    ask = Ask("y")
    out = say("さっきの3社面談の件、大洗のToDoに入れておいて", ctx, ask)
    prompt = todo["llm"].calls[0][1]
    assert todo["llm"].calls[0][0] == "ollama"                        # 本文は Ollama だけ
    assert "3社で面談したい" in prompt and "# 直前の会話" in prompt     # 直前の会話を渡した
    assert "3社（買い付け側" in ask.calls[0]["prompt"] and TODO in ask.calls[0]["prompt"]   # 書く前の確認
    assert "追記しました" in out.text
    assert "3社（買い付け側・売り側の不動産屋）で近日中に面談する" in read(todo["oarai"], TODO)


def test_declined_confirmation_writes_nothing(todo):
    todo["llm"].reply = {"op": "append", "note": "ToDo", "content": "3社面談"}
    before = read(todo["oarai"], TODO)
    say("ToDoに3社面談を追加して", orc.Context(), Ask("n"))
    assert read(todo["oarai"], TODO) == before
