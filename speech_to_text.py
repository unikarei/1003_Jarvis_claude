"""faster-whisperで音声ファイル（wav等）をローカル推論で文字起こしする。"""

import os

from faster_whisper import WhisperModel

WHISPER_MODEL_SIZE = "small"
# 認識のヒントにする語（固有名詞・Jarvis の言い回し）。2026-10-14 の計測で「ミンパク→民泊」「大洗い→大洗」「物点→物件」
# 「終点→出典」「なれじか→ナレッジ化」が直った。JARVIS_VOICE_VOCAB で差し替えられる
VOCABULARY = os.environ.get("JARVIS_VOICE_VOCAB",
                            "大洗、民泊、物件、補助金、空調、出典、ナレッジ化、テーマ登録、システムの状態、ジャービス、"
                            "ピアノ教室、タービン、改善候補。")

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


def load_model() -> None:
    """モデルを先に読み込む（初回は Hugging Face からダウンロードされる。声での会話の起動時に呼ぶ）。"""
    _get_model()


def transcribe_array(audio, language: str = "ja") -> str:
    """録音（16 kHz・モノラル・float32 の numpy 配列）を文字起こしする。録音はファイルに残さない（T-1214）。"""
    segments, _info = _get_model().transcribe(audio, language=language, vad_filter=True,
                                              initial_prompt=VOCABULARY or None)
    return "".join(segment.text for segment in segments).strip()

if __name__ == "__main__":
    import sys

    if len(sys.argv) < 2:
        print("使い方: python speech_to_text.py <音声ファイルパス>")
        sys.exit(1)

    text = transcribe_file(sys.argv[1])
    print(text)

