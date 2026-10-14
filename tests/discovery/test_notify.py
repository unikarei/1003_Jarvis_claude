"""失敗の通知（T-203）のテスト。osascript はモック。"""

import pytest
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


def test_job_failure_is_notified(monkeypatch, tmp_path):
    """定期ジョブそのものの失敗（例外・0 以外の終了コード）を通知する（2026-10-14 の初回の定期実行で、失敗が通知されなかった）。"""
    import discover
    import improve
    from discovery import notify as nt
    sent = []
    monkeypatch.setattr(nt, "run", lambda cmd: sent.append(cmd[-1]))
    # Registry を読めない（終了コード 2）
    assert discover.main(["--registry-dir", str(tmp_path), "--notify"]) == 2
    assert len(sent) == 1 and "Discovery" in sent[0] and "終了コード 2" in sent[0]
    # --notify なしなら通知しない
    assert discover.main(["--registry-dir", str(tmp_path)]) == 2 and len(sent) == 1
    # 例外
    monkeypatch.setattr(discover, "_main", lambda argv=None: (_ for _ in ()).throw(RuntimeError("壊れた")))
    with pytest.raises(RuntimeError):
        discover.main(["--notify"])
    assert "RuntimeError" in sent[-1]
    # 週次の改善の監査
    monkeypatch.setattr(improve, "main", lambda argv=None: 2)
    assert improve.weekly() == 2 and "改善の監査" in sent[-1]
