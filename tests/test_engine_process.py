"""音声エンジンの起動と停止（voice/engine_process.py、2026-10-18 Owner 指示）のテスト。
本物の VOICEVOX の代わりに、/version に答える小さな HTTP サーバーのスクリプト（子のプロセスも作る）を使う。"""

import json
import os
import signal
import socket
import stat
import subprocess
import sys
import threading
import time

import numpy as np
import pytest

from voice import engine_process, tts

FAKE = r'''#!{python}
import argparse, http.server, os, subprocess, sys, time
p = argparse.ArgumentParser()
p.add_argument("--host"); p.add_argument("--port", type=int); p.add_argument("--disable_sentry", action="store_true")
a = p.parse_args()
mode = os.environ.get("FAKE_ENGINE_MODE", "ok")
with open(os.environ["FAKE_ENGINE_ARGS"], "w") as f:
    f.write(" ".join(sys.argv[1:]))
if mode == "fail":
    sys.exit(3)
if mode == "fail_after_child":
    orphan = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(600)"])
    with open(os.environ["FAKE_ENGINE_CHILD"], "w") as f:
        f.write(str(orphan.pid))
    sys.exit(4)
child_code = ("import signal, time; signal.signal(signal.SIGTERM, signal.SIG_IGN); time.sleep(600)"
              if mode == "stubborn" else "import time; time.sleep(600)")
child = subprocess.Popen([sys.executable, "-c", child_code, sys.argv[0]])   # エンジンが作る子（本物と同じく実行ファイルの名前を含む）
with open(os.environ["FAKE_ENGINE_CHILD"], "w") as f:
    f.write(str(child.pid))
if mode == "hang":
    time.sleep(600)
time.sleep(float(os.environ.get("FAKE_ENGINE_DELAY", "0")))
class H(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
        body = b'"0.25.2"' if self.path == "/version" else b"[]"
        self.send_response(200); self.send_header("Content-Type", "application/json"); self.end_headers()
        self.wfile.write(body)
    def do_POST(self):
        with open(os.environ["FAKE_ENGINE_POSTS"], "a") as f:
            f.write(self.path + "\n")
        self.send_response(204); self.end_headers()
    def log_message(self, *x): pass
http.server.HTTPServer((a.host, a.port), H).serve_forever()
'''


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _gone(pid: int, wait: float = 5.0) -> bool:
    deadline = time.monotonic() + wait
    while time.monotonic() < deadline:
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return True
        try:                                     # 終わったがまだ回収されていない（ゾンビ）も「止まった」
            if subprocess.run(["ps", "-p", str(pid), "-o", "stat="], capture_output=True, text=True).stdout.strip() \
                    .startswith("Z"):
                return True
        except OSError:
            pass
        time.sleep(0.05)
    return False


@pytest.fixture
def fake(tmp_path, monkeypatch):
    """偽のエンジンの実行ファイル・接続先・記録の場所。"""
    script = tmp_path / "bin" / "run"
    script.parent.mkdir()
    script.write_text(FAKE.replace("{python}", sys.executable), encoding="utf-8")
    script.chmod(script.stat().st_mode | stat.S_IEXEC)
    port = _free_port()
    engine = tts.VoicevoxEngine("voicevox", "VOICEVOX", f"http://127.0.0.1:{port}", "")
    monkeypatch.setenv("FAKE_ENGINE_ARGS", str(tmp_path / "args.txt"))
    monkeypatch.setenv("FAKE_ENGINE_CHILD", str(tmp_path / "child.pid"))
    monkeypatch.setenv("FAKE_ENGINE_POSTS", str(tmp_path / "posts.txt"))
    state = tmp_path / "state"
    started = []

    def make(**kw):
        lp = engine_process.EngineProcess(engine, str(script), kw.pop("extra", []), state_dir=state, **kw)
        started.append(lp)
        return lp

    def independent():
        """この会話と関係なく起動していたエンジン。"""
        proc = subprocess.Popen([str(script), "--host", "127.0.0.1", "--port", str(port)], start_new_session=True)
        started.append(proc)
        deadline = time.monotonic() + 10
        while not engine.available()[0]:
            assert time.monotonic() < deadline, "偽のエンジンが起動しない"
            time.sleep(0.05)
        return proc
    ns = type("Fake", (), {"script": script, "port": port, "engine": engine, "state": state, "make": staticmethod(make),
                           "independent": staticmethod(independent), "tmp": tmp_path})
    yield ns
    for item in started:                         # 後片付け（テストが失敗しても残さない）
        pid = item.pid if hasattr(item, "pid") and isinstance(item.pid, int) else None
        if pid:
            try:
                os.killpg(pid, signal.SIGKILL)
            except (ProcessLookupError, PermissionError):
                pass
    child = tmp_path / "child.pid"
    if child.exists():
        try:
            os.kill(int(child.read_text()), signal.SIGKILL)
        except (ProcessLookupError, ValueError):
            pass


