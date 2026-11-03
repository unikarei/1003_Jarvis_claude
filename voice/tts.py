"""読み上げの音声エンジンと設定（声の会話、../docs/decisions.md ADR-0029、T-1408）。

エンジンを選べるようにし、どれも「1 文 → 16 kHz・モノラル・float32 の音声」を返す同じ形にする（割り込みの
voice.duplex.DuplexSpeaker の合成に差し込む）。既定はローカル・追加費用なしの macOS の say。
- say: macOS の標準（今の既定。Kyoko など）。
- voicevox / aivisspeech: ローカルの HTTP エンジン（同じ API。起動していれば使える。導入は Owner の承認の後）。
- elevenlabs / openai: 有料の API。Owner の承認まで接続しない（JARVIS_TTS_ALLOW_PAID=1 でも、この版では合成しない）。
設定は data/voice/settings.json（JARVIS_VOICE_SETTINGS で変更可）。合成した音声はファイルに残さない。
"""

import io
import json
import os
import re
import subprocess
import tempfile
import threading
import time
import urllib.error
import urllib.request
from dataclasses import asdict, dataclass, field
from pathlib import Path

import numpy as np

import config

SAMPLE_RATE = 16000
SAY_BASE_RATE = 180            # say の話速（1 分あたりの語）の基準。設定の話速 1.0 に当たる
SETTINGS_PATH = Path(os.environ.get("JARVIS_VOICE_SETTINGS", config.BASE_DIR / "data" / "voice" / "settings.json"))
HTTP_TIMEOUT = 10.0


class TTSError(Exception):
    """合成できない（エンジンが止まっている・話者がない・有料で未承認など）。"""


@dataclass
class VoiceSettings:
    engine: str = "say"
    speaker: str = "Kyoko"         # say は声の名前、voicevox・aivisspeech はスタイルの ID（文字列）
    rate: float = 1.0              # 話速（1.0 が標準。0.5〜2.0）
    intonation: float = 1.0        # 抑揚（voicevox・aivisspeech。1.0 が標準。0〜2.0）
    pitch: float = 0.0             # 声の高さ（voicevox・aivisspeech。-0.15〜0.15）

    def clamp(self) -> "VoiceSettings":
        self.rate = min(2.0, max(0.5, float(self.rate)))
        self.intonation = min(2.0, max(0.0, float(self.intonation)))
        self.pitch = min(0.15, max(-0.15, float(self.pitch)))
        self.speaker = str(self.speaker)
        return self


def load_settings(path: Path | None = None) -> VoiceSettings:
    path = path or SETTINGS_PATH
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        known = {k: v for k, v in data.items() if k in VoiceSettings.__dataclass_fields__}
        if not isinstance(known.get("engine", ""), str):
            return VoiceSettings()
        settings = VoiceSettings(**known).clamp()
    except (OSError, ValueError, TypeError, AttributeError):
        return VoiceSettings()
    if settings.engine not in ENGINES or ENGINES[settings.engine].paid:
        return VoiceSettings()                    # 知らない・有料のエンジンは使わない（say・Kyoko に戻す）
    return settings


