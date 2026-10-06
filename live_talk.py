"""実機マイクで録音→文字起こし→回答生成→VOICEVOXで音声合成→スピーカー再生、を接続するCLI（Windows実機検証用）。"""

import sys
import tempfile
from pathlib import Path

import sounddevice as sd
import soundfile as sf

from talk import talk

SAMPLE_RATE = 16000
DEFAULT_RECORD_SECONDS = 5


def record_audio(seconds: int, output_path: str) -> None:
    print(f"[録音開始] {seconds}秒間お話しください...")
    recording = sd.rec(int(seconds * SAMPLE_RATE), samplerate=SAMPLE_RATE, channels=1, dtype="int16")
    sd.wait()
    sf.write(output_path, recording, SAMPLE_RATE)
    print("[録音終了]")


def play_audio(path: str) -> None:
    data, samplerate = sf.read(path, dtype="int16")
    sd.play(data, samplerate)
    sd.wait()


def live_talk(record_seconds: int = DEFAULT_RECORD_SECONDS) -> str:
    with tempfile.TemporaryDirectory() as tmp_dir:
        input_path = str(Path(tmp_dir) / "input.wav")
        output_path = str(Path(tmp_dir) / "output.wav")

        record_audio(record_seconds, input_path)

        response_text = talk(input_path, output_path)

        print("[再生開始]")
        play_audio(output_path)
        print("[再生終了]")

        return response_text


if __name__ == "__main__":
    seconds = int(sys.argv[1]) if len(sys.argv) > 1 else DEFAULT_RECORD_SECONDS
    live_talk(seconds)