def test_starts_when_not_running_and_stops_only_its_own_process_group(fake):
    lp = fake.make()
    lp.start()
    assert lp.owned and lp.pid
    assert lp.wait_ready(timeout=15) and fake.engine.available()[0]
    assert (fake.tmp / "args.txt").read_text() == f"--host 127.0.0.1 --port {fake.port}"   # Mac の中だけ
    state = json.loads(lp.state_path.read_text())
    assert state["engine_pid"] == lp.pid and [u["pid"] for u in state["users"]] == [os.getpid()] and state["started"]
    assert state["binary"] == str(fake.script) and state["port"] == fake.port
    pid, child = lp.pid, int((fake.tmp / "child.pid").read_text())
    lp.stop()
    assert _gone(pid) and _gone(child)                                 # エンジンの子のプロセスも止まる
    assert not fake.engine.available()[0] and not lp.state_path.exists()
    lp.stop()                                                          # 何度呼んでもよい


def test_reuses_running_instance_and_does_not_stop_it(fake):
    other = fake.independent()
    lp = fake.make()
    lp.start()
    assert not lp.owned and lp.proc is None and "起動済み" in lp.status
    assert lp.wait_ready(timeout=1)
    lp.stop()
    assert other.poll() is None and fake.engine.available()[0]        # 独立して動いていたものは止めない


def test_engine_that_exits_reports_failure_and_falls_back_to_kyoko(fake, monkeypatch):
    monkeypatch.setenv("FAKE_ENGINE_MODE", "fail")
    lp = fake.make()
    lp.start()
    assert not lp.wait_ready(timeout=10) and "失敗" in lp.status and not lp.owned
    notes = []
    monkeypatch.setitem(tts.ENGINES, "voicevox", fake.engine)
    monkeypatch.setattr(tts.ENGINES["say"], "synthesize", lambda text, s, cancel=None: np.ones(10, dtype=np.float32))
    synth = tts.make_synth(tts.VoiceSettings(engine="voicevox", speaker="30", rate=1.25, intonation=1.45, pitch=0.02),
                           notify=notes.append)
    assert synth("こんにちは。", threading.Event()) is not None and "Kyoko" in notes[0]


def test_engine_that_never_becomes_ready_is_stopped_after_timeout(fake, monkeypatch):
    monkeypatch.setenv("FAKE_ENGINE_MODE", "hang")
    lp = fake.make()
    lp.start()
    pid = lp.pid
    assert not lp.wait_ready(timeout=1.5) and "使えるようになりませんでした" in lp.status
    assert _gone(pid) and not lp.owned and not lp.state_path.exists()


def test_engine_killed_mid_conversation_falls_back_to_kyoko_per_sentence(fake, monkeypatch):
    lp = fake.make()
    lp.start()
    assert lp.wait_ready(timeout=15)
    monkeypatch.setitem(tts.ENGINES, "voicevox", fake.engine)
    said = []
    monkeypatch.setattr(fake.engine, "synthesize", lambda text, s, cancel=None: (_ for _ in ()).throw(
        tts.TTSError("接続できません")) if not fake.engine.available()[0] else np.zeros(5, dtype=np.float32))
    monkeypatch.setattr(tts.ENGINES["say"], "synthesize",
                        lambda text, s, cancel=None: said.append((text, s.speaker)) or np.ones(5, dtype=np.float32))
    notes = []
    synth = tts.make_synth(tts.VoiceSettings(engine="voicevox", speaker="30"), notify=notes.append)
    assert synth("一文目。", threading.Event()) is not None and said == []
    os.killpg(lp.pid, signal.SIGKILL)                                  # 予期しない終了
    assert _gone(lp.pid)
    assert synth("二文目。", threading.Event()) is not None and said == [("二文目。", "Kyoko")]
    lp.stop()                                                          # 終わったものを止めても例外にしない
    assert not lp.owned


def _user(pid, started=None) -> dict:
    return {"pid": pid, "started": started if started is not None else engine_process._ps(pid, "lstart")}


def _record(fake, pid, users, started=None, pgid=None):
    fake.state.mkdir(parents=True, exist_ok=True)
    (fake.state / "engine-voicevox.json").write_text(json.dumps(
        {"engine_pid": pid, "pgid": pgid if pgid is not None else pid,
         "started": started if started is not None else engine_process._ps(pid, "lstart"),
         "binary": str(fake.script), "port": fake.port,
         "users": [u if isinstance(u, dict) else _user(u, started="Mon Jan  1 00:00:00 2001") for u in users]}))


def _dead_pid() -> int:
    p = subprocess.Popen([sys.executable, "-c", "pass"])
    p.wait()
    return p.pid


def test_adopts_orphan_left_by_crashed_session_and_stops_it(fake):
    other = fake.independent()                                         # 前の会話が起動したエンジン
    _record(fake, other.pid, [_dead_pid()])                            # その会話は異常終了した
    lp = fake.make()
    lp.start()
    assert lp.owned and lp.pid == other.pid and "引き継いで" in lp.status
    assert [u["pid"] for u in json.loads(lp.state_path.read_text())["users"]] == [os.getpid()]
    lp.stop()
    assert other.wait(timeout=10) is not None and not fake.engine.available()[0]


