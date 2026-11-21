"""「管理ページ」と、物についての質問をスキルに回さない（T-3402、ワークスペースの testing §37）。"""
import json

import pytest

import orchestrator as orc
import skills

R1003 = "repo-1003-jarvis-claude"


def _s(desc, permission="read", phrases=(), inputs=None):
    return {"description": desc, "status": "active", "permission": permission,
            "entry": {"repo": R1003, "function": "x"}, "inputs": inputs or {}, "phrases": list(phrases)}


@pytest.fixture(autouse=True)
def index(tmp_path, monkeypatch):
    path = tmp_path / "skills.json"
    path.write_text(json.dumps({"skills": {
        "knowledge-answer": _s("答える", inputs={"question": {"type": "string", "required": True}}),
        "general-chat": _s("雑談", inputs={"question": {"type": "string", "required": True}}),
        "control-center": _s("管理画面", "local_write", ["管理画面", "かんりがめん", "管理ページ", "管理のページ",
                                                      "かんりページ", "ダッシュボード"]),
        "theme-register": _s("テーマ登録", "local_write", ["テーマ登録:"], {"theme": {"type": "string", "required": True}}),
    }}, ensure_ascii=False), encoding="utf-8")
    monkeypatch.setenv("JARVIS_SKILLS_JSON", str(path))
    skills.reset_cache()
    yield
    skills.reset_cache()


@pytest.mark.parametrize("text", ["管理ページ開いて", "管理画面を開いて", "管理画面", "管理画面見せて", "ダッシュボードを開いて"])
def test_opens_control_center(text):
    assert orc.route(text, orc.Context(), use_llm=False).skill == "control-center"


@pytest.mark.parametrize("text", [
    "管理画面って自動で更新されるの？", "管理ページってのは常に黙っててもどんどん更新されていくもんなの？",
    "ダッシュボードって何？", "管理画面とは", "管理画面は何？",
])
def test_question_about_the_thing_is_not_a_command(text):
    assert orc.route(text, orc.Context(), use_llm=False).skill in ("general-chat", "knowledge-answer")


def test_head_phrase_with_input_unchanged():
    r = orc.route("テーマ登録: 太陽光", orc.Context(), use_llm=False)
    assert r.skill == "theme-register" and r.inputs == {"theme": "太陽光"}