def save_settings(settings: VoiceSettings, path: Path | None = None) -> None:
    """一時ファイルに書いてから置き換える。"""
    path = path or SETTINGS_PATH
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(asdict(settings.clamp()), ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    tmp.replace(path)


def resample(audio: np.ndarray, rate: int) -> np.ndarray:
    """16 kHz に変換する（線形補間。読み上げの音声なので十分）。"""
    audio = audio if audio.ndim == 1 else audio[:, 0]
    if rate == SAMPLE_RATE or not len(audio):
        return audio.astype(np.float32)
    n = int(round(len(audio) * SAMPLE_RATE / rate))
    x = np.linspace(0, len(audio) - 1, n)
    return np.interp(x, np.arange(len(audio)), audio).astype(np.float32)


# ---------------------------------------------------------------- エンジン

@dataclass
class Engine:
    name: str
    label: str
    local: bool = True
    paid: bool = False
    note: str = ""

    def available(self) -> tuple[bool, str]:
        return True, ""

    def speakers(self) -> list[dict]:
        """[{"id", "name", "styles": [{"id", "name"}]}]。スタイルのないエンジンは id と name だけ。"""
        return []

    def synthesize(self, text: str, settings: VoiceSettings, cancel: threading.Event | None = None) -> np.ndarray:
        raise TTSError(f"{self.label} では合成できません")


class SayEngine(Engine):
    def __init__(self):
        super().__init__("say", "macOS の say", note="Mac に標準で入っている声。ローカル・無料")

    def available(self) -> tuple[bool, str]:
        return (True, "") if os.path.exists("/usr/bin/say") else (False, "say がありません（macOS 以外）")

    def speakers(self) -> list[dict]:
        try:
            out = subprocess.run(["say", "-v", "?"], capture_output=True, text=True, timeout=10).stdout
        except (OSError, subprocess.SubprocessError):
            return []
        voices = []
        for line in out.splitlines():
            m = re.match(r"^(.+?)\s{2,}(ja_JP)\s+#", line)
            if m:
                voices.append({"id": m.group(1).strip(), "name": m.group(1).strip(), "styles": []})
        return voices

    def synthesize(self, text, settings, cancel=None):
        import soundfile as sf

        with tempfile.TemporaryDirectory(prefix="jarvis-tts-") as tmp:
            path = os.path.join(tmp, "s.wav")
            cmd = ["say", "-v", settings.speaker or "Kyoko", "-r", str(int(SAY_BASE_RATE * settings.rate)),
                   "-o", path, "--file-format=WAVE", f"--data-format=LEF32@{SAMPLE_RATE}", text]
            proc = subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            while proc.poll() is None:
                if cancel is not None and cancel.is_set():
                    proc.terminate()
                    proc.wait()
                    raise TTSError("取り消されました")
                time.sleep(0.01)
            if proc.returncode != 0 or not os.path.exists(path):
                raise TTSError(f"say が失敗しました（声 {settings.speaker}）")
            audio, rate = sf.read(path, dtype="float32")
        return resample(audio, rate)


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """転送（リダイレクト）を追わない（ローカルのエンジンが外へ転送しても、文を外へ送らない）。"""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise urllib.error.HTTPError(req.full_url, code, "転送は追いません", headers, fp)


# プロキシを使わない（環境変数の HTTP_PROXY があっても、ローカルのエンジンへの文を外のプロキシへ送らない）
_OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}), _NoRedirect)


def _cancellable(fn, cancel: threading.Event | None, timeout: float):
    """fn を別のスレッドで動かし、取り消されたらすぐ TTSError で戻る（通信は裏で終わるまで続くが、結果は使わない）。
    割り込みの後に、次の発言の合成が前の通信を待たないように。"""
    result: dict = {}

    def run():
        try:
            result["value"] = fn()
        except BaseException as e:  # 呼び出し側で扱う
            result["error"] = e
    t = threading.Thread(target=run, daemon=True)
    t.start()
    deadline = time.monotonic() + timeout
    while t.is_alive():
        if cancel is not None and cancel.is_set():
            raise TTSError("取り消されました")
        if time.monotonic() > deadline:
            raise TTSError("時間切れ")
        t.join(0.02)
    if "error" in result:
        raise result["error"]
    return result.get("value")


