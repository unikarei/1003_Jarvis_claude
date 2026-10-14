"""応答の速さ（FR-014、T-1212）のテスト。claude -p は偽のスクリプト、LLM・Discovery はモック。"""

import json
import stat

import pytest

import llm
import orchestrator as orc
import skills

ORIGINAL_STREAM = llm._stream_claude_cli   # conftest が差し替える前の本物


def _fake_claude(tmp_path, lines, sleep=0):
    script = tmp_path / "claude"
    body = "\n".join(f"echo {json.dumps(json.dumps(line, ensure_ascii=False))}" for line in lines)
    script.write_text(f"#!/bin/bash\ncat > /dev/null\n{'sleep ' + str(sleep) if sleep else ''}\n{body}\n", encoding="utf-8")
    script.chmod(script.stat().st_mode | stat.S_IEXEC)
    return script


def _delta(text):
    return {"type": "stream_event", "event": {"type": "content_block_delta", "delta": {"type": "text_delta", "text": text}}}


def test_stream_passes_deltas_and_returns_result(tmp_path, monkeypatch):
    monkeypatch.setattr(llm, "CLAUDE_BIN", str(_fake_claude(tmp_path, [
        {"type": "system", "subtype": "init"}, _delta("大洗は"), _delta("港町です"),
        {"type": "result", "subtype": "success", "is_error": False, "result": "大洗は港町です"}])))
    got = []
    assert ORIGINAL_STREAM("p", got.append, timeout=10) == "大洗は港町です"
    assert got == ["大洗は", "港町です"]


@pytest.mark.parametrize("lines, message", [
    ([{"type": "result", "subtype": "error_max_turns", "is_error": True, "result": "x"}], "エラーを返しました"),
    ([_delta("途中")], "終了コード"),
])
def test_stream_errors_are_llmerror(tmp_path, monkeypatch, lines, message):
    monkeypatch.setattr(llm, "CLAUDE_BIN", str(_fake_claude(tmp_path, lines)))
    with pytest.raises(llm.LLMError, match=message):
        ORIGINAL_STREAM("p", lambda t: None, timeout=10)


def test_stream_timeout(tmp_path, monkeypatch):
    monkeypatch.setattr(llm, "CLAUDE_BIN", str(_fake_claude(tmp_path, [_delta("x")], sleep=5)))
    with pytest.raises(llm.LLMError, match="タイムアウト"):
        ORIGINAL_STREAM("p", lambda t: None, timeout=1)


def test_non_streaming_provider_gives_whole_text_once(monkeypatch):
    monkeypatch.setitem(llm.PROVIDERS, "fake", lambda prompt, timeout=None: "全文")
    got = []
    assert llm.complete_stream("p", got.append, provider="fake") == "全文" and got == ["全文"]


@pytest.fixture
def phase1_index(tmp_path, monkeypatch):
    index = {n: {"description": s.description, "status": s.status, "permission": s.permission, "entry": s.entry,
                 "inputs": s.inputs, "phrases": list(s.phrases)} for n, s in skills.FALLBACK.items()}
    index["system-status"] = {"description": "照合", "status": "active", "permission": "read",
                              "entry": {"repo": "repo-1003-jarvis-claude", "function": "x"},
                              "inputs": {}, "phrases": ["システムの状態"]}
    (tmp_path / "skills.json").write_text(json.dumps({"skills": index}, ensure_ascii=False), encoding="utf-8")
    monkeypatch.setenv("JARVIS_SKILLS_JSON", str(tmp_path / "skills.json"))
    skills.reset_cache()
    yield
    skills.reset_cache()


def test_llm_routing_is_off_by_default(phase1_index, monkeypatch):
    monkeypatch.setattr(llm, "complete", lambda *a, **k: pytest.fail("振り分けに LLM を呼ばない"))
    r = orc.route("大洗の物件の空調はどうする予定？", orc.Context())
    assert (r.skill, r.method) == ("knowledge-answer", "default")


REPORT = """# Guardian レポート

- 検査日時: 2026-10-15T07:00:12+09:00
- エラー 1 件・警告 1 件・情報 2 件

## エラー

- `api-x`（unreachable）: HTTP 500

## 警告

- `web-x`（dependency）: 依存先 api-x が Failed

## 情報

- `data-ckc`（missing）: 存在しない
"""


def test_system_status_uses_last_report_unless_latest(phase1_index, tmp_path, monkeypatch):
    reg = tmp_path / "registry"
    (reg / "generated").mkdir(parents=True)
    (reg / "generated" / "guardian-report.md").write_text(REPORT, encoding="utf-8")
    (reg / "generated" / "registry.json").write_text(json.dumps({"projects": {}, "resources": {}}), encoding="utf-8")
    monkeypatch.setattr(orc, "_registry_dir", lambda: reg)
    import discovery.collect as dc
    monkeypatch.setattr(dc, "collect", lambda registry: pytest.fail("照合しない"))
    out = orc.handle("システムの状態は？", orc.Context())
    assert out.ok and out.text.startswith("エラー 1 件・警告 1 件・情報 2 件（前回の照合結果（毎朝 07:00）")
    assert "2026-10-15 07:00" in out.text and "HTTP 500" in out.text and "data-ckc" not in out.text
    # 「最新」のときだけ照合する
    called = []
    monkeypatch.setattr(dc, "collect", lambda registry: called.append(1) or {})
    import discovery.guardian as dg
    monkeypatch.setattr(dg, "evaluate", lambda registry, snap: dg.Report(checked_at="2026-10-15T09:00:00+09:00"))
    out = orc.handle("最新のシステムの状態は？", orc.Context())
    assert called and out.ok and "今の照合結果" in out.text and "エラー・警告はありません" in out.text
