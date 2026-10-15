"""声での会話（FR-014、T-1213・T-1214）のテスト。マイク・音声認識・say はモック。"""

import json

import numpy as np
import pytest

import llm
import skills
from voice import record as rec
from voice.speak import Speaker, speakable, speech_summary, summary_line


def _blocks(levels, n=1600):
    return [np.full(n, level, dtype=np.int16) for level in levels]


def test_segment_cuts_at_silence():
    blocks = _blocks([50] * 3 + [2000] * 5 + [50] * 8 + [2000] * 5)
    audio = rec.segment(iter(blocks), threshold=300, silence_seconds=0.8)
    assert audio is not None and len(audio) == 1600 * (5 + 8)      # 話し始め〜0.8 秒の無音まで


def test_segment_without_speech_is_none_and_max_length():
    assert rec.segment(iter(_blocks([50] * 100)), threshold=300, start_timeout=1.0) is None
    audio = rec.segment(iter(_blocks([2000] * 500)), threshold=300, max_seconds=2.0)
    assert len(audio) == 1600 * 21


def test_threshold_from_noise():
    assert rec.threshold_from(_blocks([90, 80])) == rec.MIN_THRESHOLD
    assert rec.threshold_from(_blocks([400])) == 1200


def test_speakable_and_summary():
    assert speakable("結論：未定です [1][2]。詳しくは https://x.example/a を。**検討** | 案 |") == "結論：未定です。詳しくは を。検討 案"
    assert summary_line("# Guardian レポート\n\n- 検査日時") == "Guardian レポート"


class FakeProc:
    def __init__(self, cmd, said):
        said.append(cmd[-1]); self.cmd = cmd
    def wait(self): return 0
    def poll(self): return 0
    def terminate(self): pass


def _speaker(device=None):
    said = []
    return Speaker(device=device, runner=lambda cmd: FakeProc(cmd, said)), said


def test_speaker_reads_sentences_and_skips_code():
    sp, said = _speaker(device="USB PnP Sound Device")
    for chunk in ("一文目。二文", "目？\n```\nprint(1)\n```\n三文目"):
        sp.feed(chunk)
    sp.flush(); sp.wait(); sp.close()
    assert said == ["一文目。", "二文目？", "三文目"]
    assert sp._command("x")[:5] == ["say", "-v", "Kyoko", "-a", "USB PnP Sound Device"]


def test_main_without_microphone(monkeypatch, capsys):
    import voice_chat
    monkeypatch.setattr(rec, "has_input_device", lambda: False)
    assert voice_chat.main() == 1 and "マイクが見つかりません" in capsys.readouterr().out


def test_voice_conversation_streams_speech_and_records(db_path, monkeypatch, capsys):
    """声で質問 → 答えを表示しながら読み上げ、出典は読まない。記録とナレッジ化の材料は文字の対話と同じ。"""
    import db
    import voice_chat
    db.init_db()
    db.insert_node("Knowledge", category="minpaku", title="大洗の補助金", content="民泊の補助金の条件",
                   url="https://example.com/a")
    skills.reset_cache()

    def fake_stream(prompt, on_text, provider=None, timeout=None):
        for part in ("条件があります [1]。", "申請は来月です。"):
            on_text(part)
        return "条件があります [1]。申請は来月です。"
    monkeypatch.setattr(llm, "complete_stream", fake_stream)
    keys = iter(["", "", "q"])
    monkeypatch.setattr("builtins.input", lambda prompt="": next(keys))
    texts = iter(["民泊の補助金の条件は？", ""])
    sp, said = _speaker()
    with pytest.warns(UserWarning):
        assert voice_chat.main(listen=lambda: np.zeros(10, dtype=np.float32),
                               transcribe=lambda audio: next(texts), speaker=sp) == 0
    out = capsys.readouterr().out
    assert "あなた（声）> 民泊の補助金の条件は？" in out and "https://example.com/a" in out
    assert "条件があります。" in said and "申請は来月です。" in said
    assert not any("example.com" in s for s in said)                       # 出典は読まない
    assert "聞き取れませんでした" in out                                    # 2 回目は空
    tasks = db._fetchall("SELECT meta FROM nodes WHERE type = 'Task'")
    assert [json.loads(t["meta"])["skill"] for t in tasks] == ["knowledge-answer"]
    skills.reset_cache()


def test_table_separator_is_not_spoken():
    assert speakable("|---|---|") == "" and speakable("------") == ""


def test_speech_summaries():
    search = "1. 大洗_空調（oarai-minpaku、2026-10-07）\n   https://x\n2. 熊本県の補助（oarai-minpaku、2026-10-11）\n   https://y"
    assert speech_summary("knowledge-search", search) == "2 件見つかりました。1 件目は、大洗 空調。"
    trace = "知識: 大洗_空調（oarai-minpaku、2026-10-07）  https://x\n  ← 出典: 大洗_空調  https://x\n          vault"
    assert speech_summary("knowledge-trace", trace) == "出典は、大洗 空調です。"
    status = ("エラー 1 件・警告 2 件・情報 1 件（前回の照合結果、検査日時 2026-10-14 18:08）\nエラー:\n"
              "- `api-oarai-project-os`（unreachable）: HTTP 500: https://oarai.rebirth-lab.site/health")
    assert speech_summary("system-status", status) == "エラー 1 件、警告 2 件です。エラーは、api oarai project os、HTTP 500。"
    assert speech_summary("dev-board", "# 開発ボード\n\n- タスク") == "開発ボード"
