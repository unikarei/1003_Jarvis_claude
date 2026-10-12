"""取り込み（T-110）のテスト。"""

import pytest

from ckc import capture, store
from ckc.models import Origin, Speaker


@pytest.fixture
def env(db_path, ckc_data_dir):
    return ckc_data_dir


def test_parse_turns_with_speaker_labels():
    turns = capture.parse_turns("Owner: 大洗の件\n続きの行\nAI: 了解です\nあなた > 次へ\nJARVIS：はい")
    assert [(t.speaker, t.text) for t in turns] == [
        (Speaker.OWNER, "大洗の件\n続きの行"), (Speaker.AI, "了解です"), (Speaker.OWNER, "次へ"), (Speaker.AI, "はい")]


def test_parse_turns_without_labels_is_one_owner_turn():
    turns = capture.parse_turns("\nメモ: 来週調べる\n二行目\n")
    assert [(t.speaker, t.text) for t in turns] == [(Speaker.OWNER, "メモ: 来週調べる\n二行目")]


def test_capture_text_saves_raw(env):
    result = capture.capture_text("Owner: 決めた\nAI: 了解", title="t")
    assert result.is_new
    conv = store.load_conversation(result.conversation_id)
    assert conv.origin == Origin.PASTE and conv.title == "t"
    assert (env / "raw" / f"{result.conversation_id}.json").exists()


def test_capture_file(env, tmp_path):
    f = tmp_path / "会議メモ.md"
    f.write_text("User: 仕様を決めた\nAssistant: 承知しました\n", encoding="utf-8")
    result = capture.capture_file(f)
    conv = store.load_conversation(result.conversation_id)
    assert (conv.origin, conv.title, len(conv.turns)) == (Origin.FILE, "会議メモ", 2)
    with pytest.raises(capture.CaptureError, match="形式"):
        capture.capture_file(tmp_path / "x.pdf")


def test_capture_twice_returns_existing(env):
    first = capture.capture_text("Owner: 決めた\nAI: 了解")
    second = capture.capture_text("Owner:  決めた \r\nAI: 了解\n")
    assert (second.conversation_id, second.is_new) == (first.conversation_id, False)
    assert len(list((env / "raw").iterdir())) == 1


def test_capture_stops_on_secret(env):
    secret = "sk-ant-" + "x" * 40
    with pytest.raises(capture.CaptureError, match="秘密情報"):
        capture.capture_text(f"Owner: キーは {secret} です")
    assert not (env / "raw").exists()
    assert store.find_conversation_by_hash("x") is None


def test_empty_text_is_rejected(env):
    with pytest.raises(capture.CaptureError, match="空"):
        capture.capture_text("  \n ")
