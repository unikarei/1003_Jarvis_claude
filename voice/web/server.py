"""ブラウザでの声の会話の HTTP サーバー（T-1501、ADR-0031、../docs/browser-voice-spec.md §6・§7）。

標準ライブラリの ThreadingHTTPServer で 127.0.0.1 だけに置く。ブラウザ → サーバーは POST、サーバー → ブラウザは
SSE（EventSource）の 1 本。起動ごとのトークンを HttpOnly の Cookie に移して認証する（acc/voice_settings.py と同じ形）。
VS Code のポート転送は別の番号になることがあるため、Host・Origin は localhost・127.0.0.1 ならポートを問わない。
トークン・Cookie・本文・音声はログに出さない。
"""

import hmac
import json
import queue
import re
import secrets
import threading
import time
from http.cookies import CookieError, SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from voice import record as rec

HOST = "127.0.0.1"
DEFAULT_PORT = 8771
COOKIE = "jv"
CSRF_HEADER = "X-Jarvis-Voice"
STATIC_DIR = Path(__file__).parent / "static"
MAX_BODY = 4 * 1024 * 1024        # 発話 1 回分の WAV の上限
MAX_SMALL_BODY = 10_000           # 音声以外の POST の本文の上限
PING_SECONDS = 15.0
ATTACH_SECONDS = 30.0             # 会話を始めてから SSE をつなぐまでの猶予（つながなかった会話は次の開始で終える）
WAV_TYPES = ("audio/wav", "audio/x-wav", "audio/wave")

HOST_RE = re.compile(r"(localhost|127\.0\.0\.1)(:\d{1,5})?")
ORIGIN_RE = re.compile(r"http://(localhost|127\.0\.0\.1)(:\d{1,5})?")
STATIC_NAME_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]*\.(html|js|css)")
CONTENT_TYPES = {"html": "text/html; charset=utf-8", "js": "text/javascript; charset=utf-8",
                 "css": "text/css; charset=utf-8"}
CSP = ("default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; connect-src 'self'; "
       "img-src 'self' data:; media-src 'self' blob:; frame-ancestors 'none'; base-uri 'none'; form-action 'none'")

# ページへ渡す発話の検出の設定。振幅はブラウザの float（-1〜1）の RMS で、voice.record の int16 の値とは別
# （2026-11-03 の試作で確かめた値）
THRESHOLD_FACTOR = 4.0
MIN_THRESHOLD = 0.002
PREROLL_SECONDS = 0.3


def page_config() -> dict:
    silence = rec._env_float("JARVIS_VOICE_SILENCE_SECONDS", rec.SILENCE_SECONDS)
    if not silence > 0:
        silence = rec.SILENCE_SECONDS
    return {"silence_seconds": silence, "max_seconds": rec.MAX_SECONDS, "preroll_seconds": PREROLL_SECONDS,
            "threshold_factor": THRESHOLD_FACTOR, "min_threshold": MIN_THRESHOLD}


def default_session_factory(emit):
    from voice.web.session import WebSession
    return WebSession(emit=emit)


class Sessions:
    """同時に 1 つだけの会話と、その SSE に送るイベントの待ち行列。"""

    def __init__(self, factory):
        self.factory = factory
        self.lock = threading.Lock()
        self.current = None
        self.events: queue.Queue = queue.Queue()
        self.attached = False
        self.started = 0.0

    def start(self):
        with self.lock:
            s = self.current
            if s is not None and not s.closed:
                if self.attached or time.monotonic() - self.started < ATTACH_SECONDS:
                    return None           # 動いている会話を奪わない
                s.end()                   # SSE をつながないまま残った会話
            events: queue.Queue = queue.Queue()
            self.current = self.factory(events.put)
            self.events, self.attached, self.started = events, False, time.monotonic()
            return self.current

    def get(self, sid: str | None):
        with self.lock:
            s = self.current
            if s is None or s.closed or not sid or not hmac.compare_digest(s.id.encode(), sid.encode()):
                return None
            return s

    def attach(self, sid: str | None):
        """SSE をつなぐ。返り値は（会話, 待ち行列）。会話がない・すでにつながっていれば None。"""
        with self.lock:
            s = self.current
            if s is None or s.closed or not sid or not hmac.compare_digest(s.id.encode(), sid.encode()):
                return None
            if self.attached:
                return None
            self.attached = True
            return s, self.events


