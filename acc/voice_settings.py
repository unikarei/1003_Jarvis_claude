"""声の会話の音声の設定と試聴の画面（ACC、../docs/decisions.md ADR-0029、T-1408）。

音声エンジン・話者・話速・抑揚を選び、同じ日本語の文を各音声で試聴して比べる。選んだ設定は
data/voice/settings.json に保存し、次に起動する声の会話（voice_chat.py）が使う。
127.0.0.1 に固定し、起動のたびに作るトークンで認証する（操作画面 acc/dev_console.py と同じ）。有料の API は使わない。

使い方: python -m acc.voice_settings [--port 8772]
"""

import argparse
import hmac
import io
import json
import secrets
import sys
import threading
import time
from http.cookies import SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

import numpy as np

from voice import tts

HOST = "127.0.0.1"
DEFAULT_PORT = 8772
COOKIE = "jarvis_voice_settings"
CSRF_HEADER = "X-Jarvis-Console"
MAX_TEXT = 300
SAMPLE_TEXT = ("はい、承知しました。大洗の民泊の物件について、補助金の申請期限と、タービンのフラッタ解析の"
               "進み具合をご報告します。Obsidianのナレッジには、関連するメモが三件あります。")


def wav_bytes(audio: np.ndarray) -> bytes:
    import soundfile as sf
    buf = io.BytesIO()
    sf.write(buf, audio, tts.SAMPLE_RATE, format="WAV", subtype="PCM_16")
    return buf.getvalue()


def engines_info() -> list[dict]:
    out = []
    for e in tts.ENGINES.values():
        ok, reason = e.available()
        speakers = e.speakers() if ok else []
        if ok and not speakers:                   # 起動していても話者を読めなければ選べない
            ok, reason = False, f"{e.label} の話者の一覧を読めません"
        out.append({"name": e.name, "label": e.label, "local": e.local, "paid": e.paid, "note": e.note,
                    "available": ok, "reason": reason, "speakers": speakers})
    return out


def preview(body: dict) -> tuple[bytes, float]:
    """設定（エンジン・話者・話速・抑揚・高さ）と文から音声を作る。返り値は（WAV, 合成の秒数）。"""
    name = str(body.get("engine", "say"))
    e = tts.engine(name)
    if e.paid:
        raise tts.TTSError("有料の API は Owner の承認まで使いません")
    ok, reason = e.available()
    if not ok:
        raise tts.TTSError(reason)
    text = str(body.get("text") or SAMPLE_TEXT)[:MAX_TEXT]
    settings = tts.VoiceSettings(engine=name, speaker=str(body.get("speaker", "Kyoko")),
                                 rate=body.get("rate", 1.0), intonation=body.get("intonation", 1.0),
                                 pitch=body.get("pitch", 0.0)).clamp()
    started = time.monotonic()
    audio = e.synthesize(text, settings)
    return wav_bytes(audio), round(time.monotonic() - started, 2)


