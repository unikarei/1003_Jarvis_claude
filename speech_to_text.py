"""音声（wav 等・録音の配列）をローカル推論で文字起こしする。

Apple Silicon の Mac で mlx-whisper が使えれば GPU（MLX）で、使えなければ faster-whisper（CPU・int8）で認識する。
2026-11-05 Owner 承認「mlx-whisper を採用」: 同じ large-v3-turbo で 1 回の認識 2.7 → 0.5 秒（say の 8 文、一致 7/8 は同じ）。
"""

import os
import platform
import sys

from faster_whisper import WhisperModel

# 2026-11-04 Owner 指示「高精度に変更」: small → large-v3-turbo。Mac mini（CPU・int8・8 スレッド）で say の 8 文の
# 一致 4/8 → 7/8、1 回の認識 1.0 → 2.7 秒、起動時の読み込み約 36 秒。JARVIS_WHISPER_MODEL=small で戻せる
WHISPER_MODEL_SIZE = os.environ.get("JARVIS_WHISPER_MODEL", "large-v3-turbo")
WHISPER_THREADS = int(os.environ.get("JARVIS_WHISPER_THREADS", "8"))
# 認識の方式: auto（mlx-whisper が使えれば mlx、なければ faster）/ mlx / faster。JARVIS_STT_BACKEND で変える
STT_BACKEND = os.environ.get("JARVIS_STT_BACKEND", "auto")
MLX_REPO = os.environ.get("JARVIS_MLX_WHISPER_REPO", f"mlx-community/whisper-{WHISPER_MODEL_SIZE}")
# 認識のヒントにする語（固有名詞・Jarvis の言い回し）。2026-10-14 の計測で「ミンパク→民泊」「大洗い→大洗」「物点→物件」
# 「終点→出典」「なれじか→ナレッジ化」が直った。JARVIS_VOICE_VOCAB で差し替えられる
VOCABULARY = os.environ.get("JARVIS_VOICE_VOCAB",
                            "大洗、民泊、物件、補助金、空調、出典、ナレッジ化、テーマ登録、システムの状態、ジャービス、"
                            "ピアノ教室、タービン、改善候補、フラッタ、Obsidian、ナレッジ、データソース、件数。")
SAMPLE_RATE = 16000

_model: WhisperModel | None = None
_backend: str | None = None


def _get_model() -> WhisperModel:
    global _model
    if _model is None:
        _model = WhisperModel(WHISPER_MODEL_SIZE, device="cpu", compute_type="int8", cpu_threads=WHISPER_THREADS)
    return _model


def backend() -> str:
    """使う認識の方式（"mlx" / "faster"）。auto は Apple Silicon の Mac で mlx_whisper を import できれば mlx。"""
    global _backend
    if _backend is None:
        choice = STT_BACKEND if STT_BACKEND in ("mlx", "faster") else "auto"
        if choice == "auto":
            choice = "mlx" if sys.platform == "darwin" and platform.machine() == "arm64" and _mlx() is not None \
                else "faster"
        _backend = choice
    return _backend


def _mlx():
    try:
        import mlx_whisper
    except Exception:  # noqa: BLE001  入っていない・GPU が使えない環境では faster-whisper にする
        return None
    return mlx_whisper


def _speech_only(audio):
    """声のある区間だけをつないだ配列（なければ None）。faster-whisper の vad_filter と同じ Silero VAD を使う
    （mlx-whisper は雑音だけの音を「ご視聴ありがとうございました」と書き起こし、no_speech_prob でも見分けられない）。"""
    from faster_whisper.vad import collect_chunks, get_speech_timestamps
    stamps = get_speech_timestamps(audio, sampling_rate=SAMPLE_RATE)
    if not stamps:
        return None
    import numpy as np
    chunks, _ = collect_chunks(audio, stamps, sampling_rate=SAMPLE_RATE)
    return np.concatenate(chunks)


def _mlx_transcribe(audio, language: str) -> str:
    result = _mlx().transcribe(audio, path_or_hf_repo=MLX_REPO, language=language, initial_prompt=VOCABULARY or None,
                               condition_on_previous_text=False)
    return (result.get("text") or "").strip()


def transcribe_file(audio_path: str, language: str = "ja") -> str:
    """音声ファイルを文字起こしし、テキストを返す。"""
    if backend() == "mlx":
        from faster_whisper.audio import decode_audio
        return transcribe_array(decode_audio(audio_path, sampling_rate=SAMPLE_RATE), language)
    model = _get_model()
    segments, _info = model.transcribe(audio_path, language=language)
    return "".join(segment.text for segment in segments).strip()


def load_model() -> None:
    """モデルを先に読み込む（初回は Hugging Face からダウンロードされる。声での会話の起動時に呼ぶ）。"""
    if backend() == "mlx":
        import numpy as np
        _mlx_transcribe(np.zeros(SAMPLE_RATE, dtype=np.float32), "ja")   # 読み込みと GPU の準備
        return
    _get_model()


def transcribe_array(audio, language: str = "ja") -> str:
    """録音（16 kHz・モノラル・float32 の numpy 配列）を文字起こしする。録音はファイルに残さない（T-1214）。
    faster-whisper ではヒントの語を initial_prompt ではなく hotwords で渡す（雑音のある計測で誤りが 16.1% → 11.4%、
    T-1404）。mlx-whisper には hotwords がないため initial_prompt で渡す（say の 8 文で一致 6/8 → 7/8）。"""
    if backend() == "mlx":
        speech = _speech_only(audio)
        return "" if speech is None else _mlx_transcribe(speech, language)
    segments, _info = _get_model().transcribe(audio, language=language, vad_filter=True,
                                              hotwords=VOCABULARY or None)
    return "".join(segment.text for segment in segments).strip()

if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("使い方: python speech_to_text.py <音声ファイルパス>")
        sys.exit(1)

    text = transcribe_file(sys.argv[1])
    print(text)
