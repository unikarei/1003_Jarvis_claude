"""音声対話CLI（ファイルベース検証用）。音声ファイル→文字起こし→回答生成→音声合成を接続する。

実機マイク入力・スピーカー出力への接続は本番環境（Mac mini）側で行う。
"""

import sys

from ask import answer
from speech_to_text import transcribe_file
from text_to_speech import synthesize


def talk(input_audio_path: str, output_audio_path: str) -> str:
    question = transcribe_file(input_audio_path)
    print(f"[文字起こし] {question}")

    response_text = answer(question)
    print(f"[回答] {response_text}")

    synthesize(response_text, output_audio_path)
    print(f"[音声出力] {output_audio_path}")

    return response_text


if __name__ == "__main__":
    if len(sys.argv) < 3:
        print("使い方: python talk.py <入力音声ファイル> <出力音声ファイル>")
        sys.exit(1)

    talk(sys.argv[1], sys.argv[2])