def make_handler(token: str, port: int):
    allowed_hosts = {f"127.0.0.1:{port}", f"localhost:{port}"}
    allowed_origins = {f"http://{h}" for h in allowed_hosts}
    lock = threading.Lock()                       # 合成は 1 つずつ（say を重ねて起動しない）

    class Handler(BaseHTTPRequestHandler):
        server_version = "JarvisVoiceSettings"

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
                             "style-src 'unsafe-inline'; media-src 'self' blob:; frame-ancestors 'none'")
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
                return self._send(200, PAGE.replace("__SAMPLE__", SAMPLE_TEXT).encode("utf-8"),
                                  "text/html; charset=utf-8")
            if url.path == "/api/engines":
                return self._json(200, {"engines": engines_info()})
            if url.path == "/api/settings":
                from dataclasses import asdict
                return self._json(200, asdict(tts.load_settings()))
            self._json(404, {"error": "ありません"})

        def do_POST(self):
            if not self._guard(write=True):
                return
            try:
                length = int(self.headers.get("Content-Length") or 0)
            except ValueError:
                return self._json(400, {"error": "Content-Length が違います"})
            if not 0 <= length <= 10_000:              # 負の値・大きすぎる要求は読まない
                return self._json(400, {"error": "要求が大きすぎるか、長さが違います"})
            try:
                body = json.loads(self.rfile.read(length) or b"{}")
            except ValueError:
                return self._json(400, {"error": "JSON ではありません"})
            if not isinstance(body, dict):
                return self._json(400, {"error": "JSON のオブジェクトではありません"})
            if self.path == "/api/preview":
                try:
                    with lock:
                        wav, seconds = preview(body)
                except (tts.TTSError, ValueError, TypeError) as e:
                    return self._json(400, {"error": str(e)})
                return self._send(200, wav, "audio/wav", {"X-Synth-Seconds": str(seconds)})
            if self.path == "/api/settings":
                name = str(body.get("engine", "say"))
                if name not in tts.ENGINES:
                    return self._json(400, {"error": "知らないエンジンです"})
                if tts.ENGINES[name].paid:
                    return self._json(400, {"error": "有料の API は Owner の承認まで選べません"})
                try:
                    settings = tts.VoiceSettings(engine=name, speaker=str(body.get("speaker", "Kyoko")),
                                                 rate=body.get("rate", 1.0), intonation=body.get("intonation", 1.0),
                                                 pitch=body.get("pitch", 0.0)).clamp()
                except (ValueError, TypeError):
                    return self._json(400, {"error": "値が違います"})
                tts.save_settings(settings)
                return self._json(200, {"saved": True})
            self._json(404, {"error": "ありません"})

    return Handler