class VoicevoxEngine(Engine):
    """VOICEVOX ENGINE と同じ HTTP API（AivisSpeech Engine も同じ形）。"""

    def __init__(self, name: str, label: str, base_url: str, note: str):
        super().__init__(name, label, note=note)
        self.base_url = base_url.rstrip("/")
        from urllib.parse import urlparse
        host = urlparse(self.base_url).hostname or ""
        # ローカル・無料のエンジンに限る（環境変数で外のサーバーを指しても、文を外へ送らない）
        self.local_ok = host in ("127.0.0.1", "localhost", "::1")

    def _get(self, path: str, timeout: float = HTTP_TIMEOUT):
        with _OPENER.open(self.base_url + path, timeout=timeout) as r:
            return json.loads(r.read())

    def _post(self, path: str, body: bytes | None = None, timeout: float = HTTP_TIMEOUT) -> bytes:
        req = urllib.request.Request(self.base_url + path, data=body or b"", method="POST",
                                     headers={"Content-Type": "application/json"})
        with _OPENER.open(req, timeout=timeout) as r:
            return r.read()

    def preload(self, style: str) -> bool:
        """話者のモデルを先に読み込ませる（最初の文の合成を速くする）。失敗しても False を返すだけ。"""
        if not self.local_ok or not str(style).isdigit():
            return False
        from urllib.parse import urlencode
        try:
            self._post("/initialize_speaker?" + urlencode({"speaker": style, "skip_reinit": "true"}), timeout=60)
            return True
        except (OSError, ValueError):
            return False

    def available(self) -> tuple[bool, str]:
        if not self.local_ok:
            return False, f"{self.label} の接続先が Mac の中（127.0.0.1）ではありません（{self.base_url}）"
        try:
            self._get("/version", timeout=1.0)
            return True, ""
        except (OSError, ValueError):
            return False, f"{self.label} が起動していません（{self.base_url}）"

    def speakers(self) -> list[dict]:
        try:
            data = self._get("/speakers")
        except (OSError, ValueError):
            return []
        if not isinstance(data, list):            # 応答の形が違えば話者なし（画面ではエンジンを使えないと示す）
            return []
        out = []
        for s in data:
            if not isinstance(s, dict) or not isinstance(s.get("styles", []), list):
                continue
            out.append({"id": s.get("speaker_uuid") or s.get("name"), "name": s.get("name"),
                        "styles": [{"id": str(st.get("id")), "name": st.get("name")}
                                   for st in s.get("styles", []) if isinstance(st, dict)]})
        return out

    def synthesize(self, text, settings, cancel=None):
        import soundfile as sf
        from urllib.parse import urlencode

        if not self.local_ok:
            raise TTSError(f"{self.label} の接続先が Mac の中ではありません")
        style = settings.speaker
        if not str(style).isdigit():
            raise TTSError(f"{self.label} の話者はスタイルの番号で指定してください（今の設定: {style}）")
        try:
            query = json.loads(_cancellable(
                lambda: self._post("/audio_query?" + urlencode({"text": text, "speaker": style})), cancel,
                HTTP_TIMEOUT))
            if not isinstance(query, dict):
                raise TTSError(f"{self.label} の応答の形が違います")
            query.update(speedScale=settings.rate, intonationScale=settings.intonation, pitchScale=settings.pitch)
            wav = _cancellable(lambda: self._post("/synthesis?" + urlencode({"speaker": style}),
                                                  json.dumps(query).encode()), cancel, HTTP_TIMEOUT)
        except (OSError, ValueError) as e:
            raise TTSError(f"{self.label} で合成できません: {e}") from None
        if cancel is not None and cancel.is_set():
            raise TTSError("取り消されました")
        try:
            audio, rate = sf.read(io.BytesIO(wav), dtype="float32")
        except Exception as e:                    # 壊れた音声も「合成できない」にして say に切り替えさせる
            raise TTSError(f"{self.label} の音声を読めません: {e}") from None
        return resample(audio, rate)


class PaidEngine(Engine):
    """有料の API。Owner の承認まで接続しない（ADR-0029）。比較の表と設定の画面に並べるだけ。"""

    def available(self) -> tuple[bool, str]:
        return False, "有料の API のため、Owner の承認まで使いません（接続・課金なし）"


ENGINES: dict[str, Engine] = {e.name: e for e in (
    SayEngine(),
    VoicevoxEngine("aivisspeech", "AivisSpeech", os.environ.get("JARVIS_AIVISSPEECH_URL", "http://127.0.0.1:10101"),
                   "ローカル・無料。自然な抑揚の日本語（AivisSpeech Engine を起動すると使える。導入は Owner の承認の後）"),
    VoicevoxEngine("voicevox", "VOICEVOX", os.environ.get("JARVIS_VOICEVOX_URL", "http://127.0.0.1:50021"),
                   "ローカル・無料。話者が多い（VOICEVOX ENGINE を起動すると使える。導入は Owner の承認の後）"),
    PaidEngine("elevenlabs", "ElevenLabs", local=False, paid=True, note="クラウド・従量課金（未接続）"),
    PaidEngine("openai", "OpenAI TTS", local=False, paid=True, note="クラウド・従量課金（未接続）"),
)}


