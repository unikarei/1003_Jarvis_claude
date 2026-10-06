"""faster-whisperで音声ファイル（wav等）をローカル推論で文字起こしする。"""

from faster_whisper import WhisperModel

WHISPER_MODEL_SIZE = "small"

_model: WhisperModel | None = None


def _get_model() -> WhisperModel:
    global _model
    if _model is None:
        _model = WhisperModel(WHISPER_MODEL_SIZE, device="cpu", compute_type="int8")
    return _model


def transcribe_file(audio_path: str, language: str = "ja") -> str:
    """音声ファイルを文字起こしし、テキストを返す。"""
    model = _get_model()
    segments, _info = model.transcribe(audio_path, language=language)
    return "".join(segment.text for segment in segments).strip()


if __name__ == "__main__":
    import sys

    if len(sys.argv) < 2:
        print("使い方: python speech_to_text.py <音声ファイルパス>")
        sys.exit(1)

    text = transcribe_file(sys.argv[1])
    print(text)