def make_server(port: int = DEFAULT_PORT, token: str | None = None):
    token = token or secrets.token_urlsafe(32)
    server = ThreadingHTTPServer((HOST, port), None)
    server.RequestHandlerClass = make_handler(token, server.server_address[1])
    return server, token


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="声の会話の音声の設定と試聴（127.0.0.1 だけ）")
    parser.add_argument("--port", type=int, default=DEFAULT_PORT)
    args = parser.parse_args(argv)
    server, token = make_server(args.port)
    print(f"音声の設定と試聴: http://{HOST}:{server.server_address[1]}/?token={token}", flush=True)
    print("（この URL は他人に渡さない。Ctrl-C で終了。設定は次に起動する声の会話から使われます）", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
    return 0


PAGE = """<!doctype html>
<html lang="ja"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>音声の設定</title>
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
.muted { color:var(--muted); font-size:13px; }
label { display:inline-block; margin:6px 16px 6px 0; font-size:14px; }
select, input[type=range] { font:inherit; max-width:100%; }
textarea { width:100%; font:inherit; min-height:70px; }
button { font:inherit; padding:4px 12px; border-radius:6px; border:1px solid var(--line); background:var(--card); color:var(--fg); cursor:pointer; }
table { border-collapse:collapse; width:100%; font-size:13px; }
td, th { border-bottom:1px solid var(--line); padding:6px 8px; text-align:left; vertical-align:top; }
.ok { color:var(--ok); } .ng { color:var(--ng); }
@media (max-width:600px) { main, header { padding-left:16px; padding-right:16px; } }
</style></head><body>
<header><h1>声の会話の音声</h1>
<div class="muted">音声エンジン・話者・話速・抑揚を選んで試聴し、「この音声を使う」で保存する。次に起動する声の会話から使われる。有料の API は使わない。</div></header>
<main>
<section><h2>試聴する文</h2><textarea id="text">__SAMPLE__</textarea></section>
<section><h2>音声を選ぶ</h2>
<label>音声エンジン <select id="engine"></select></label>
<label>話者・スタイル <select id="speaker"></select></label><br>
<label>話速 <input type="range" id="rate" min="0.5" max="2" step="0.05" value="1"> <span id="rate_v">1.00</span></label>
<label>抑揚 <input type="range" id="intonation" min="0" max="2" step="0.05" value="1"> <span id="intonation_v">1.00</span></label>
<label>高さ <input type="range" id="pitch" min="-0.15" max="0.15" step="0.01" value="0"> <span id="pitch_v">0.00</span></label>
<div><button id="play">試聴</button> <button id="save">この音声を使う</button> <span id="msg" class="muted"></span></div>
<div class="muted" id="note"></div>
<audio id="audio" controls></audio></section>
<section><h2>エンジンの比較（同じ文を各音声で）</h2>
<table><thead><tr><th>エンジン</th><th>状態</th><th>話者</th><th>合成の秒数</th><th></th></tr></thead><tbody id="compare"></tbody></table>
<p class="muted">抑揚・高さは VOICEVOX・AivisSpeech だけに効く。macOS の say は話速だけ。</p></section>
</main>
<script>
const H = {"Content-Type": "application/json", "X-Jarvis-Console": "1"};
let engines = [];
const esc = s => String(s ?? "").replace(/[&<>"']/g, c => ({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"}[c]));
const $ = id => document.getElementById(id);
function options(e) {
  if (!e.speakers.length) return "";
  if (e.speakers[0].styles && e.speakers[0].styles.length)
    return e.speakers.map(s => s.styles.map(st => `<option value="${esc(st.id)}">${esc(s.name)}（${esc(st.name)}）</option>`).join("")).join("");
  return e.speakers.map(s => `<option value="${esc(s.id)}">${esc(s.name)}</option>`).join("");
}
function current() {
  return {engine: $("engine").value, speaker: $("speaker").value, rate: +$("rate").value,
          intonation: +$("intonation").value, pitch: +$("pitch").value, text: $("text").value};
}
async function play(body, target) {
  $("msg").textContent = "合成しています…";
  const r = await fetch("/api/preview", {method: "POST", headers: H, body: JSON.stringify(body)});
  if (!r.ok) { $("msg").textContent = (await r.json()).error; return null; }
  const secs = r.headers.get("X-Synth-Seconds");
  $("audio").src = URL.createObjectURL(await r.blob()); $("audio").play();
  $("msg").textContent = `合成 ${secs} 秒`;
  if (target) target.textContent = secs + " 秒";
  return secs;
}
function onEngine() {
  const e = engines.find(x => x.name === $("engine").value);
  $("speaker").innerHTML = options(e);
  $("note").textContent = e.available ? e.note : e.reason;
}
async function load() {
  engines = (await (await fetch("/api/engines")).json()).engines;
  $("engine").innerHTML = engines.map(e => `<option value="${esc(e.name)}" ${e.available ? "" : "disabled"}>${esc(e.label)}${e.available ? "" : "（使えない）"}</option>`).join("");
  const s = await (await fetch("/api/settings")).json();
  $("engine").value = engines.find(e => e.name === s.engine && e.available) ? s.engine : "say";
  onEngine();
  if ([...$("speaker").options].some(o => o.value === s.speaker)) $("speaker").value = s.speaker;
  for (const k of ["rate", "intonation", "pitch"]) { $(k).value = s[k]; $(k + "_v").textContent = (+s[k]).toFixed(2); }
  $("compare").innerHTML = engines.map((e, i) => {
    const first = e.speakers[0] ? (e.speakers[0].styles?.[0]?.id ?? e.speakers[0].id) : "";
    const name = e.speakers[0] ? (e.speakers[0].name + (e.speakers[0].styles?.[0] ? "（" + e.speakers[0].styles[0].name + "）" : "")) : "-";
    return `<tr><td>${esc(e.label)}<div class="muted">${esc(e.note)}</div></td>
      <td class="${e.available ? "ok" : "ng"}">${e.available ? "使える" : esc(e.reason)}</td><td>${esc(name)}</td>
      <td id="t${i}">-</td><td>${e.available && first ? `<button data-i="${i}" data-sp="${esc(first)}">試聴</button>` : ""}</td></tr>`;
  }).join("");
}
$("engine").onchange = onEngine;
for (const k of ["rate", "intonation", "pitch"]) $(k).oninput = () => $(k + "_v").textContent = (+$(k).value).toFixed(2);
$("play").onclick = () => play(current());
$("save").onclick = async () => {
  const r = await fetch("/api/settings", {method: "POST", headers: H, body: JSON.stringify(current())});
  $("msg").textContent = r.ok ? "保存しました。次に起動する声の会話から使われます。" : (await r.json()).error;
};
$("compare").onclick = e => {
  const b = e.target.closest("button[data-i]"); if (!b) return;
  const eng = engines[+b.dataset.i];
  play({engine: eng.name, speaker: b.dataset.sp, rate: 1, intonation: 1, pitch: 0, text: $("text").value}, $("t" + b.dataset.i));
};
load();
</script></body></html>
"""

if __name__ == "__main__":
    sys.exit(main())
