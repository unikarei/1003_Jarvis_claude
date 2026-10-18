"""実マイクの評価の画面（ACC、../docs/decisions.md ADR-0027、T-1406）。

決まった 10 の文を Owner が読み上げ、1 件ずつ Mac のマイクで録って文字にし、認識の誤り（CER）と、
話し終えてから最初の声までの時間（mode="full"）を測る。10 件の結果を data/voice/eval/<日時>.json に保存し、
CER の平均と、最初の声までの p50・p95（目標 p50 2 秒・p95 5 秒）に対する達否を示す。
音声はメモリの中だけで扱い、ファイルに書かない。書き込み・確認のいるスキルは実行しない（振り分けの結果だけ）。
127.0.0.1 に固定し、起動のたびに作るトークンで認証する（acc/voice_settings.py と同じ守り）。有料の API は使わない。

使い方: python -m acc.voice_eval [--port 8773]
"""

import argparse
import hmac
import json
import os
import secrets
import subprocess
import sys
import threading
import time
import unicodedata
from datetime import datetime
from http.cookies import SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import config
import latency
from voice import speak, tts

HOST = "127.0.0.1"
DEFAULT_PORT = 8773
COOKIE = "jarvis_voice_eval"
CSRF_HEADER = "X-Jarvis-Console"
MAX_BODY = 10_000
MODES = ("stt", "full")
TARGET_P50_MS = 2000                 # 話し終えてから最初の声まで（暫定の目標、ADR-0027）
TARGET_P95_MS = 5000
MEASURE_TIMEOUT_SECONDS = 150        # 1 件の答えを待つ上限（スキルの実行の上限 120 秒 + 合成）
REPORTED_STAGES = ("segment", "stt", "route", "first_text", "first_audio")   # 返す・保存する段階
RECENT_RUNS = 10                    # GET /api/results で返す直近の評価の数

EVAL_SENTENCES = (
    "ジャービス、今日のAIニュースで重要なものを三つ教えて",
    "大洗の民泊の物件について、補助金の申請期限はいつだった？",
    "タービンブレードのフラッタ解析の手順を説明して",
    "Obsidianのナレッジから、民泊に関する最新のメモを探して",
    "接続しているナレッジデータベースを、登録件数の多い順に上位三つ教えて",
    "毎回エンターを押すのは会話っぽくないから、もっとスムーズな会話にできないのかな",
    "えっと、大洗の、いや、ひたちなかの物件の話なんだけど、駐車場は何台分あったっけ",
    "システムの状態は？",
    "今の会話をナレッジ化して",
    "ピアノ教室のホームページのアクセス数は先週と比べて増えた？",
)


def eval_dir() -> Path:
    """結果の保存先（JARVIS_VOICE_EVAL_DIR で変更可。テストでは config の data/ の一時フォルダに移る）。"""
    return Path(os.environ.get("JARVIS_VOICE_EVAL_DIR", config.CKC_DATA_DIR.parent / "voice" / "eval"))


# ---------------------------------------------------------------- 認識の誤り（CER）

def normalize(text: str) -> str:
    """NFKC で正規化し、空白・句読点・記号を除く（Unicode の分類 Z・P・S と制御文字）。"""
    text = unicodedata.normalize("NFKC", text or "")
    return "".join(c for c in text if not c.isspace() and unicodedata.category(c)[0] not in "ZPSC")


def edit_distance(a: str, b: str) -> int:
    """文字の編集距離（置換・挿入・削除を 1 とする）。"""
    prev = list(range(len(b) + 1))
    for i, ca in enumerate(a, 1):
        cur = [i]
        for j, cb in enumerate(b, 1):
            cur.append(min(prev[j] + 1, cur[j - 1] + 1, prev[j - 1] + (ca != cb)))
        prev = cur
    return prev[-1]


def cer(reference: str, hypothesis: str) -> float:
    """文字の誤り率 = 正規化した編集距離 ÷ 正解の文字数。"""
    ref, hyp = normalize(reference), normalize(hypothesis)
    if not ref:
        return 0.0 if not hyp else 1.0
    return round(edit_distance(ref, hyp) / len(ref), 4)


# ---------------------------------------------------------------- 集計と保存