def test_joins_engine_used_by_live_session_and_does_not_stop_it(fake):
    other = fake.independent()
    live = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"])
    try:
        _record(fake, other.pid, [_user(live.pid)])                    # ほかの会話が起動して使っている
        lp = fake.make()
        lp.start()
        assert lp.owned and "一緒に使います" in lp.status
        assert sorted(u["pid"] for u in json.loads(lp.state_path.read_text())["users"]) == sorted([live.pid, os.getpid()])
        lp.stop()
        assert other.poll() is None and fake.engine.available()[0]     # ほかの会話が使っている間は止めない
        assert [u["pid"] for u in json.loads(lp.state_path.read_text())["users"]] == [live.pid]
    finally:
        live.kill()


def test_reused_session_pid_is_not_counted_as_a_live_user(fake):
    """異常終了した会話の pid が別のプロセスに再利用されていても、使っている会話とみなさない（開始時刻で区別）。"""
    other = fake.independent()
    reused = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"])
    try:
        _record(fake, other.pid, [_user(reused.pid, started="Mon Jan  1 00:00:00 2001")])
        lp = fake.make()
        lp.start()
        assert lp.owned and "引き継いで" in lp.status                   # 生きている会話はいない
        lp.stop()
        assert other.wait(timeout=10) is not None                       # 最後の会話として止める
    finally:
        reused.kill()


def test_engine_with_other_port_or_group_is_not_ours(fake):
    """開始時刻が同じ秒でも、ポートやプロセスのグループが記録と違えば別のエンジンとして扱う。"""
    other = fake.independent()
    _record(fake, other.pid, [_dead_pid()], pgid=other.pid + 100000)   # グループが違う
    lp = fake.make()
    lp.start()
    assert not lp.owned
    lp.stop()
    assert other.poll() is None
    data = json.loads((fake.state / "engine-voicevox.json").read_text()) if (fake.state / "engine-voicevox.json").exists() \
        else None
    assert data is None                                                 # 合わない記録は消す
    _record(fake, other.pid, [_dead_pid()])
    rec = json.loads((fake.state / "engine-voicevox.json").read_text())
    rec["port"] = fake.port + 1                                         # ポートが違う
    (fake.state / "engine-voicevox.json").write_text(json.dumps(rec))
    lp2 = fake.make()
    lp2.start()
    assert not lp2.owned
    lp2.stop()
    assert other.poll() is None


def test_stop_without_readable_state_keeps_engine_and_allows_retry(fake, monkeypatch):
    """記録を読み書きできないとき: 使っている会話が分からないので止めずに残し、もう一度 stop() できる。"""
    lp = fake.make()
    lp.start()
    assert lp.wait_ready(timeout=15)
    pid = lp.pid
    original = engine_process.EngineProcess._locked

    def broken(self):
        raise OSError("記録の場所を読めません")
    monkeypatch.setattr(engine_process.EngineProcess, "_locked", broken)
    lp.stop()
    assert lp.owned and _alive_pid(pid) and "残しました" in lp.status
    monkeypatch.setattr(engine_process.EngineProcess, "_locked", original)
    lp.stop()                                                          # 回復したら止められる
    assert _gone(pid) and not lp.owned


def test_unreadable_state_is_not_treated_as_missing(fake, monkeypatch):
    other = fake.independent()
    live = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"])
    try:
        _record(fake, other.pid, [_user(live.pid)])
        lp = fake.make()
        lp.start()
        assert lp.owned
        real_read = engine_process.EngineProcess._read

        def unreadable(self):
            raise PermissionError("読めません")
        monkeypatch.setattr(engine_process.EngineProcess, "_read", unreadable)
        lp.stop()
        assert lp.owned and other.poll() is None                       # 止めず、所有も消さない
        monkeypatch.setattr(engine_process.EngineProcess, "_read", real_read)
        lp.stop()
        assert not lp.owned and other.poll() is None                   # ほかの会話が使っているので止めない
    finally:
        live.kill()


def test_signal_during_start_is_delivered_after_ownership_is_recorded(fake, monkeypatch):
    """起動の途中に届いた SIGTERM は、所有を記録した後に届く（finally の stop() でエンジンを止められる）。"""
    lp = fake.make()
    real = engine_process.EngineProcess._start_locked

    def start_then_signal(self):
        os.kill(os.getpid(), signal.SIGTERM)                           # 区間の途中で届く
        real(self)
        assert self.owned                                              # まだ SystemExit になっていない
    monkeypatch.setattr(engine_process.EngineProcess, "_start_locked", start_then_signal)
    restore = engine_process.stop_on_signals(lp)
    try:
        with pytest.raises(SystemExit):
            try:
                lp.start()
                time.sleep(1)
            finally:
                assert lp.owned
                lp.stop()
    finally:
        restore()
    assert not lp.owned and not lp.state_path.exists()


def _alive_pid(pid: int) -> bool:
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False