def engine(name: str) -> Engine:
    if name not in ENGINES:
        raise TTSError(f"知らないエンジンです: {name}")
    return ENGINES[name]


RECOVER_INTERVAL_SECONDS = 30   # エンジンの起動し直しを試す間隔の下限（止まり続けるときに毎文待たない）


def make_synth(settings: VoiceSettings, fallback: VoiceSettings | None = None, notify=print, recover=None):
    """DuplexSpeaker に渡す合成の関数（文, 取り消し）→ 音声。選んだエンジンが使えなければ say（Kyoko）にする。
    会話の途中でエンジンが止まったときも、その文は say（Kyoko）で読む（会話を止めない）。
    recover（エンジンを起動し直して使えれば True を返す関数）を渡すと、止まったときに一度起動し直して、選んだ音声に戻す
    （30 秒に 1 回まで。2026-10-18、会話の途中で声が Kyoko に変わったまま戻らなかった）。"""
    fallback = fallback or VoiceSettings()
    chosen = engine(settings.engine)
    ok, reason = chosen.available()
    if not ok and recover is not None and settings != fallback:
        ok = bool(recover())
    if not ok:
        notify(f"[音声] {chosen.label} は使えません（{reason}）。macOS の say（Kyoko）で読み上げます。")
        settings = fallback
        chosen = engine(settings.engine)
    warned = {"done": False}
    tried = {"at": None}

    def try_recover(sentence: str, cancel: threading.Event):
        """エンジンを起動し直して、同じ文を選んだ音声で読む。起動し直せない・間隔が短いときは None。"""
        now = time.monotonic()
        if recover is None or (tried["at"] is not None and now - tried["at"] < RECOVER_INTERVAL_SECONDS):
            return None
        tried["at"] = now
        notify(f"[音声] {chosen.label} が止まりました。起動し直します…")
        try:
            if not recover():
                return None
            audio = chosen.synthesize(sentence, settings, cancel)
        except TTSError:
            return None
        warned["done"] = False                    # 戻ったので、次に止まったときもまた知らせる
        notify(f"[音声] {chosen.label} に戻りました。")
        return audio

    def synth(sentence: str, cancel: threading.Event):
        try:
            return chosen.synthesize(sentence, settings, cancel)
        except TTSError as e:
            if (cancel is not None and cancel.is_set()) or settings == fallback:
                return None                       # 取り消し、または say 自体の失敗（その文は読まない）
            audio = try_recover(sentence, cancel)
            if audio is not None:
                return audio
            if not warned["done"]:
                warned["done"] = True
                notify(f"[音声] {chosen.label} で合成できません（{e}）。say（Kyoko）で読み上げます。")
            try:
                return engine(fallback.engine).synthesize(sentence, fallback, cancel)
            except TTSError:
                return None
    return synth


# ---------------------------------------------------------------- 文の区切り

LONG_SENTENCE = 60             # これより長い文は、読点で区切って先に読み始める（最初の声を早く）
MIN_PHRASE = 12                # 読点で区切るときの、前の部分の最短の長さ（短すぎる切れ目を作らない）
FIRST_PHRASE = 24              # 発言の最初の文は、これより長ければ読点で区切って先に読む（最初の声を早く、2026-11-03 Owner 指示）


def split_long(text: str, limit: int = LONG_SENTENCE) -> tuple[str, str]:
    """句点の前で limit 字（既定 LONG_SENTENCE）より長くなった文を、読点（、）の切れ目で前後に分ける。
    分けられなければ ("", text)。語や数字の途中では切らない（読点の直後だけ）。"""
    if len(text) <= limit:
        return "", text
    # 読点の直後だけで切る。半角・全角のカンマは数字（1,000・１，０００）の間なら使わない
    found = [m.end() for m in re.finditer(r"、|(?<![0-9０-９])，|，(?![0-9０-９])", text) if m.end() < len(text)]
    if not found:
        return "", text
    preferred = [c for c in found if MIN_PHRASE <= c <= limit]
    later = [c for c in found if c > limit]
    # 12〜limit 字の読点（最後のもの）→ limit 字より後の最初の読点 → 短い前半句の読点、の順に選ぶ
    cut = preferred[-1] if preferred else (later[0] if later else found[-1])
    return text[:cut], text[cut:]