def summarize(items: list[dict]) -> dict:
    """CER の平均と、話し終えてから最初の声まで（first_audio）の p50・p95（最近傍順位法）、目標の達否。"""
    cers = [i["cer"] for i in items if isinstance(i.get("cer"), (int, float))]
    audio = [i["stages"]["first_audio"] for i in items
             if isinstance((i.get("stages") or {}).get("first_audio"), (int, float))]
    p50, p95 = latency.percentile(audio, 0.5), latency.percentile(audio, 0.95)
    return {"count": len(items), "cer_mean": round(sum(cers) / len(cers), 4) if cers else None,
            "first_audio_count": len(audio), "p50_ms": p50, "p95_ms": p95,
            "target_p50_ms": TARGET_P50_MS, "target_p95_ms": TARGET_P95_MS,
            "p50_met": None if p50 is None else p50 <= TARGET_P50_MS,
            "p95_met": None if p95 is None else p95 <= TARGET_P95_MS}


SAVED_KEYS = ("index", "sentence", "mode", "text", "cer", "stages", "skill", "method", "provider", "executed",
              "note", "measured_at")


def save_run(run: dict, folder: Path | None = None) -> Path:
    """1 回の評価を保存する（決まった項目だけ。音声は含めない）。"""
    folder = folder or eval_dir()
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / f"{run['id']}.json"
    items = [{k: i.get(k) for k in SAVED_KEYS} for i in sorted(run["items"].values(), key=lambda x: x["index"])]
    data = {"id": run["id"], "started": run["started"], "items": items, "summary": summarize(items)}
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(tmp, path)
    return path