def test_does_not_adopt_reused_pid_or_restarted_engine(fake):
    """記録の pid が別のプロセス、または開始時刻が違う（同じ実行ファイルの別の起動）なら、引き継がず止めない。"""
    other = fake.independent()
    live = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"])
    try:
        _record(fake, live.pid, [_dead_pid()])                         # pid がエンジンでない
        lp = fake.make()
        lp.start()
        assert not lp.owned and "起動済み" in lp.status and not lp.state_path.exists()
        lp.stop()
        assert live.poll() is None and other.poll() is None
        _record(fake, other.pid, [_dead_pid()], started="Mon Jan  1 00:00:00 2001")   # 開始時刻が違う
        lp2 = fake.make()
        lp2.start()
        assert not lp2.owned
        lp2.stop()
        assert other.poll() is None
    finally:
        live.kill()


SESSION = r"""
import sys, time
sys.path.insert(0, {root!r})
from voice import engine_process, tts
engine = tts.VoicevoxEngine("voicevox", "VOICEVOX", "http://127.0.0.1:{port}", "")
lp = engine_process.EngineProcess(engine, {script!r}, state_dir={state!r})
lp.start()
print("ready" if lp.wait_ready(timeout=20) else "fail", lp.owned, lp.pid, flush=True)
sys.stdin.readline()
lp.stop()
print("stopped", flush=True)
"""


def _session(fake):
    code = SESSION.format(root=os.getcwd(), port=fake.port, script=str(fake.script), state=str(fake.state))
    return subprocess.Popen([sys.executable, "-c", code], stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True)


def _engines(fake) -> list[str]:
    out = subprocess.run(["pgrep", "-f", f"{fake.script} --host"], capture_output=True, text=True).stdout.split()
    return [p for p in out if p != str(os.getpid())]


def test_two_concurrent_sessions_share_one_engine_until_the_last_ends(fake):
    """同時に起動した 2 つの会話: エンジンは 1 つだけ起動し、先に終わった会話は止めず、最後の会話が止める。"""
    a, b = _session(fake), _session(fake)
    try:
        ra, rb = a.stdout.readline().split(), b.stdout.readline().split()
        assert ra[0] == rb[0] == "ready" and ra[2] == rb[2]           # 同じエンジン
        assert len(_engines(fake)) == 1
        a.stdin.write("\n"); a.stdin.flush()
        assert a.stdout.readline().strip() == "stopped"
        assert fake.engine.available()[0]                             # b が使っている間は止めない
        b.stdin.write("\n"); b.stdin.flush()
        assert b.stdout.readline().strip() == "stopped"
        assert _gone(int(ra[2])) and not fake.engine.available()[0]
    finally:
        for p in (a, b):
            p.kill()


def test_engine_stops_on_sigterm_without_waiting_for_sigkill(fake, monkeypatch):
    """エンジンは SIGTERM で止まる（強制終了まで待たない）。"""
    monkeypatch.setattr(engine_process, "STOP_TIMEOUT_SECONDS", 5)
    lp = fake.make()
    lp.start()
    assert lp.wait_ready(timeout=15)
    pid = lp.pid
    started = time.monotonic()
    lp.stop()
    assert _gone(pid) and time.monotonic() - started < 3


def test_stop_kills_children_that_ignore_sigterm(fake, monkeypatch):
    monkeypatch.setenv("FAKE_ENGINE_MODE", "stubborn")
    monkeypatch.setattr(engine_process, "STOP_TIMEOUT_SECONDS", 1)
    lp = fake.make()
    lp.start()
    assert lp.wait_ready(timeout=15)
    child = int((fake.tmp / "child.pid").read_text())
    lp.stop()
    assert _gone(child) and not lp.state_path.exists()                # 残った子は強制終了する


def test_preload_through_launcher_initializes_the_selected_style(fake):
    lp = fake.make()
    lp.start()
    assert lp.wait_ready(timeout=15)
    engine_process.preload(lp, tts.VoiceSettings(engine="voicevox", speaker="30", rate=1.25, intonation=1.45,
                                                  pitch=0.02))
    assert (fake.tmp / "posts.txt").read_text().splitlines() == ["/initialize_speaker?speaker=30&skip_reinit=true"]
    lp.stop()


def test_does_not_start_non_local_or_missing_engine(fake, tmp_path):
    remote = tts.VoicevoxEngine("voicevox", "VOICEVOX", "http://192.0.2.1:50021", "")
    lp = engine_process.EngineProcess(remote, str(fake.script), state_dir=tmp_path / "s1")
    lp.start()
    assert not lp.owned and "Mac の中ではない" in lp.status and not lp.wait_ready(timeout=0.5)
    lp2 = engine_process.EngineProcess(fake.engine, str(tmp_path / "no-such-run"), state_dir=tmp_path / "s2")
    lp2.start()
    assert not lp2.owned and "見つかりません" in lp2.status


def test_for_settings_only_manages_http_engines(monkeypatch):
    assert engine_process.for_settings(tts.VoiceSettings()) is None                       # say
    lp = engine_process.for_settings(tts.VoiceSettings(engine="voicevox", speaker="30"))
    assert lp is not None and lp.engine is tts.ENGINES["voicevox"] and lp.extra == []
    lp = engine_process.for_settings(tts.VoiceSettings(engine="aivisspeech", speaker="1"))
    assert lp is not None and "--disable_sentry" in lp.extra
    assert engine_process.for_settings(tts.VoiceSettings(engine="openai", speaker="x")) is None


