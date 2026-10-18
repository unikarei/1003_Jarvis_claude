"""音声エンジン（VOICEVOX ENGINE・AivisSpeech Engine）を、声の会話の間だけ起動する（ADR-0029、2026-10-18 Owner 指示）。

- 選んだエンジンが起動していなければ、この会話で起動し（127.0.0.1 だけで待ち受ける）、使えるまで待つ。
- Jarvis の会話が起動したエンジンは、使っている会話の一覧（記録のファイル）で管理する。後から来た会話は一覧に加わり、
  最後の会話が終わったときだけ止める（ほかの会話が使っている間は止めない）。異常終了した会話は一覧から外す。
- Jarvis と関係なく起動していたエンジンは使うだけで、止めない。
- 止める相手は pid・プロセスの開始時刻・実行ファイル・ポート・プロセスのグループが記録と一致するものだけ（pid の再利用で
  別のものを止めない。開始時刻は秒単位のため、同じ秒に同じ pid・実行ファイル・ポートで別に起動したものとは区別できない）。
- 記録を読み書きできないときは、使っている会話が分からないので止めずに残す。
  プロセスのグループごと止め、グループが消えるまで待つ（残れば強制終了）。
- 記録の読み書きはファイルのロックで 1 つずつ（同時に起動した会話が 2 つ目のエンジンを起動しない）。
- 起動できない・途中で止まったときは、tts.make_synth が say（Kyoko）で読む（会話は止めない）。
常駐・自動起動の設定はしない（会話を起動したときだけ）。
"""

import contextlib
import fcntl
import json
import os
import signal
import subprocess
import threading
import time
from pathlib import Path
from urllib.parse import urlparse

from voice import tts

HOME_APPS = Path.home() / "Applications"
# エンジン名 → (実行ファイル, 追加の引数)。AivisSpeech は外部へのエラー送信（Sentry）を止める
LAUNCH = {
    "voicevox": (os.environ.get("JARVIS_VOICEVOX_BIN", str(HOME_APPS / "VOICEVOX-Engine" / "run")), []),
    "aivisspeech": (os.environ.get("JARVIS_AIVISSPEECH_BIN", str(HOME_APPS / "AivisSpeech-Engine" / "run")),
                    ["--disable_sentry"]),
}
READY_TIMEOUT_SECONDS = 90      # 起動から使えるまでの上限（音声のモデルの読み込みを含む）
STOP_TIMEOUT_SECONDS = 10       # 止める指示からグループが消えるまでの上限（超えたら強制終了）
POLL_SECONDS = 0.25


def _alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def _ps(pid: int, field: str) -> str:
    try:
        return subprocess.run(["ps", "-p", str(pid), "-o", f"{field}="], capture_output=True, text=True,
                              timeout=5).stdout.strip()
    except (OSError, subprocess.SubprocessError):
        return ""


def _me() -> dict:
    """この会話（プロセス）の印: pid と開始時刻（pid の再利用で別のプロセスを会話と取り違えない）。"""
    return {"pid": os.getpid(), "started": _ps(os.getpid(), "lstart")}


def _live_users(users, exclude_me: bool = True) -> list[dict]:
    """記録の会話のうち、今も同じプロセスとして動いているもの。"""
    me = os.getpid()
    out = []
    for u in users if isinstance(users, list) else []:
        if not isinstance(u, dict) or not isinstance(u.get("pid"), int):
            continue
        if exclude_me and u["pid"] == me:
            continue
        if not _alive(u["pid"]):
            continue
        started = _ps(u["pid"], "lstart")
        if not started or started == u.get("started"):   # 開始時刻を調べられないときは、生きているものとして数える
            out.append(u)
    return out