def load_runs(folder: Path | None = None, limit: int = RECENT_RUNS) -> list[dict]:
    """直近の評価（新しい順）。壊れたファイルは飛ばす。"""
    folder = folder or eval_dir()
    runs = []
    for p in sorted(folder.glob("*.json"), reverse=True) if folder.is_dir() else []:
        try:
            data = json.loads(p.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if isinstance(data, dict) and isinstance(data.get("items"), list):
            data["summary"] = summarize([i for i in data["items"] if isinstance(i, dict)])
            runs.append(data)
        if len(runs) >= limit:
            break
    return runs


# ---------------------------------------------------------------- 1 件の計測

class Busy(Exception):
    """計測の実行中。"""


class NoMicrophone(Exception):
    """マイクがない。"""


class NoSpeech(Exception):
    """話し始めを検出できなかった。"""


def _executable(skill_name: str) -> bool:
    """評価の画面から実行してよいスキル: 権限が read で確認のいらないもの。"""
    import orchestrator
    import skills
    skill = skills.active().get(skill_name)
    return skill is not None and orchestrator.effective(skill) == ("read", False)


# ---------------------------------------------------------------- 読み上げ（ファイルを使わない）

def _popen(cmd: list[str]) -> subprocess.Popen:
    return subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


class SaySpeaker(speak.Speaker):
    """macOS の say で直接再生する（-o を使わず、音声をファイルに書かない）。voice.speak.Speaker に話速を足し、
    say を起動した時に on_start を呼ぶ（最初の声は say の再生の開始＝起動の時刻で記録する）。"""

    def __init__(self, voice: str, rate: float, device: str | None, on_start):
        self.rate, self._on_start = rate, on_start
        super().__init__(voice=voice, device=device, runner=self._start)

    def _command(self, sentence: str) -> list[str]:
        cmd = super()._command(sentence)
        return cmd[:1] + ["-r", str(int(tts.SAY_BASE_RATE * self.rate))] + cmd[1:]

    def _start(self, cmd: list[str]) -> subprocess.Popen:
        proc = _popen(cmd)
        self._on_start()
        return proc


def make_speaker(on_start):
    """選んだ音声（python -m acc.voice_settings、voice_chat.build_speaker と同じ設定）で、音声をファイルに書かない
    経路だけで読み上げる。ローカルの HTTP エンジン（voicevox・aivisspeech）はメモリの中で合成して再生し、say は
    直接再生する（say の合成の -o は一時ファイルを書くため使わない）。HTTP エンジンで合成できない文は、say の
    一時ファイルに切り替えず読まない。HTTP エンジンが使えなければ say（Kyoko）で直接再生する。"""
    import voice_chat
    from voice import duplex
    settings = tts.load_settings()
    chosen = tts.engine(settings.engine)
    if settings.engine != "say" and chosen.available()[0]:
        def synth(sentence: str, cancel: threading.Event):
            try:
                return chosen.synthesize(sentence, settings, cancel)
            except tts.TTSError:
                return None
        player = duplex.Player(device=voice_chat.output_device())
        return duplex.DuplexSpeaker(player, voice=voice_chat.VOICE, synth=synth)
    if settings.engine != "say":
        settings = tts.VoiceSettings()
    return SaySpeaker(settings.speaker or "Kyoko", settings.rate, voice_chat.output_device(), on_start)


class Evaluator:
    """計測を 1 件ずつ行い、今の評価（10 件）の結果を持つ。"""

    def __init__(self, folder: Path | None = None):
        self.folder = folder
        self._busy = threading.Lock()      # 計測と評価の切り替えを同じロックで守る
        self._run: dict | None = None

    def new_run(self) -> dict:
        """新しい評価を始める。計測の実行中は Busy（進行中の結果が別の評価に入らないように）。"""
        if not self._busy.acquire(blocking=False):
            raise Busy("計測の実行中です。終わってから新しい評価を始めてください")
        try:
            return self._start_run()
        finally:
            self._busy.release()

    def _start_run(self) -> dict:
        """評価ごとに一意な ID（マイクロ秒まで。同じ ID・同じファイルがあれば番号を足す）。ロックを持って呼ぶ。"""
        now = datetime.now().astimezone()
        folder = self.folder or eval_dir()
        used = {self._run["id"]} if self._run else set()
        run_id = base = now.strftime("%Y%m%d_%H%M%S_%f")
        n = 0
        while run_id in used or (folder / f"{run_id}.json").exists():
            n += 1
            run_id = f"{base}_{n}"
        self._run = {"id": run_id, "started": now.isoformat(timespec="seconds"), "items": {}}
        return self._run

    def current(self) -> dict | None:
        if self._run is None:
            return None
        items = [self._run["items"][k] for k in sorted(self._run["items"])]
        return {"id": self._run["id"], "started": self._run["started"], "items": items, "summary": summarize(items)}

    def measure(self, index: int, mode: str) -> dict:
        if isinstance(index, bool) or not isinstance(index, int) or not 1 <= index <= len(EVAL_SENTENCES):
            raise ValueError(f"index は 1〜{len(EVAL_SENTENCES)} です")
        if mode not in MODES:
            raise ValueError(f"mode は {' / '.join(MODES)} のどれかです")
        if not self._busy.acquire(blocking=False):
            raise Busy("計測の実行中です。終わってからもう一度押してください")
        try:
            result = self._measure(index, mode)
            if self._run is None:
                self._start_run()
            self._run["items"][index] = result
            save_run(self._run, self.folder)
            return result
        finally:
            self._busy.release()

    def _measure(self, index: int, mode: str) -> dict:
        import voice_chat
        from voice import record as mic
        if not mic.has_input_device():
            raise NoMicrophone("マイクが見つかりません。ヘッドセットなどをつないでから押してください")
        from speech_to_text import transcribe_array
        spoken = voice_chat.SpeechEnd()
        with spoken.watching():                     # 話し終えた時刻（声のあった最後のブロック）を残す
            audio = mic.record_utterance()
        segmented = time.monotonic()
        if audio is None:
            raise NoSpeech("話し始めを検出できませんでした。文を読み上げてから黙ってください")
        text = transcribe_array(audio).strip()
        transcribed = time.monotonic()
        del audio                                   # 音声はここで手放す（ファイルに書かない）
        turn = voice_chat.start_turn(spoken.last, segmented, transcribed)
        result = {"index": index, "sentence": EVAL_SENTENCES[index - 1], "mode": mode, "text": text,
                  "cer": cer(EVAL_SENTENCES[index - 1], text), "skill": None, "method": None, "provider": None,
                  "executed": False, "note": "", "measured_at": datetime.now().astimezone().isoformat(timespec="seconds")}
        if mode == "full":
            self._respond(text, turn, result)
        result["stages"] = {k: v for k, v in turn.record()["stages"].items() if k in REPORTED_STAGES}
        return result

    def _respond(self, text: str, turn: "latency.Turn", result: dict) -> None:
        """振り分け → 読み取りのスキルだけ実行 → 最初の 1 文を読み上げ、最初の声の再生開始で止める。"""
        import orchestrator
        import voice_chat
        from voice.speak import speech_summary
        ctx = orchestrator.Context(voice=True)
        r = orchestrator.route(text, ctx)
        turn.mark("route")
        turn.set("skill", r.skill)
        result.update(skill=r.skill, method=r.method)
        if not _executable(r.skill):
            result["note"] = "実行しない（書き込みのあるスキル）"
            return
        speaker = make_speaker(on_start=lambda: "first_audio" in turn.stages or turn.mark("first_audio"))
        stop = threading.Event()
        fed = {"any": False}
        voice_chat.track_first_audio(speaker, lambda: turn)   # 再生の待ちの列のある読み上げ（HTTP エンジン）

        def stream(chunk: str) -> None:
            if stop.is_set():
                raise voice_chat.Cancelled()
            fed["any"] = True
            speaker.feed(chunk)
        ctx.stream, ctx.cancel = stream, stop.is_set
        box: dict = {}

        def work() -> None:
            latency.use(turn)
            try:   # 書き込みの確認には常に「いいえ」（振り分けの判定と実行の間で結果が変わっても実行しない）
                box["outcome"] = orchestrator.handle(text, ctx, ask=lambda prompt: "n")
            except Exception as e:  # noqa: BLE001  取り消し・失敗でも計測は続ける
                box["error"] = f"{type(e).__name__}: {e}"
        worker = threading.Thread(target=work, name="jarvis-voice-eval", daemon=True)
        worker.start()
        deadline = time.monotonic() + MEASURE_TIMEOUT_SECONDS
        answered = False
        try:
            while "first_audio" not in turn.stages:
                if time.monotonic() > deadline:
                    result["note"] = "時間切れ（最初の声まで届きませんでした）"
                    break
                if not answered and not worker.is_alive():
                    answered = True
                    outcome = box.get("outcome")
                    if outcome is None or not outcome.text:
                        result["note"] = box.get("error", "答えがありません")
                        break
                    turn.mark("first_text")             # 逐次表示のないスキルは、答えができた時点
                    if fed["any"]:
                        speaker.flush()
                    else:
                        speaker.say(speech_summary(outcome.skill, outcome.text))
                if answered and not speaker.speaking():
                    if "first_audio" not in turn.stages:
                        result["note"] = "読み上げる文がありませんでした"
                    break
                time.sleep(0.01)
        finally:
            stop.set()                                   # 最初の声まで測ったら、生成と読み上げを止める
            speaker.stop()
            worker.join(timeout=5)
            speaker.close()
        outcome = box.get("outcome")
        result["executed"] = True
        result["provider"] = turn.attrs.get("provider") or ctx.last_provider
        if outcome is not None and outcome.skill != r.skill:
            result["skill"] = outcome.skill               # 実行の時に振り分けが変わった（記録だけ）


# ---------------------------------------------------------------- 画面

def make_handler(token: str, port: int, evaluator: Evaluator):
    allowed_hosts = {f"127.0.0.1:{port}", f"localhost:{port}"}
    allowed_origins = {f"http://{h}" for h in allowed_hosts}

    class Handler(BaseHTTPRequestHandler):
        server_version = "JarvisVoiceEval"

        def log_message(self, fmt, *args):
            sys.stderr.write(f"{self.command} {urlparse(self.path).path} -> {args[1] if len(args) > 1 else ''}\n")

        def _send(self, code, body: bytes, ctype: str, headers: dict | None = None):
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("X-Frame-Options", "DENY")
            self.send_header("Content-Security-Policy", "default-src 'self'; script-src 'unsafe-inline'; "
                             "style-src 'unsafe-inline'; frame-ancestors 'none'")
            for k, v in (headers or {}).items():
                self.send_header(k, v)
            self.end_headers()
            self.wfile.write(body)

        def _json(self, code, data):
            self._send(code, json.dumps(data, ensure_ascii=False).encode("utf-8"), "application/json; charset=utf-8")

        def _guard(self, write=False) -> bool:
            if self.headers.get("Host", "") not in allowed_hosts:
                self._json(403, {"error": "Host が違います"})
                return False
            cookie = SimpleCookie(self.headers.get("Cookie", ""))
            value = cookie[COOKIE].value if COOKIE in cookie else ""
            if not hmac.compare_digest(value.encode(), token.encode()):
                self._json(401, {"error": "認証が必要です（起動時にターミナルに表示した URL を開いてください）"})
                return False
            if write:
                origin = self.headers.get("Origin")
                if self.headers.get(CSRF_HEADER) != "1" or (origin and origin not in allowed_origins):
                    self._json(403, {"error": "操作の要求の形が違います"})
                    return False
            return True

        def do_GET(self):
            url = urlparse(self.path)
            if url.path == "/" and "token" in parse_qs(url.query):
                if self.headers.get("Host", "") not in allowed_hosts:
                    return self._json(403, {"error": "Host が違います"})
                if not hmac.compare_digest(parse_qs(url.query)["token"][0].encode(), token.encode()):
                    return self._json(401, {"error": "トークンが違います"})
                return self._send(303, b"", "text/plain", {
                    "Location": "/", "Set-Cookie": f"{COOKIE}={token}; HttpOnly; SameSite=Strict; Path=/"})
            if not self._guard():
                return
            if url.path == "/":
                return self._send(200, PAGE.encode("utf-8"), "text/html; charset=utf-8")
            if url.path == "/api/sentences":
                return self._json(200, {"sentences": [{"index": i, "text": s} for i, s in enumerate(EVAL_SENTENCES, 1)],
                                        "modes": list(MODES)})
            if url.path == "/api/results":
                return self._json(200, {"current": evaluator.current(), "runs": load_runs(evaluator.folder)})
            self._json(404, {"error": "ありません"})

        def do_POST(self):
            if not self._guard(write=True):
                return
            try:
                length = int(self.headers.get("Content-Length") or 0)
            except ValueError:
                return self._json(400, {"error": "Content-Length が違います"})
            if not 0 <= length <= MAX_BODY:              # 負の値・大きすぎる要求は読まない
                return self._json(400, {"error": "要求が大きすぎるか、長さが違います"})
            try:
                body = json.loads(self.rfile.read(length) or b"{}")
            except ValueError:
                return self._json(400, {"error": "JSON ではありません"})
            if not isinstance(body, dict):
                return self._json(400, {"error": "JSON のオブジェクトではありません"})
            if self.path == "/api/measure":
                try:
                    return self._json(200, evaluator.measure(body.get("index"), body.get("mode", "stt")))
                except ValueError as e:
                    return self._json(400, {"error": str(e)})
                except Busy as e:
                    return self._json(409, {"error": str(e)})
                except NoMicrophone as e:
                    return self._json(503, {"error": str(e)})
                except NoSpeech as e:
                    return self._json(422, {"error": str(e)})
            if self.path == "/api/new":
                try:
                    run = evaluator.new_run()
                except Busy as e:
                    return self._json(409, {"error": str(e)})
                return self._json(200, {"id": run["id"]})
            self._json(404, {"error": "ありません"})

    return Handler


def make_server(port: int = DEFAULT_PORT, token: str | None = None, evaluator: Evaluator | None = None):
    token = token or secrets.token_urlsafe(32)
    server = ThreadingHTTPServer((HOST, port), None)
    server.RequestHandlerClass = make_handler(token, server.server_address[1], evaluator or Evaluator())
    return server, token


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="実マイクの評価（127.0.0.1 だけ）")
    parser.add_argument("--port", type=int, default=DEFAULT_PORT)
    args = parser.parse_args(argv)
    server, token = make_server(args.port)
    print(f"実マイクの評価: http://{HOST}:{server.server_address[1]}/?token={token}", flush=True)
    print("（この URL は他人に渡さない。Ctrl-C で終了。音声は保存しません）", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
    return 0


PAGE = """<!doctype html>
<html lang="ja"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>実マイクの評価</title>
<style>
:root { --bg:#f8fafc; --fg:#0f172a; --muted:#64748b; --card:#fff; --line:#e2e8f0; --ok:#16a34a; --ng:#dc2626; }
@media (prefers-color-scheme: dark) { :root { --bg:#0b1120; --fg:#e2e8f0; --muted:#94a3b8; --card:#111827; --line:#1f2937; } }
* { box-sizing:border-box; }
body { margin:0; font-family:-apple-system, "Hiragino Sans", "Noto Sans JP", sans-serif; background:var(--bg); color:var(--fg); }
header { padding:16px 24px; border-bottom:1px solid var(--line); }
header h1 { margin:0; font-size:19px; }
main { padding:16px 24px 48px; max-width:1100px; margin:0 auto; }
section { background:var(--card); border:1px solid var(--line); border-radius:10px; padding:12px 14px; margin-bottom:16px; }
h2 { font-size:16px; margin:0 0 10px; }
ol.steps { margin:0; padding-left:20px; font-size:14px; line-height:1.7; }
.muted { color:var(--muted); font-size:13px; }
label { display:inline-block; margin:6px 16px 6px 0; font-size:14px; }
button { font:inherit; padding:4px 12px; border-radius:6px; border:1px solid var(--line); background:var(--card); color:var(--fg); cursor:pointer; }
button:disabled { opacity:.5; cursor:default; }
.table { overflow-x:auto; }
table { border-collapse:collapse; width:100%; font-size:13px; }
td, th { border-bottom:1px solid var(--line); padding:6px 8px; text-align:left; vertical-align:top; }
.num { text-align:right; font-variant-numeric:tabular-nums; white-space:nowrap; }
.ok { color:var(--ok); } .ng { color:var(--ng); }
.stats { display:flex; flex-wrap:wrap; gap:24px; font-size:14px; }
.stats b { font-size:20px; display:block; }
@media (max-width:600px) { main, header { padding-left:16px; padding-right:16px; } }
</style></head><body>
<header><h1>実マイクの評価</h1>
<div class="muted">10 の文を読み上げ、認識の誤り（CER）と、話し終えてから最初の声までの時間を測る。音声は保存しない。</div></header>
<main>
<section><h2>手順</h2><ol class="steps">
<li>ヘッドセットかマイクをつなぎ、Mac の既定の入力になっていることを確かめる（読み上げはヘッドセットで聞く）。</li>
<li>静かな場所で行う（周囲の音でしきい値が上がると、話し始め・話し終わりの検出がずれる）。</li>
<li>文の「測る」を押してから、その文をふつうの速さで読み上げ、読み終えたら黙る（約 0.8 秒の無音で区切る）。</li>
<li>1〜10 をすべて測ったら、下の「集計」で CER の平均と p50・p95 の達否を見る。同じ文は測り直すと置き換わる。</li>
<li>「認識だけ」（stt）は文字にするところまで。「答えまで」（full）は振り分け・読み取りのスキルの実行・最初の 1 文の読み上げまで測る。
書き込み・確認のいるスキル（例: 9 の「ナレッジ化して」）は実行せず、振り分けの結果だけを示す。</li>
</ol></section>
<section><h2>評価の文</h2>
<label><input type="radio" name="mode" value="full" checked> 答えまで（full）</label>
<label><input type="radio" name="mode" value="stt"> 認識だけ（stt）</label>
<button id="new">新しい評価を始める</button> <span id="msg" class="muted"></span>
<div class="table"><table><thead><tr><th>#</th><th>文 / 認識した文</th><th class="num">CER</th><th class="num">区切り</th>
<th class="num">認識</th><th class="num">振り分け</th><th class="num">最初の文字</th><th class="num">最初の声</th><th>スキル / LLM</th><th></th></tr></thead>
<tbody id="rows"></tbody></table></div>
<p class="muted">時間はすべて話し終えた時点からのミリ秒。</p></section>
<section><h2>集計（今の評価）</h2><div class="stats" id="summary"></div></section>
<section><h2>直近の評価</h2><div class="table"><table><thead><tr><th>日時</th><th class="num">件数</th><th class="num">CER の平均</th>
<th class="num">p50</th><th class="num">p95</th></tr></thead><tbody id="runs"></tbody></table></div></section>
</main>
<script>
const H = {"Content-Type": "application/json", "X-Jarvis-Console": "1"};
const esc = s => String(s ?? "").replace(/[&<>"']/g, c => ({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"}[c]));
const $ = id => document.getElementById(id);
const ms = v => v == null ? "-" : v;
const pct = v => v == null ? "-" : (v * 100).toFixed(1) + "%";
let sentences = [], busy = false;
function met(v, ok) { return v == null ? "-" : `<span class="${ok ? "ok" : "ng"}">${v} ms（${ok ? "達成" : "未達"}）</span>`; }
function render(cur) {
  const items = {}; for (const it of (cur?.items || [])) items[it.index] = it;
  $("rows").innerHTML = sentences.map(s => {
    const it = items[s.index], st = it?.stages || {};
    const skill = it ? `${esc(it.skill || "-")}<div class="muted">${esc(it.provider || "")}${it.note ? " " + esc(it.note) : ""}</div>` : "";
    return `<tr><td>${s.index}</td><td>${esc(s.text)}${it ? `<div class="muted">→ ${esc(it.text)}（${esc(it.mode)}）</div>` : ""}</td>
      <td class="num">${it ? pct(it.cer) : "-"}</td><td class="num">${ms(st.segment)}</td><td class="num">${ms(st.stt)}</td>
      <td class="num">${ms(st.route)}</td><td class="num">${ms(st.first_text)}</td><td class="num">${ms(st.first_audio)}</td>
      <td>${skill}</td><td><button data-i="${s.index}" ${busy ? "disabled" : ""}>測る</button></td></tr>`;
  }).join("");
  const m = cur?.summary;
  $("summary").innerHTML = m ? `<div>件数<b>${m.count} / 10</b></div><div>CER の平均<b>${pct(m.cer_mean)}</b></div>
    <div>最初の声 p50（目標 ${m.target_p50_ms} ms）<b>${met(m.p50_ms, m.p50_met)}</b></div>
    <div>最初の声 p95（目標 ${m.target_p95_ms} ms）<b>${met(m.p95_ms, m.p95_met)}</b></div>
    <div class="muted">最初の声の件数 ${m.first_audio_count}（full で測った分だけ）</div>` : `<div class="muted">まだ測っていません</div>`;
}
function renderRuns(runs) {
  $("runs").innerHTML = runs.map(r => `<tr><td>${esc(r.started)}</td><td class="num">${r.summary.count}</td>
    <td class="num">${pct(r.summary.cer_mean)}</td><td class="num">${met(r.summary.p50_ms, r.summary.p50_met)}</td>
    <td class="num">${met(r.summary.p95_ms, r.summary.p95_met)}</td></tr>`).join("") || `<tr><td colspan="5" class="muted">なし</td></tr>`;
}
async function refresh() {
  const data = await (await fetch("/api/results")).json();
  render(data.current); renderRuns(data.runs);
}
$("rows").onclick = async e => {
  const b = e.target.closest("button[data-i]"); if (!b || busy) return;
  const mode = document.querySelector("input[name=mode]:checked").value;
  busy = true; await refresh();
  $("msg").textContent = `${b.dataset.i} の文を読み上げてください（読み終えたら黙る）…`;
  try {
    const r = await fetch("/api/measure", {method: "POST", headers: H, body: JSON.stringify({index: +b.dataset.i, mode})});
    const data = await r.json();
    $("msg").textContent = r.ok ? `${data.index}: CER ${pct(data.cer)}` + (data.stages.first_audio != null ? `、最初の声 ${data.stages.first_audio} ms` : "") : data.error;
  } catch (err) { $("msg").textContent = String(err); }
  busy = false; await refresh();
};
$("new").onclick = async () => {
  const r = await fetch("/api/new", {method: "POST", headers: H, body: "{}"});
  $("msg").textContent = r.ok ? "新しい評価を始めました" : (await r.json()).error;
  await refresh();
};
(async () => { sentences = (await (await fetch("/api/sentences")).json()).sentences; await refresh(); })();
</script></body></html>
"""

if __name__ == "__main__":
    sys.exit(main())