def test_selected_voice_settings_are_preserved(tmp_path, monkeypatch):
    """Owner が選んだ設定（VOICEVOX No.7 アナウンス・話速 1.25・抑揚 1.45・高さ +0.02）がそのまま読める。"""
    monkeypatch.setattr(tts, "SETTINGS_PATH", tmp_path / "voice" / "settings.json")
    tts.save_settings(tts.VoiceSettings(engine="voicevox", speaker="30", rate=1.25, intonation=1.45, pitch=0.02))
    s = tts.load_settings()
    assert (s.engine, s.speaker, s.rate, s.intonation, s.pitch) == ("voicevox", "30", 1.25, 1.45, 0.02)


def test_sigterm_becomes_system_exit_and_is_restored(fake):
    lp = fake.make()
    before = signal.getsignal(signal.SIGTERM)
    restore = engine_process.stop_on_signals(lp)
    try:
        with pytest.raises(SystemExit):
            os.kill(os.getpid(), signal.SIGTERM)
            time.sleep(1)
    finally:
        restore()
    assert signal.getsignal(signal.SIGTERM) == before
    assert engine_process.stop_on_signals(None)() is None              # 管理しないときは何もしない


# ---------------------------------------------------------------- 声の会話・評価の画面からの呼び出し

class FakeEngine:
    def __init__(self, calls):
        self.calls = calls

    def available(self):
        return True, ""

    def preload(self, style):
        self.calls.append(f"preload:{style}")
        return True


class FakeLauncher:
    def __init__(self, owned=True):
        self.calls, self.owned, self.status = [], owned, "VOICEVOX を起動しました"
        self.engine = FakeEngine(self.calls)

    def start(self):
        self.calls.append("start")

    def wait_ready(self, timeout=None):
        self.calls.append("wait")
        return True

    def stop(self):
        self.calls.append("stop")


def test_voice_chat_starts_waits_and_stops_launcher(monkeypatch):
    import voice_chat
    lp = FakeLauncher()
    seen = {}

    def fake_main(listen, transcribe, speaker, push_to_talk, keys, barge_in, launcher):
        voice_chat.wait_engine(launcher)
        seen["calls"] = list(lp.calls)
        return 0
    monkeypatch.setattr(voice_chat, "_main", fake_main)
    assert voice_chat.main(listen=lambda: None, launcher=lp) == 0
    assert seen["calls"][:2] == ["start", "wait"] and seen["calls"][2].startswith("preload:")
    assert lp.calls[-1] == "stop"


def test_voice_chat_stops_launcher_even_when_the_conversation_fails(monkeypatch):
    import voice_chat
    lp = FakeLauncher()

    def boom(*a):
        raise RuntimeError("会話の途中の失敗")
    monkeypatch.setattr(voice_chat, "_main", boom)
    with pytest.raises(RuntimeError):
        voice_chat.main(listen=lambda: None, launcher=lp)
    assert lp.calls[-1] == "stop"


def test_voice_chat_without_microphone_starts_nothing(monkeypatch):
    import voice_chat
    from voice import record as mic
    monkeypatch.setattr(mic, "has_input_device", lambda: False)
    made = []
    monkeypatch.setattr(engine_process, "for_settings", lambda s: made.append(s))
    assert voice_chat.main() == 1 and made == []


def test_voice_chat_does_not_wait_for_reused_engine():
    import voice_chat
    lp = FakeLauncher(owned=False)
    voice_chat.wait_engine(lp)
    assert "wait" not in lp.calls and lp.calls[0].startswith("preload:")      # 待たずに話者だけ読み込む


def test_voice_eval_main_starts_and_stops_engine(monkeypatch):
    from acc import voice_eval
    lp = FakeLauncher()
    monkeypatch.setattr(engine_process, "for_settings", lambda s: lp)

    class Server:
        server_address = ("127.0.0.1", 0)

        def serve_forever(self):
            lp.calls.append("serve")
            raise KeyboardInterrupt

        def server_close(self):
            pass
    monkeypatch.setattr(voice_eval, "make_server", lambda port: (Server(), "t"))
    assert voice_eval.main([]) == 0
    assert lp.calls[:2] == ["start", "wait"] and lp.calls[2].startswith("preload:")
    assert lp.calls[3:] == ["serve", "stop"]


def test_preload_posts_initialize_speaker_and_ignores_failures(monkeypatch):
    engine = tts.VoicevoxEngine("voicevox", "VOICEVOX", "http://127.0.0.1:50021", "")
    posted = []
    monkeypatch.setattr(engine, "_post", lambda path, body=None, timeout=10: posted.append(path) or b"")
    assert engine.preload("30") and posted == ["/initialize_speaker?speaker=30&skip_reinit=true"]
    assert not engine.preload("Kyoko")                                   # 番号でない話者は送らない
    monkeypatch.setattr(engine, "_post", lambda *a, **k: (_ for _ in ()).throw(OSError("接続できません")))
    assert not engine.preload("30")
    remote = tts.VoicevoxEngine("voicevox", "VOICEVOX", "http://192.0.2.1:50021", "")
    assert not remote.preload("30")                                      # Mac の外には送らない


