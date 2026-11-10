"""会話の遅延の記録（ADR-0027、T-1405）。1 回の発言ごとに、段階ごとの経過ミリ秒を 1 行の JSON で残す。

  start(mode) で今の発言の Turn を始め、mark(stage)・set(key, value) で段階と属性を記録し、finish() で保存する。
  今の Turn がなければ mark・set は何もしない（記録しない呼び出し元・テストに影響しない）。
  発言・答えの本文・音声は記録しない（段階の経過ミリ秒と、決まった名前の属性だけ）。

集計: python -m latency report [--days N] [--mode voice|text|web] [--skill NAME]
"""

import argparse
import contextvars
import json
import math
import os
import sys
import threading
import time
from collections import defaultdict
from datetime import datetime, timedelta
from pathlib import Path

# 段階（この順の想定。ない段階は記録しない）。audio_ready はブラウザでの声の会話で最初の文の音を保持した時刻（T-1502）
STAGES = ("segment", "stt", "route", "search", "llm_ready", "first_text", "audio_ready", "first_audio", "done")
# 属性と、受け付ける値の型（本文が入らないよう、名前と真偽だけ）
ATTRS = {"mode": str, "skill": str, "provider": str, "harness": str, "warm": bool, "fallback": bool, "ok": bool}
MODES = ("voice", "text", "web")    # web はブラウザでの声の会話（T-1502）
NAME_MAX = 64                       # skill・provider の名前の長さの上限
MAX_BYTES = 5 * 1024 * 1024         # これを超えたら .1 に回す（1 世代だけ残す）

_current: contextvars.ContextVar["Turn | None"] = contextvars.ContextVar("jarvis_latency_turn", default=None)


class Turn:
    """1 回の発言の記録。clock は経過時間を測る時計（既定は time.monotonic、テストで差し替える）。"""

    def __init__(self, mode: str, clock=None, at: float | None = None, wall: datetime | None = None):
        self.clock = clock or time.monotonic
        self.started = self.clock() if at is None else at
        self.wall = wall or datetime.now().astimezone()
        self.stages: dict[str, int] = {}
        self.attrs: dict = {}
        self._lock = threading.Lock()   # 読み上げ・LLM の読み取りのスレッドからも記録する
        self.set("mode", mode)

    def mark(self, stage: str, at: float | None = None) -> None:
        """stage までの経過ミリ秒を記録する（同じ段階は最初の 1 回だけ）。at は時計の値（省くと今）。"""
        if stage not in STAGES:
            raise ValueError(f"未知の段階です: {stage}")
        now = self.clock() if at is None else at
        with self._lock:
            if stage not in self.stages:
                self.stages[stage] = max(0, round((now - self.started) * 1000))

    def set(self, key: str, value) -> None:
        if key not in ATTRS:
            raise ValueError(f"記録しない属性です: {key}")
        if not isinstance(value, ATTRS[key]):
            raise ValueError(f"属性 {key} の値の型が違います: {type(value).__name__}")
        if key == "mode" and value not in MODES:
            raise ValueError(f"mode は {' / '.join(MODES)} のどれかです: {value}")
        if isinstance(value, str) and len(value) > NAME_MAX:
            raise ValueError(f"属性 {key} の値が長すぎます")
        with self._lock:
            self.attrs[key] = value

    def record(self) -> dict:
        with self._lock:
            return {"ts": self.wall.isoformat(timespec="seconds"), **self.attrs, "stages": dict(self.stages)}


def start(mode: str, clock=None, at: float | None = None) -> Turn:
    """今の発言の Turn を始めて返す（このスレッド・文脈の今の Turn にする）。"""
    turn = Turn(mode, clock=clock, at=at)
    _current.set(turn)
    return turn


def current() -> Turn | None:
    return _current.get()


def use(turn: Turn | None) -> None:
    """別のスレッドで turn を今の Turn にする（threading.Thread には文脈が引き継がれないため）。"""
    _current.set(turn)


def mark(stage: str, at: float | None = None) -> None:
    turn = _current.get()
    if turn is not None:
        turn.mark(stage, at)


def set(key: str, value) -> None:  # noqa: A001  latency.set(key, value) として呼ぶ
    turn = _current.get()
    if turn is not None:
        turn.set(key, value)


def log_path() -> Path:
    import config
    return Path(config.LATENCY_LOG)


