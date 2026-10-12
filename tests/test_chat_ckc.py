"""chat.py の CKC 連携（T-115）のテスト。run_task と LLM はモック。"""

import importlib
import io
import json
import sys

import pytest

import llm
from ckc import store
from ckc.models import CandidateStatus, Origin


@pytest.fixture
def chat(monkeypatch):
    # chat.py は読み込み時に標準入出力を UTF-8 に設定するため、設定できるダミーに差し替えてから読み込む
    monkeypatch.setattr(sys, "stdin", io.TextIOWrapper(io.BytesIO()))
    monkeypatch.setattr(sys, "stdout", io.TextIOWrapper(io.BytesIO()))
    sys.modules.pop("chat", None)
    module = importlib.import_module("chat")
    monkeypatch.setattr(sys, "stdout", sys.__stdout__)
    return module


def test_trigger_detection(chat):
    assert chat.is_ckc_request("今の会話をナレッジ化して")
    assert chat.is_ckc_request(" /CKC ")
    assert not chat.is_ckc_request("ナレッジを検索して")


def test_chat_records_turns_and_runs_ckc(chat, db_path, ckc_data_dir, make_vault, monkeypatch):
    jk = make_vault("jarvis-knowledge")
    monkeypatch.setattr(chat, "run_task", lambda text: "了解しました。週次で確認します。")
    item = {"kind": "Decision", "title": "週次で確認する", "body": "レポートは週次で確認する", "projects": ["jarvis"],
            "category": None, "confidence": "confirmed",
            "evidence": [{"quote": "レポートは週次で確認することにする", "speaker": "owner"}]}
    monkeypatch.setattr(llm, "complete", lambda prompt, provider=None: json.dumps({"items": [item]}, ensure_ascii=False))
    answers = iter(["レポートは週次で確認することにする", "ナレッジ化して", "a", "y", "ナレッジ化して", "exit"])
    monkeypatch.setattr("builtins.input", lambda prompt="": next(answers))

    chat.main()

    [c] = store.list_candidates()
    assert c.status == CandidateStatus.PUBLISHED
    assert store.load_conversation(c.conversation_id).origin == Origin.JARVIS
    assert len(list((jk / "_jarvis" / "knowledge" / "Decision").glob("*.md"))) == 1
    assert len(store.list_candidates()) == 1  # 2回目の「ナレッジ化して」は対話がリセット済みで何もしない


def test_theme_request_prefix(chat):
    assert chat.theme_request("テーマ登録: タービンの冷却") == "タービンの冷却"
    assert chat.theme_request("/theme 家具の購入計画") == "家具の購入計画"
    assert chat.theme_request("テーマ登録:") is None
    assert chat.theme_request("大洗の進捗は") is None


def test_chat_theme_flow_declined_creates_nothing(chat, db_path, ckc_data_dir, monkeypatch, capsys):
    import architect
    proposal = architect.Proposal("arch_x", "t", "2026-10-13", [], False, None, [{"title": "n", "purpose": "p"}])
    monkeypatch.setattr(architect, "propose", lambda theme: proposal)
    applied = []
    monkeypatch.setattr(architect, "apply", lambda p: applied.append(p))
    answers = iter(["テーマ登録: 家具の購入計画", "n", "exit"])
    monkeypatch.setattr("builtins.input", lambda prompt="": next(answers))
    chat.main()
    assert applied == [] and "arch_x" in capsys.readouterr().out