def _group_alive(pgid: int) -> bool:
    """プロセスのグループに、まだ終わっていないプロセスがあるか（回収待ちのゾンビだけなら消えたとみなす）。"""
    try:
        os.killpg(pgid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    try:
        done = subprocess.run(["ps", "-g", str(pgid), "-o", "stat="], capture_output=True, text=True, timeout=5)
    except (OSError, subprocess.SubprocessError):
        return True
    out = done.stdout.split()
    if done.returncode != 0 or not out:
        return True                                  # グループはある（killpg が通った）のに調べられない → 残っている
    return any(not st.startswith("Z") for st in out)


_deferral = {"depth": 0, "pending": None}
_deferral_lock = threading.Lock()


@contextlib.contextmanager
def _signals_held():
    """起動・停止の途中で SIGTERM・SIGHUP を受けても中断しないよう、区間の終わりまで stop_on_signals のハンドラーが
    終了を遅らせる（Python のシグナルのハンドラーはメインのスレッドで動くので、どのスレッドに届いても同じ）。"""
    with _deferral_lock:
        _deferral["depth"] += 1
    try:
        yield
    finally:
        with _deferral_lock:
            _deferral["depth"] -= 1
            pending = _deferral["pending"] if _deferral["depth"] == 0 else None
            if pending is not None:
                _deferral["pending"] = None
        if pending is not None:
            if threading.current_thread() is threading.main_thread():
                raise _Exit(128 + pending)
            os.kill(os.getpid(), pending)            # 読み上げのスレッド（起動し直しの途中）→ メインのスレッドで終了する


def _group_runs(pgid: int, binary: str) -> bool:
    """エンジン本体（グループの先頭、pid = グループの番号）が終わったグループに、この実行ファイルの子が残っているか。
    先頭が生きているなら false（グループの番号が別の起動に再利用されたものかもしれないので触らない）。"""
    if _alive(pgid) and not _ps(pgid, "stat").startswith("Z"):
        return False                                 # 先頭が動いている（回収待ちのゾンビは終わったものとみなす）
    try:
        done = subprocess.run(["ps", "-g", str(pgid), "-o", "stat=,command="], capture_output=True, text=True, timeout=5)
    except (OSError, subprocess.SubprocessError):
        return False
    for line in done.stdout.splitlines():
        stat, _, command = line.strip().partition(" ")
        if stat and not stat.startswith("Z") and binary in command:
            return True
    return False


class CorruptState(OSError):
    """記録のファイルが壊れている（中身を解釈できない）。使っている会話が分からないものとして扱う。"""


class EngineProcess:
    """1 つのエンジンの起動と停止。owned が True なら、この会話は使っている会話の一覧に入っていて、
    終わりに stop() で抜ける（最後の会話ならエンジンを止める）。"""

    def __init__(self, engine: "tts.VoicevoxEngine", binary: str, extra: list[str] | None = None,
                 state_dir: Path | None = None, ready_timeout: float = READY_TIMEOUT_SECONDS):
        self.engine, self.binary, self.extra = engine, binary, list(extra or [])
        self.state_dir = Path(state_dir) if state_dir is not None else tts.SETTINGS_PATH.parent
        self.ready_timeout = ready_timeout
        self.proc: subprocess.Popen | None = None    # この会話が起動したときだけ
        self.pid: int | None = None
        self.owned = False
        self.status = ""                             # 画面に出す一言
        self._lock = threading.Lock()

    # ------------------------------------------------------------ 記録のファイル

    @property
    def state_path(self) -> Path:
        return self.state_dir / f"engine-{self.engine.name}.json"

    @contextlib.contextmanager
    def _locked(self):
        """記録の読み書きを、ほかの会話（別のプロセス）と 1 つずつにする。"""
        self.state_dir.mkdir(parents=True, exist_ok=True)
        with open(self.state_dir / f"engine-{self.engine.name}.lock", "a") as f:
            fcntl.flock(f, fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(f, fcntl.LOCK_UN)

    def _read(self) -> dict | None:
        """記録。ないときは None。読めない（権限・入出力の失敗）ときは OSError（「ない」と取り違えない）。"""
        try:
            data = json.loads(self.state_path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return None
        except ValueError:
            raise CorruptState("記録が壊れています") from None
        if not isinstance(data, dict):
            raise CorruptState("記録の形が違います")
        return data

    def _write(self, data: dict) -> None:
        tmp = self.state_path.with_suffix(".tmp")
        tmp.write_text(json.dumps(data), encoding="utf-8")
        os.replace(tmp, self.state_path)

    def _remove(self) -> None:
        try:
            self.state_path.unlink()
        except OSError:
            pass

    def _same_process(self, data: dict) -> bool:
        """記録のエンジンが今も同じプロセスか（pid・開始時刻・実行ファイル。pid の再利用を除く）。"""
        pid = data.get("engine_pid")
        if not isinstance(pid, int) or not _alive(pid):
            return False
        command = _ps(pid, "command")
        return (bool(data.get("started")) and _ps(pid, "lstart") == data.get("started")
                and data.get("binary") == self.binary and self.binary in command
                and f"--port {data.get('port')}" in command          # 開始時刻は秒単位なので、ポートと
                and _ps(pid, "pgid") == str(data.get("pgid")))       # プロセスのグループも照合する

    # ------------------------------------------------------------ 起動・待機・停止

    def start(self) -> None:
        """待たずに起動する（使えるまで待つのは wait_ready）。Jarvis が起動したものがあれば一覧に加わる。"""
        with self._lock, _signals_held():             # 途中のシグナルは、所有を記録し終えてから終了にする
            if self.owned:
                return
            if not self.engine.local_ok:
                self.status = f"{self.engine.label} の接続先が Mac の中ではないため起動しません"
                return
            try:
                with self._locked():
                    self._start_locked()
            except OSError as e:
                self.status = f"{self.engine.label} の起動を管理できません（{e}）"

    def _start_locked(self) -> None:
        try:
            data = self._read()
        except CorruptState:
            if self.engine.available()[0]:           # 動いているエンジンの持ち主が分からない → 使うだけ（止めない）
                self.status = f"{self.engine.label} は起動済みのものを使います（記録が読めないので止めません）"
                return
            self._remove()                           # エンジンは動いていない → 記録を作り直してよい
            data = None
        if data is not None and self._same_process(data):
            users = _live_users(data.get("users"))
            data["users"] = users + [_me()]
            self._write(data)                        # Jarvis の会話が起動したもの → 一覧に加わる
            self.pid, self.owned = data["engine_pid"], True
            self.status = (f"{self.engine.label} はほかの会話が起動したものを一緒に使います" if users
                           else f"{self.engine.label} は前の会話が起動したものを引き継いで使います")
            return
        carried: list[dict] = []
        if data is not None:
            pgid = data.get("pgid")
            if isinstance(pgid, int) and not self.engine.available()[0] and _group_runs(pgid, self.binary):
                self._kill_group(pgid)               # 記録のエンジン本体は終わり、子だけが残っている → 片付ける
            if not self.engine.available()[0]:
                carried = _live_users(data.get("users"))   # 一緒に使っていた会話は、新しく起動するものの一覧に引き継ぐ
            self._remove()                           # 記録のエンジンはもうない（または別のプロセス）
        if self.engine.available()[0]:
            self.status = f"{self.engine.label} は起動済みのものを使います（Jarvis が起動したものではないので止めません）"
            return
        if not os.access(self.binary, os.X_OK):
            self.status = f"{self.engine.label} が見つかりません（{self.binary}）"
            return
        port = urlparse(self.engine.base_url).port
        if not port:
            self.status = f"{self.engine.label} の接続先にポートがありません（{self.engine.base_url}）"
            return
        log = self.state_dir / f"engine-{self.engine.name}.log"
        try:
            with open(log, "wb") as out:             # 起動のたびに書き直す（大きくならない）
                self.proc = subprocess.Popen(
                    [self.binary, "--host", "127.0.0.1", "--port", str(port), *self.extra],
                    stdin=subprocess.DEVNULL, stdout=out, stderr=out, start_new_session=True)
        except OSError as e:
            self.status = f"{self.engine.label} を起動できません（{e}）"
            self.proc = None
            return
        started = _ps(self.proc.pid, "lstart")
        try:
            if not started:
                raise OSError("開始時刻を調べられません")   # 後で同じプロセスか確かめられない → 管理しない
            self._write({"engine_pid": self.proc.pid, "pgid": self.proc.pid, "started": started,
                         "binary": self.binary, "port": port, "users": carried + [_me()]})
        except OSError as e:                         # 記録できない → 管理できないので、起動した子をすぐ片付ける
            self._kill_group(self.proc.pid)
            self.proc = None
            self.status = f"{self.engine.label} の起動を記録できないため止めました（{e}）"
            return
        self.pid, self.owned = self.proc.pid, True
        self.status = f"{self.engine.label} を起動しています…"

    def wait_ready(self, timeout: float | None = None) -> bool:
        """使えるようになるまで待つ。起動したものが終わった・時間切れなら False（この会話は一覧から抜ける）。"""
        deadline = time.monotonic() + (self.ready_timeout if timeout is None else timeout)
        while True:
            if self.engine.available()[0]:
                if self.proc is not None:
                    self.status = f"{self.engine.label} を起動しました（終了するときに止めます）"
                return True
            if self.proc is not None and self.proc.poll() is not None:
                self.status = f"{self.engine.label} の起動に失敗しました（終了コード {self.proc.returncode}）"
                if _group_alive(self.proc.pid):
                    # 親は終わったが、作った子が残っている。グループの番号はメンバーがいる間は再利用されないので、
                    # グループが残っている今なら、自分が起動したグループとして止めてよい
                    self._kill_group(self.proc.pid)
                self.stop()
                return False
            if not self.owned:
                return False                         # 起動していない（見つからない など）
            if time.monotonic() >= deadline:
                self.status = f"{self.engine.label} が {self.ready_timeout:.0f} 秒で使えるようになりませんでした"
                self.stop()
                return False
            time.sleep(POLL_SECONDS)

    def recover(self, timeout: float = 20, speaker: str | None = None) -> bool:
        """会話の途中でエンジンが止まったとき、起動し直す（Jarvis が起動したものなら記録を片付けてから）。
        使えるようになれば True。独立して動いていたものが止まったときも、この会話で起動する。"""
        if self.engine.available()[0]:
            return True
        self.stop()                                  # 止まったエンジンの記録を片付ける（動いていれば止めない）
        if self.owned:
            return False                             # 記録を読めない → 起動し直さない（取り違えを避ける）
        self.start()
        ready = self.wait_ready(timeout) if self.owned else self.engine.available()[0]
        if ready and speaker is not None:
            self.engine.preload(speaker)             # 起動し直した後の最初の文も速く
        return ready

    def stop(self) -> None:
        """この会話を一覧から外す。ほかに使っている会話がなければ、記録と一致するエンジンを止める。何度呼んでもよい。"""
        with self._lock, _signals_held():             # 途中のシグナルは、止めて片付け終えてから終了にする
            if not self.owned:
                return
            try:
                with self._locked():
                    finished = self._stop_locked()
            except OSError:
                # 記録を読み書きできない → ほかの会話が使っているかが分からないので、止めずに残す（使っている会話を
                # 壊さない方を選ぶ）。owned はそのままにし、もう一度 stop() できる。次の会話は記録が読めれば引き継ぐ
                self.status = f"{self.engine.label} の記録を読み書きできないため、止めずに残しました"
                return
            if not finished:
                return                               # 止めずに残した。owned・pid はそのまま（もう一度 stop() できる）
            if self.proc is not None:
                self.proc.poll()                     # 自分の子なら回収する
            self.proc, self.pid, self.owned = None, None, False

    def _own_child_unreaped(self) -> bool:
        """自分が起動した子で、まだ回収していない（pid とグループの番号が再利用されていない）。"""
        return self.proc is not None and self.proc.returncode is None

    def _stop_locked(self) -> None:
        data = self._read()
        if (data is not None and data.get("engine_pid") != self.pid and isinstance(data.get("users"), list)
                and any(isinstance(u, dict) and u.get("pid") == os.getpid() for u in data["users"])):
            # ほかの会話が起動し直したエンジンの一覧に、この会話も引き継がれている → そのエンジンの利用者として抜ける
            self.pid = data["engine_pid"]
        if data is None or data.get("engine_pid") != self.pid:
            # 記録が消えた・別の記録に置き換わった
            if self._own_child_unreaped() and _group_alive(self.proc.pid):
                # この会話が起動したもの（回収前の子なので取り違えない）→ 止める。一緒に使っていた会話があっても、
                # その会話は読み上げのときに起動し直す（recover）ので、声は戻る
                self._kill_group(self.proc.pid)
                return True
            # 一緒に使っていただけ → ほかの会話が使っているかが分からないので止めない
            self.status = f"{self.engine.label} の記録が見つからないため、止めずに残しました"
            return True
        users = _live_users(data.get("users"))
        if users:
            data["users"] = users                    # ほかの会話が使っている → 止めない（照合できなくても）
            self._write(data)
            return True
        pgid = data.get("pgid") if isinstance(data.get("pgid"), int) else self.pid
        if self._same_process(data):
            self._kill_group(pgid)
        elif self._own_child_unreaped() and _group_alive(self.proc.pid):
            self._kill_group(self.proc.pid)          # 開始時刻を記録できなかった場合も、回収前の自分の子は止める
        elif pgid and _group_runs(pgid, self.binary):
            # エンジン本体は終わったが、同じグループに子が残っている（グループの番号はメンバーがいる間は再利用されない。
            # 残っているのがこの実行ファイルのプロセスであることも確かめる）
            self._kill_group(pgid)
        self._remove()
        return True

    def _kill_group(self, pgid: int) -> None:
        try:
            os.killpg(pgid, signal.SIGTERM)          # エンジンが作った子のプロセスも一緒に
        except (ProcessLookupError, PermissionError):
            return
        deadline = time.monotonic() + STOP_TIMEOUT_SECONDS
        while time.monotonic() < deadline:
            if self.proc is not None:
                self.proc.poll()
            if not _group_alive(pgid):
                return
            time.sleep(0.1)
        try:
            os.killpg(pgid, signal.SIGKILL)          # グループに残ったもの（SIGTERM を無視した子など）
        except (ProcessLookupError, PermissionError):
            pass
        if self.proc is not None:
            try:
                self.proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                pass


def preload(launcher: "EngineProcess | None", settings: "tts.VoiceSettings") -> None:
    """選んだ話者のモデルを読み込ませておく（使えるときだけ。失敗は無視）。"""
    if launcher is not None and launcher.engine.available()[0]:
        launcher.engine.preload(settings.speaker)


def for_settings(settings: "tts.VoiceSettings", state_dir: Path | None = None) -> EngineProcess | None:
    """選んだ音声のエンジンが、起動を管理するもの（VOICEVOX・AivisSpeech）なら EngineProcess。say などは None。"""
    if settings.engine not in LAUNCH or settings.engine not in tts.ENGINES:
        return None
    binary, extra = LAUNCH[settings.engine]
    return EngineProcess(tts.ENGINES[settings.engine], binary, extra, state_dir=state_dir)


class _Exit(SystemExit):
    pass


def stop_on_signals(launcher: EngineProcess | None):
    """SIGTERM・SIGHUP（ターミナルを閉じたなど）でも finally の停止が走るよう、SystemExit に変える。
    戻り値は元の設定に戻す関数。メインのスレッドからだけ呼ぶ。"""
    if launcher is None or threading.current_thread() is not threading.main_thread():
        return lambda: None
    previous = {}

    def handler(signum, frame):
        with _deferral_lock:
            if _deferral["depth"] > 0:               # 起動・停止の途中 → 区間の終わりで終了する
                _deferral["pending"] = signum
                return
        raise _Exit(128 + signum)
    for sig in (signal.SIGTERM, signal.SIGHUP):
        previous[sig] = signal.signal(sig, handler)

    def restore():
        for sig, old in previous.items():
            signal.signal(sig, old)
    return restore