# ---------------------------------------------------------------- 会話の途中で止まったときの起動し直し

def test_engine_stopped_mid_conversation_is_restarted_and_voice_returns(fake, monkeypatch):
    """会話の途中でエンジンが止まったら一度起動し直し、選んだ音声に戻す（Kyoko のままにしない。2026-10-18）。"""
    lp = fake.make()
    lp.start()
    assert lp.wait_ready(timeout=15)
    monkeypatch.setitem(tts.ENGINES, "voicevox", fake.engine)
    voiced, kyoko, notes = [], [], []
    monkeypatch.setattr(fake.engine, "synthesize", lambda text, s, cancel=None: (_ for _ in ()).throw(
        tts.TTSError("接続できません")) if not fake.engine.available()[0] else voiced.append(text) or np.zeros(5))
    monkeypatch.setattr(tts.ENGINES["say"], "synthesize",
                        lambda text, s, cancel=None: kyoko.append(text) or np.ones(5, dtype=np.float32))
    synth = tts.make_synth(tts.VoiceSettings(engine="voicevox", speaker="30", rate=1.25, intonation=1.45, pitch=0.02),
                           notify=notes.append, recover=lp.recover)
    synth("一文目。", threading.Event())
    old = lp.pid
    os.killpg(old, signal.SIGKILL)                                     # ほかの作業がエンジンを止めた
    assert _gone(old)
    synth("二文目。", threading.Event())
    assert voiced == ["一文目。", "二文目。"] and kyoko == []           # 起動し直して同じ声で読んだ
    assert lp.owned and lp.pid != old and any("戻りました" in n for n in notes)
    lp.stop()


def test_recovery_is_rate_limited_and_falls_back_to_kyoko(monkeypatch):
    engine = tts.VoicevoxEngine("voicevox", "VOICEVOX", "http://127.0.0.1:9", "")
    monkeypatch.setitem(tts.ENGINES, "voicevox", engine)
    monkeypatch.setattr(engine, "available", lambda: (True, ""))
    monkeypatch.setattr(engine, "synthesize", lambda *a, **k: (_ for _ in ()).throw(tts.TTSError("止まった")))
    kyoko = []
    monkeypatch.setattr(tts.ENGINES["say"], "synthesize", lambda text, s, cancel=None: kyoko.append(text) or np.ones(3))
    calls = []
    synth = tts.make_synth(tts.VoiceSettings(engine="voicevox", speaker="30"), notify=lambda m: None,
                           recover=lambda: calls.append(1) or False)
    for text in ("一。", "二。", "三。"):
        synth(text, threading.Event())
    assert calls == [1] and kyoko == ["一。", "二。", "三。"]           # 起動し直しは 30 秒に 1 回、文は Kyoko で読む


def test_recover_starts_engine_when_unavailable_at_conversation_start(fake, monkeypatch):
    lp = fake.make()
    monkeypatch.setitem(tts.ENGINES, "voicevox", fake.engine)
    monkeypatch.setattr(fake.engine, "synthesize", lambda *a, **k: np.zeros(5, dtype=np.float32))
    synth = tts.make_synth(tts.VoiceSettings(engine="voicevox", speaker="30"), notify=lambda m: None,
                           recover=lp.recover)
    assert lp.owned and fake.engine.available()[0]                     # 起動時に使えなければ、まず起動する
    assert synth("こんにちは。", threading.Event()) is not None
    lp.stop()


def test_corrupt_record_never_stops_and_is_rebuilt_only_when_engine_is_down(fake):
    fake.state.mkdir(parents=True, exist_ok=True)
    path = fake.state / "engine-voicevox.json"
    other = fake.independent()
    path.write_text("{壊れた")
    lp = fake.make()
    lp.start()
    assert not lp.owned and "記録が読めない" in lp.status
    lp.stop()
    assert other.poll() is None                                       # 持ち主が分からないものは止めない
    os.killpg(other.pid, signal.SIGKILL)
    other.wait()
    lp2 = fake.make()
    lp2.start()                                                       # エンジンが止まっていれば作り直して起動
    assert lp2.owned and lp2.wait_ready(timeout=15)
    lp2.stop()


def test_live_user_with_unknown_start_time_is_counted(fake, monkeypatch):
    """ほかの会話の開始時刻を調べられないときは、生きている会話として数える（止めない）。"""
    other = fake.independent()
    live = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"])
    try:
        _record(fake, other.pid, [_user(live.pid)])
        lp = fake.make()
        lp.start()
        real_ps = engine_process._ps
        monkeypatch.setattr(engine_process, "_ps", lambda pid, field: "" if pid == live.pid else real_ps(pid, field))
        lp.stop()
        assert other.poll() is None
    finally:
        live.kill()


