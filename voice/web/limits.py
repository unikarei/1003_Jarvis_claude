"""ブラウザの声の会話の発話 1 回の長さの上限（上位 spec §3.4.9・browser-voice-spec BV-03、T-2901）。

ページに渡す最長・サーバーが受け付ける WAV の長さ・本文の大きさを同じ値から決める。端末の声の会話
（voice/record.py の 20 秒）とは別。標準ライブラリだけ（server.py が起動時に読み込む）。
"""

import os

DEFAULT_MAX_SECONDS = 120.0   # 2026-11-15 Owner 指示で 20 秒から延ばした
MIN_MAX_SECONDS, MAX_MAX_SECONDS = 10.0, 300.0
PREROLL_MARGIN = 0.5          # 話し始めの前 0.3 秒＋余裕
MAX_RATE = 96000              # session.MAX_RATE と同じ
WAV_HEADER_MARGIN = 1024


def max_seconds() -> float:
    """発話 1 回の最長（秒）。JARVIS_VOICE_WEB_MAX_SECONDS（10〜300）。範囲外・不正なら既定。"""
    try:
        value = float(os.environ.get("JARVIS_VOICE_WEB_MAX_SECONDS", DEFAULT_MAX_SECONDS))
    except ValueError:
        return DEFAULT_MAX_SECONDS
    return value if MIN_MAX_SECONDS <= value <= MAX_MAX_SECONDS else DEFAULT_MAX_SECONDS


def max_audio_seconds() -> float:
    """サーバーが受け付ける WAV の長さの上限（秒）。"""
    return max_seconds() + PREROLL_MARGIN


def max_body() -> int:
    """発話 1 回分の WAV の本文の上限（バイト）。最大のサンプルレート・16 bit・モノラルで最長が収まる大きさ。"""
    return int(max_audio_seconds() * MAX_RATE * 2) + WAV_HEADER_MARGIN