def _write(record: dict, path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists() and path.stat().st_size > MAX_BYTES:
        os.replace(path, path.with_name(path.name + ".1"))
    with open(path, "a", encoding="utf-8") as f:
        f.write(json.dumps(record, ensure_ascii=False) + "\n")


def finish(turn: Turn | None = None, path: Path | None = None, **attrs) -> dict | None:
    """答えの完了（done）を記録し、1 行の JSON を追記する。attrs は最後に入れる属性（ok=False など）。
    書き込みの失敗は会話を止めない（標準エラーに 1 行出すだけ）。今の Turn は外す。"""
    turn = turn or _current.get()
    if turn is None:
        return None
    if _current.get() is turn:
        _current.set(None)
    try:
        for key, value in attrs.items():
            turn.set(key, value)
        turn.mark("done")
        record = turn.record()
        _write(record, Path(path) if path is not None else log_path())
        return record
    except Exception as e:  # noqa: BLE001  記録の失敗で会話を止めない
        print(f"[遅延を記録できませんでした] {type(e).__name__}: {e}", file=sys.stderr)
        return None


# ---------------------------------------------------------------- 集計

def percentile(values: list, p: float) -> int | float | None:
    """最近傍順位法（ceil(p * n) 番目）。"""
    if not values:
        return None
    ordered = sorted(values)
    return ordered[max(1, math.ceil(p * len(ordered))) - 1]


def load(path: Path | None = None) -> list[dict]:
    """記録を読む（.1 の古い世代を含む）。壊れた行は飛ばす。"""
    path = Path(path) if path is not None else log_path()
    records = []
    for p in (path.with_name(path.name + ".1"), path):
        try:
            lines = p.read_text(encoding="utf-8").splitlines()
        except OSError:
            continue
        for line in lines:
            try:
                rec = json.loads(line)
            except ValueError:
                continue
            if isinstance(rec, dict) and isinstance(rec.get("stages"), dict):
                records.append(rec)
    return records


def select(records: list[dict], days: int | None = None, mode: str | None = None, skill: str | None = None,
           now: datetime | None = None) -> list[dict]:
    if days is not None:
        since = (now or datetime.now().astimezone()) - timedelta(days=days)
        kept = []
        for r in records:
            try:
                ts = datetime.fromisoformat(r.get("ts", ""))
            except (TypeError, ValueError):
                continue
            if ts.tzinfo is None:
                ts = ts.astimezone()
            if ts >= since:
                kept.append(r)
        records = kept
    if mode:
        records = [r for r in records if r.get("mode") == mode]
    if skill:
        records = [r for r in records if r.get("skill") == skill]
    return records


def _fmt(value) -> str:
    return "-" if value is None else str(value)


def report(records: list[dict]) -> str:
    """段階ごとの件数・p50・p95 と、skill・provider・warm の組ごとの first_text・first_audio。"""
    if not records:
        return "記録がありません"
    lines = [f"記録 {len(records)} 件（ミリ秒）", "", f"{'段階':<12}{'件数':>6}{'p50':>8}{'p95':>8}"]
    for stage in STAGES:
        values = [r["stages"][stage] for r in records if isinstance(r["stages"].get(stage), (int, float))]
        if values:
            lines.append(f"{stage:<12}{len(values):>6}{percentile(values, 0.5):>8}{percentile(values, 0.95):>8}")
    groups: dict[tuple, list[dict]] = defaultdict(list)
    for r in records:
        groups[(str(r.get("skill", "-")), str(r.get("provider", "-")), str(r.get("warm", "-")))].append(r)
    lines += ["", f"{'skill':<20}{'provider':<14}{'warm':<7}{'件数':>6}"
                  f"{'first_text p50':>16}{'p95':>8}{'first_audio p50':>17}{'p95':>8}"]
    for (skill, provider, warm), rs in sorted(groups.items()):
        cells = []
        for stage in ("first_text", "first_audio"):
            values = [r["stages"][stage] for r in rs if isinstance(r["stages"].get(stage), (int, float))]
            cells.append((percentile(values, 0.5), percentile(values, 0.95)))
        lines.append(f"{skill:<20}{provider:<14}{warm:<7}{len(rs):>6}"
                     f"{_fmt(cells[0][0]):>16}{_fmt(cells[0][1]):>8}{_fmt(cells[1][0]):>17}{_fmt(cells[1][1]):>8}")
    return "\n".join(lines)


def main(argv: list[str] | None = None, path: Path | None = None, now: datetime | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m latency", description="会話の遅延の記録を集計する")
    sub = parser.add_subparsers(dest="command", required=True)
    rep = sub.add_parser("report", help="段階ごとの件数・p50・p95")
    rep.add_argument("--days", type=int, help="直近 N 日だけ")
    rep.add_argument("--mode", choices=MODES)
    rep.add_argument("--skill")
    args = parser.parse_args(argv)
    records = select(load(path), days=args.days, mode=args.mode, skill=args.skill, now=now)
    print(report(records))
    return 0


if __name__ == "__main__":
    sys.exit(main())