def test_signal_from_another_thread_during_stop_is_deferred(fake):
    """停止の途中に（どのスレッドに届いても）SIGTERM が来ても、停止を終えてから終了する。"""
    lp = fake.make()
    lp.start()
    assert lp.wait_ready(timeout=15)
    pid = lp.pid
    restore = engine_process.stop_on_signals(lp)
    real = engine_process.EngineProcess._kill_group

    def kill_with_signal(self, pgid):
        t = threading.Thread(target=lambda: os.kill(os.getpid(), signal.SIGTERM))
        t.start(); t.join()
        time.sleep(0.2)                                              # ハンドラーが動く時間
        real(self, pgid)
    try:
        engine_process.EngineProcess._kill_group = kill_with_signal
        with pytest.raises(SystemExit):
            lp.stop()
    finally:
        engine_process.EngineProcess._kill_group = real
        restore()
    assert _gone(pid) and not lp.owned and not lp.state_path.exists()


def test_signal_deferred_in_reader_thread_ends_the_main_thread(fake):
    """読み上げのスレッドで起動し直している途中の SIGTERM は、終わった後にメインのスレッドで終了になる。"""
    lp = fake.make()
    restore = engine_process.stop_on_signals(lp)
    done = threading.Event()

    def reader():
        with engine_process._signals_held():
            os.kill(os.getpid(), signal.SIGTERM)
            time.sleep(0.3)                                          # 起動し直しの途中
        done.set()
    try:
        with pytest.raises(SystemExit):
            threading.Thread(target=reader, daemon=True).start()
            deadline = time.monotonic() + 5
            while time.monotonic() < deadline:
                time.sleep(0.05)
        assert done.wait(2)                                          # 読み上げのスレッドは区間を終えている
    finally:
        restore()


def test_missing_record_still_stops_engine_started_by_this_session(fake):
    lp = fake.make()
    lp.start()
    assert lp.wait_ready(timeout=15)
    pid = lp.pid
    lp.state_path.unlink()                                           # 記録が消えた
    lp.stop()
    assert _gone(pid) and not lp.owned                               # この会話が起動したものは止める


def test_group_alive_treats_failed_ps_as_alive(monkeypatch):
    monkeypatch.setattr(os, "killpg", lambda pgid, sig: None)        # グループはある
    monkeypatch.setattr(subprocess, "run", lambda *a, **k: subprocess.CompletedProcess(a, 1, "", "error"))
    assert engine_process._group_alive(12345)
    monkeypatch.setattr(subprocess, "run", lambda *a, **k: subprocess.CompletedProcess(a, 0, "Z\nZ\n", ""))
    assert not engine_process._group_alive(12345)                    # ゾンビだけなら消えた


def test_reaped_own_child_group_is_not_killed_without_identity(fake, monkeypatch):
    """回収済みの自分の子（pid が再利用され得る）は、記録と一致しなければグループを止めない。"""
    lp = fake.make()
    lp.start()
    assert lp.wait_ready(timeout=15)
    killed = []
    monkeypatch.setattr(engine_process.EngineProcess, "_same_process", lambda self, data: False)
    monkeypatch.setattr(engine_process.EngineProcess, "_kill_group", lambda self, pgid: killed.append(pgid))
    monkeypatch.setattr(engine_process, "_group_runs", lambda pgid, binary: False)   # 番号が別のプログラムに再利用された
    lp.proc.returncode = 0                                           # 回収済みとみなす
    pid = lp.pid
    lp.stop()
    assert killed == []
    os.killpg(pid, signal.SIGKILL)


def test_shared_engine_is_kept_even_if_identity_check_fails(fake, monkeypatch):
    lp = fake.make()
    lp.start()
    assert lp.wait_ready(timeout=15)
    live = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"])
    try:
        data = json.loads(lp.state_path.read_text())
        data["users"].append(_user(live.pid))
        lp.state_path.write_text(json.dumps(data))
        monkeypatch.setattr(engine_process.EngineProcess, "_same_process", lambda self, d: False)   # ps の一時的な失敗
        pid = lp.pid
        lp.stop()
        assert _alive_pid(pid) and fake.engine.available()[0]       # ほかの会話が使っているので止めない
        os.killpg(pid, signal.SIGKILL)
    finally:
        live.kill()


def test_started_engine_is_cleaned_up_when_record_cannot_be_written(fake, monkeypatch):
    def fail(self, data):
        raise OSError("書けません")
    monkeypatch.setattr(engine_process.EngineProcess, "_write", fail)
    lp = fake.make()
    lp.start()
    assert not lp.owned and "記録できない" in lp.status
    time.sleep(0.5)
    assert not _engines(fake) and not fake.engine.available()[0]


def test_recover_preloads_selected_speaker(fake):
    lp = fake.make()
    lp.start()
    assert lp.wait_ready(timeout=15)
    os.killpg(lp.pid, signal.SIGKILL)
    time.sleep(0.3)
    assert lp.recover(speaker="30")
    assert (fake.tmp / "posts.txt").read_text().splitlines() == ["/initialize_speaker?speaker=30&skip_reinit=true"]
    lp.stop()


def test_engine_that_exits_before_ready_does_not_leave_its_children(fake, monkeypatch):
    monkeypatch.setenv("FAKE_ENGINE_MODE", "fail_after_child")
    lp = fake.make()
    lp.start()
    pgid = lp.pid
    assert not lp.wait_ready(timeout=10) and "失敗" in lp.status
    assert not engine_process._group_alive(pgid)                     # 子も残っていない
    assert not lp.state_path.exists()


