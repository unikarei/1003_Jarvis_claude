"""失敗の通知（T-203）のテスト。osascript はモック。"""

import subprocess
from datetime import date

from discovery import notify
from discovery.guardian import Finding, Report


def _report(*findings):
    r = Report(checked_at="x")
    r.findings = list(findings)
    return r


def test_notifies_errors_once_per_day(tmp_path, monkeypatch):
    calls = []
    monkeypatch.setattr(notify, "run", lambda cmd: calls.append(cmd) or subprocess.CompletedProcess(cmd, 0))
    state = tmp_path / "last.json"
    report = _report(Finding("error", "stopped", "job-a", 'ジョブが "停止"'), Finding("error", "unreachable", "api-b", "500"),
                     Finding("warning", "backup", "data-c", "なし"))
    assert notify.notify_errors(report, date(2026, 10, 13), state) is True
    script = calls[0][2]
    assert calls[0][:2] == ["osascript", "-e"] and "エラー 2 件" in script and 'ジョブが \\"停止\\"' in script
    assert "ほか 1 件" in script
    assert notify.notify_errors(report, date(2026, 10, 13), state) is False      # 同じ日・同じ内容は1回だけ
    assert notify.notify_errors(report, date(2026, 10, 14), state) is True       # 翌日はもう一度
    assert len(calls) == 2


def test_no_errors_no_notification(tmp_path, monkeypatch):
    calls = []
    monkeypatch.setattr(notify, "run", lambda cmd: calls.append(cmd))
    assert notify.notify_errors(_report(Finding("warning", "backup", "d", "x")), state_file=tmp_path / "s") is False
    assert calls == []