def make_handler(token: str, sessions: Sessions, ping_seconds: float):

    class Handler(BaseHTTPRequestHandler):
        server_version = "JarvisVoiceWeb"

        def log_message(self, fmt, *args):   # トークン・Cookie・本文を出さない
            pass

        # -------------------------------------------------------- 応答

        def _headers(self, code, ctype, length=None, extra=None):
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            if length is not None:
                self.send_header("Content-Length", str(length))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("X-Frame-Options", "DENY")
            self.send_header("Referrer-Policy", "no-referrer")
            self.send_header("Content-Security-Policy", CSP)
            for k, v in (extra or {}).items():
                self.send_header(k, v)
            self.end_headers()

        def _send(self, code, body: bytes, ctype: str, extra: dict | None = None):
            self._headers(code, ctype, len(body), extra)
            self.wfile.write(body)

        def _json(self, code, data):
            self._send(code, json.dumps(data, ensure_ascii=False).encode("utf-8"), "application/json; charset=utf-8")

        # -------------------------------------------------------- 確認

        def _host_ok(self) -> bool:
            if HOST_RE.fullmatch(self.headers.get("Host", "")):
                return True
            self._json(400, {"error": "bad_host"})
            return False

        def _cookie_ok(self) -> bool:
            try:
                cookie = SimpleCookie(self.headers.get("Cookie", ""))
            except CookieError:
                cookie = SimpleCookie()
            value = cookie[COOKIE].value if COOKIE in cookie else ""
            if value and hmac.compare_digest(value.encode(), token.encode()):
                return True
            self._json(403, {"error": "forbidden"})
            return False

        def _write_ok(self) -> bool:
            if not ORIGIN_RE.fullmatch(self.headers.get("Origin", "")) or self.headers.get(CSRF_HEADER) != "1":
                self._json(403, {"error": "forbidden"})
                return False
            return True

        def _query(self):
            url = urlparse(self.path)
            return url.path, {k: v[0] for k, v in parse_qs(url.query).items()}

        def _length(self):
            try:
                length = int(self.headers.get("Content-Length") or 0)
            except ValueError:
                return None
            return length if length >= 0 else None

        # -------------------------------------------------------- GET

        def do_GET(self):
            if not self._host_ok():
                return
            path, q = self._query()
            if path == "/" and "token" in q:
                if not hmac.compare_digest(q["token"].encode(), token.encode()):
                    return self._json(403, {"error": "forbidden"})
                return self._send(303, b"", "text/plain", {
                    "Location": "/", "Set-Cookie": f"{COOKIE}={token}; HttpOnly; SameSite=Strict; Path=/"})
            if not self._cookie_ok():
                return
            if path == "/":
                return self._static("index.html")
            if path.startswith("/static/"):
                return self._static(path[len("/static/"):])
            if path == "/api/config":
                return self._json(200, page_config())
            if path == "/api/events":
                return self._events(q.get("session"))
            self._json(404, {"error": "not_found"})

        def _static(self, name: str):
            if not STATIC_NAME_RE.fullmatch(name):
                return self._json(404, {"error": "not_found"})
            file = STATIC_DIR / name
            if not file.is_file() or file.resolve().parent != STATIC_DIR.resolve():
                return self._json(404, {"error": "not_found"})
            self._send(200, file.read_bytes(), CONTENT_TYPES[name.rsplit(".", 1)[1]])

        def _events(self, sid):
            attached = sessions.attach(sid)
            if attached is None:
                return self._json(404, {"error": "no_session"})
            session, events = attached
            try:
                self._headers(200, "text/event-stream; charset=utf-8", extra={"X-Accel-Buffering": "no"})
                self.wfile.flush()
                while True:
                    try:
                        event = events.get(timeout=ping_seconds)
                    except queue.Empty:
                        self.wfile.write(b": ping\n\n")
                        self.wfile.flush()
                        continue
                    self.wfile.write(b"data: " + json.dumps(event, ensure_ascii=False).encode("utf-8") + b"\n\n")
                    self.wfile.flush()
                    if event.get("type") == "ended":
                        break
            except OSError:              # ブラウザが閉じた・接続が切れた
                pass
            finally:
                session.end()            # 再接続は T-1503 で作る。それまでは切れたら会話を終える
                self.close_connection = True

        # -------------------------------------------------------- POST

        def do_POST(self):
            if not (self._host_ok() and self._cookie_ok() and self._write_ok()):
                return
            path, q = self._query()
            if path == "/api/utterance":
                return self._utterance(q)
            length = self._length()
            if length is None or length > MAX_SMALL_BODY:
                return self._json(400, {"error": "bad_length"})
            self.rfile.read(length)       # 本文は使わない
            if path == "/api/session":
                session = sessions.start()
                if session is None:
                    return self._json(409, {"error": "busy"})
                return self._json(200, {"session": session.id})
            if path == "/api/end":
                session = sessions.get(q.get("session"))
                if session is None:
                    return self._json(404, {"error": "no_session"})
                session.end()
                return self._json(200, {"ended": True})
            self._json(404, {"error": "not_found"})

        def _utterance(self, q):
            from voice.web.session import AudioError, Busy

            length = self._length()
            if length is None:
                return self._json(400, {"error": "bad_length"})
            if length > MAX_BODY:
                self.close_connection = True          # 大きすぎる本文は読まずに切る
                return self._json(413, {"error": "too_large"})
            ctype = self.headers.get("Content-Type", "").split(";")[0].strip().lower()
            if ctype not in WAV_TYPES:
                return self._json(400, {"error": "not_wav"})
            try:
                rate = int(q.get("rate", ""))
            except ValueError:
                return self._json(400, {"error": "bad_rate"})
            session = sessions.get(q.get("session"))
            if session is None:
                return self._json(404, {"error": "no_session"})
            data = self.rfile.read(length)
            try:
                turn = session.submit(data, rate)
            except AudioError as e:
                return self._json(e.status, {"error": "bad_audio", "message": str(e)})
            except Busy:
                return self._json(409, {"error": "thinking"})
            self._json(202, {"turn": turn})

    return Handler


def make_server(port: int = DEFAULT_PORT, session_factory=None, token: str | None = None,
                ping_seconds: float = PING_SECONDS):
    """（サーバー, トークン）を返す。port=0 なら空いている番号。session_factory(emit) は会話を作る（テストで差し替える）。"""
    token = token or secrets.token_urlsafe(24)
    server = ThreadingHTTPServer((HOST, port), None)
    server.daemon_threads = True
    server.sessions = Sessions(session_factory or default_session_factory)
    server.RequestHandlerClass = make_handler(token, server.sessions, ping_seconds)
    return server, token