def test_engine_is_cleaned_up_when_start_time_cannot_be_read(fake, monkeypatch):
    real_ps = engine_process._ps
    monkeypatch.setattr(engine_process, "_ps", lambda pid, field: "" if field == "lstart" else real_ps(pid, field))
    lp = fake.make()
    lp.start()
    assert not lp.owned and "記録できない" in lp.status
    time.sleep(0.5)
    assert not _engines(fake) and not fake.engine.available()[0]


def test_last_session_stops_children_left_after_engine_main_process_died(fake):
    """共有中にエンジン本体が終わり子だけが残ったとき、起動していない最後の会話も子を止める。"""
    other = fake.independent()
    child = int((fake.tmp / "child.pid").read_text())
    _record(fake, other.pid, [_dead_pid()])
    lp = fake.make()
    lp.start()                                                       # 引き継ぐ（起動はしていない）
    assert lp.owned and lp.proc is None
    os.kill(other.pid, signal.SIGKILL)                               # 本体だけが落ちた
    other.wait()
    assert _alive_pid(child)
    lp.stop()
    assert _gone(child) and not lp.state_path.exists()


def test_joined_session_with_missing_record_leaves_engine(fake):
    other = fake.independent()
    _record(fake, other.pid, [_dead_pid()])
    lp = fake.make()
    lp.start()
    assert lp.owned and lp.proc is None
    (fake.state / "engine-voicevox.json").unlink()
    lp.stop()
    assert other.poll() is None and "残しました" in lp.status        # 起動していないものは、分からなければ止めない


def test_next_session_cleans_children_left_after_crashed_engine(fake):
    """前の会話が異常終了し、エンジン本体も終わって子だけが残ったら、次の会話が起動前に片付ける。"""
    other = fake.independent()
    child = int((fake.tmp / "child.pid").read_text())
    _record(fake, other.pid, [_dead_pid()])
    os.kill(other.pid, signal.SIGKILL)
    other.wait()
    assert _alive_pid(child)
    lp = fake.make()
    lp.start()
    assert _gone(child)                                              # 残った子は片付けた
    assert lp.owned and lp.wait_ready(timeout=15)                    # 新しく起動した
    lp.stop()


def test_recovery_while_shared_cleans_old_children_and_keeps_other_user(fake):
    """共有中に本体が落ち、片方の会話が起動し直す: 古い子を片付け、もう片方の会話を新しい記録に引き継ぐ。"""
    lp = fake.make()
    lp.start()
    assert lp.wait_ready(timeout=15)
    child = int((fake.tmp / "child.pid").read_text())
    live = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"])
    try:
        data = json.loads(lp.state_path.read_text())
        data["users"].append(_user(live.pid))                        # もう 1 つの会話が使っている
        lp.state_path.write_text(json.dumps(data))
        old = lp.pid
        os.kill(old, signal.SIGKILL)                                 # 本体だけが落ちた（子は残る）
        lp.proc.wait()
        assert lp.recover(timeout=15)
        assert _gone(child) and lp.pid != old
        users = [u["pid"] for u in json.loads(lp.state_path.read_text())["users"]]
        assert sorted(users) == sorted([live.pid, os.getpid()])     # もう 1 つの会話も一覧に残る
        new = lp.pid
        lp.stop()
        assert _alive_pid(new)                                       # もう 1 つの会話が使っているので止めない
        os.killpg(new, signal.SIGKILL)
    finally:
        live.kill()


def test_group_with_live_leader_is_never_treated_as_leftover(monkeypatch):
    """グループの先頭が生きている（番号が別の起動に再利用されたかもしれない）なら、残った子とみなさない。"""
    monkeypatch.setattr(engine_process, "_alive", lambda pid: True)
    assert not engine_process._group_runs(12345, "/x/run")


def test_carried_user_unregisters_from_restarted_engine_and_last_one_stops_it(fake):
    """A が起動し直したエンジンに引き継がれた B は、新しい記録から抜け、最後の会話がエンジンを止める。"""
    a = fake.make()
    a.start()
    assert a.wait_ready(timeout=15)
    b = fake.make()                                                  # 同じプロセスの別の会話の代わり
    b.pid, b.owned = a.pid, True
    old = a.pid
    os.kill(old, signal.SIGKILL)
    a.proc.wait()
    assert a.recover(timeout=15) and a.pid != old                    # 新しい記録の一覧に、この会話（a・b 共通）
    b.stop()                                                         # 古い pid の b も新しい記録から抜ける
    assert not b.owned and not a.state_path.exists() and _gone(a.pid)  # 最後の利用者として止めた
    a.stop()                                                         # もう一度呼んでもよい


def test_zombie_leader_counts_as_ended(monkeypatch):
    monkeypatch.setattr(engine_process, "_alive", lambda pid: True)
    monkeypatch.setattr(engine_process, "_ps", lambda pid, field: "Z" if field == "stat" else "")
    monkeypatch.setattr(subprocess, "run", lambda *a, **k: subprocess.CompletedProcess(a, 0, "S /x/run -c child\n", ""))
    assert engine_process._group_runs(12345, "/x/run")
