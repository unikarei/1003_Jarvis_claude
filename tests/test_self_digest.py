"""Jarvis の仕組みのダイジェストと前の会話の時刻（T-3400、ワークスペースの testing §35）。一時 DB・偽の Registry・偽の Git。"""
import subprocess

import pytest

import briefing
import config
import db
import registry
from memory import recall

REGISTRY = {"resources": {
    "job-news": {"resource_type": "scheduled_job", "schedule": "毎日 06:00", "purpose": "ニュースの取り込みと採点"},
    "svc-ir": {"resource_type": "service", "purpose": "1004 の管理 Web と内蔵 Scheduler（毎週金曜 18:00）"},
    "vault-x": {"resource_type": "vault", "purpose": "対象外"},
}}
MERGES = ("2026-11-21 09:36:00 +0900\x1fMerge pull request #105 from u/feat\x1ffeat: ニュースの一覧のページ（T-3301）\n\x1e\n"
          "2026-11-21 06:25:00 +0900\x1fMerge branch 'main'\x1f# Conflicts:\n\x1e\n"
          "2026-11-20 23:05:00 +0900\x1fMerge pull request #100 from u/fix\x1f\nCo-Authored-By: x\nfix: かみ合わなかった点\n\x1e")


@pytest.fixture
def setup(db_path, monkeypatch):
    monkeypatch.setattr(registry, "load", lambda: REGISTRY)
    monkeypatch.setattr(registry, "vault_paths", lambda: {})
    monkeypatch.setattr(briefing, "_git_merges", lambda days: MERGES)


def _session(sid, started, ended, turns=3):
    db._execute("INSERT INTO memory_sessions (session_id, channel, started_at, last_at, ended_at, owner_turns, summary, "
                "summary_status) VALUES (?, 'web', ?, ?, ?, ?, '話題: テスト', 'done')", (sid, started, ended, ended, turns))


def test_briefing_has_digest_jobs_and_changes(setup):
    text = briefing.build()
    assert "## Jarvis の仕組み" in text
    assert "管理画面（ACC）" in text and "毎週金曜 18:00" in text and "再読み込み" in text     # self-digest.md の中身
    assert "- 定期（毎日 06:00）: ニュースの取り込みと採点" in text
    assert "- 常駐（常時）: 1004 の管理 Web と内蔵 Scheduler（毎週金曜 18:00）" in text and "対象外" not in text
    assert "- 2026-11-21 09:36 feat: ニュースの一覧のページ（T-3301）" in text
    assert "- 2026-11-20 23:05 fix: かみ合わなかった点" in text
    assert "Conflicts" not in text and "Co-Authored-By" not in text                       # PR のマージだけ
    assert len(text) <= briefing.MAX_CHARS == 7000


def test_digest_file_is_written_for_the_llm():
    lines = briefing._self_digest()
    assert lines and not any(line.startswith("#") for line in lines)
    joined = "\n".join(lines)
    for word in ("管理画面", "ランチャー", "ニュース", "Insight Reporter", "会話の記憶", "メールで送る機能はない"):
        assert word in joined


def test_shrinks_changes_when_long(setup, monkeypatch):
    monkeypatch.setattr(briefing, "_self_digest", lambda: ["- " + "長い説明" * 1000])          # 4,000 字: 全部入る
    text = briefing.build()
    assert "## 最近入った変更" in text and len(text) <= briefing.MAX_CHARS
    monkeypatch.setattr(briefing, "_self_digest", lambda: ["- " + "長い説明" * 1400])          # 5,600 字: 変更を落とす
    text = briefing.build()
    assert "## 最近入った変更" not in text and "## 定期の仕事" in text and len(text) <= briefing.MAX_CHARS


def test_unknown_when_git_or_registry_fail(db_path, monkeypatch):
    monkeypatch.setattr(registry, "load", lambda: None)
    monkeypatch.setattr(registry, "vault_paths", lambda: {})

    def broken(days):
        raise subprocess.CalledProcessError(128, "git")
    monkeypatch.setattr(briefing, "_git_merges", broken)
    text = briefing.build()
    assert "- 最近の変更: 未確認" in text and "未確認（Registry を読めませんでした）" in text


def test_last_sessions_have_times_and_exclude_current(setup):
    _session("old", "2026-11-21T08:53:20.1", "2026-11-21T09:07:37.3", 26)
    _session("now", "2026-11-21T20:28:43.5", "2026-11-21T20:47:27.5", 39)
    text = briefing.build(session_id="now")
    assert "直前の会話の時刻（新しい順）: 2026-11-21 08:53〜09:07（ブラウザの会話、Owner の発言 26 回）" in text
    assert "20:28" not in text


def test_build_does_not_write(setup):
    before = config.DB_PATH.stat().st_mtime_ns
    briefing.build()
    assert config.DB_PATH.stat().st_mtime_ns == before


@pytest.mark.parametrize("started, ended, expected", [
    ("2026-11-21T08:53:20.1", "2026-11-21T09:07:37.3", "2026-11-21 08:53〜09:07"),
    ("2026-11-20T23:50:00", "2026-11-21T00:10:00", "2026-11-20 23:50〜2026-11-21 00:10"),
    (None, "2026-11-21T09:07:37", "2026-11-21 09:07〜09:07"),
    (None, None, ""),
])
def test_session_span(started, ended, expected):
    assert recall.session_span(started, ended) == expected


def test_memory_search_shows_times(db_path):
    _session("s1", "2026-11-21T08:53:20.1", "2026-11-21T09:07:37.3")
    db._execute("UPDATE memory_sessions SET summary='話題: 大洗の買い付け' WHERE session_id='s1'")
    hits = recall.search("大洗の買い付け", None)
    assert any("[会話の要約 2026-11-21 08:53〜09:07]" in h["text"] for h in hits)
