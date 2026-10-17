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
        return VoiceSettings(**known).clamp()
    except (OSError, ValueError, TypeError):
        return VoiceSettings()


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


class VoicevoxEngine(Engine):
    """VOICEVOX ENGINE と同じ HTTP API（AivisSpeech Engine も同じ形）。"""

    def __init__(self, name: str, label: str, base_url: str, note: str):
        super().__init__(name, label, note=note)
        self.base_url = base_url.rstrip("/")

    def _get(self, path: str, timeout: float = HTTP_TIMEOUT):
        with urllib.request.urlopen(self.base_url + path, timeout=timeout) as r:
            return json.loads(r.read())

    def _post(self, path: str, body: bytes | None = None, timeout: float = HTTP_TIMEOUT) -> bytes:
        req = urllib.request.Request(self.base_url + path, data=body or b"", method="POST",
                                     headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.read()

    def available(self) -> tuple[bool, str]:
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
        return [{"id": s.get("speaker_uuid") or s.get("name"), "name": s.get("name"),
                 "styles": [{"id": str(st.get("id")), "name": st.get("name")} for st in s.get("styles", [])]}
                for s in data]

    def synthesize(self, text, settings, cancel=None):
        import soundfile as sf
        from urllib.parse import urlencode

        style = settings.speaker
        if not str(style).isdigit():
            raise TTSError(f"{self.label} の話者はスタイルの番号で指定してください（今の設定: {style}）")
        try:
            query = json.loads(self._post("/audio_query?" + urlencode({"text": text, "speaker": style})))
            if cancel is not None and cancel.is_set():
                raise TTSError("取り消されました")
            query.update(speedScale=settings.rate, intonationScale=settings.intonation, pitchScale=settings.pitch)
            wav = self._post("/synthesis?" + urlencode({"speaker": style}), json.dumps(query).encode())
        except (OSError, ValueError) as e:
            raise TTSError(f"{self.label} で合成できません: {e}") from None
        if cancel is not None and cancel.is_set():
            raise TTSError("取り消されました")
        audio, rate = sf.read(io.BytesIO(wav), dtype="float32")
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


def make_synth(settings: VoiceSettings, fallback: VoiceSettings | None = None, notify=print):
    """DuplexSpeaker に渡す合成の関数（文, 取り消し）→ 音声。選んだエンジンが使えなければ say（Kyoko）にする。
    会話の途中でエンジンが止まったときも、その文は say（Kyoko）で読む（会話を止めない）。"""
    fallback = fallback or VoiceSettings()
    chosen = engine(settings.engine)
    ok, reason = chosen.available()
    if not ok:
        notify(f"[音声] {chosen.label} は使えません（{reason}）。macOS の say（Kyoko）で読み上げます。")
        settings = fallback
        chosen = engine(settings.engine)
    warned = {"done": False}

    def synth(sentence: str, cancel: threading.Event):
        try:
            return chosen.synthesize(sentence, settings, cancel)
        except TTSError as e:
            if (cancel is not None and cancel.is_set()) or settings == fallback:
                return None                       # 取り消し、または say 自体の失敗（その文は読まない）
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


def split_long(text: str) -> tuple[str, str]:
    """句点の前で長くなった文を、読点（、）の切れ目で前後に分ける。分けられなければ ("", text)。
    語や数字の途中では切らない（読点の直後だけ）。"""
    if len(text) <= LONG_SENTENCE:
        return "", text
    commas = [m.end() for m in re.finditer(r"[、，]", text)   # 半角のカンマは数字（1,000）にあるので使わない
              if m.end() >= MIN_PHRASE]
    if not commas:
        return "", text
    within = [c for c in commas if c <= LONG_SENTENCE]
    cut = within[-1] if within else commas[0]                # 60 字までに読点がなければ、最初の読点で
    return text[:cut], text[cut:]
