"""言いかけの文で答え始めない（上位 spec §3.4.9、T-3102）。

ブラウザの声の会話は 0.8 秒の無音で発話を区切る。認識した文の終わりが言いかけの形なら、答える前に少し待ち、
続きの発話とつなげる。ここは判定と設定だけ（待つ・つなげる処理は session.py）。標準ライブラリだけ。
"""

import os
import re

DEFAULT_CONTINUE_SECONDS = 1.5
MAX_CONTINUE_SECONDS = 5.0
EXTEND_SECONDS = 2.0          # 待っている間に発話の確認が来たら、そこからさらに待つ長さ

_TRAIL = re.compile(r"[\s。、，．,!！」』）)]+$")
_ELLIPSIS = re.compile(r"(?:…|\.{2,}|・{2,})$")
# 接続の語・格の助詞・つなぎの語。「て」「で」（依頼）、「は」「も」（挨拶）、「の」「と」（問い・副詞）は入れない
_CONTINUES = re.compile(r"(?:から|けど|けれど|けども|ので|のに|ながら|たり|とか|って|ば|たら|を|に|が|"
                        r"あの|えっと|えーと|まあ|その)$")


def continue_seconds() -> float:
    """言いかけのときに待つ長さ（秒）。JARVIS_VOICE_WEB_CONTINUE_SECONDS（0〜5、0 で無効）。不正なら既定。"""
    try:
        value = float(os.environ.get("JARVIS_VOICE_WEB_CONTINUE_SECONDS", DEFAULT_CONTINUE_SECONDS))
    except ValueError:
        return DEFAULT_CONTINUE_SECONDS
    return value if 0 <= value <= MAX_CONTINUE_SECONDS else DEFAULT_CONTINUE_SECONDS


def continues(text: str) -> bool:
    """文の終わりが言いかけの形か。「？」で終わる文は問いなので待たない。"""
    t = (text or "").strip()
    if not t or re.search(r"[?？]\s*$", t):
        return False
    if _ELLIPSIS.search(t):
        return True
    return bool(_CONTINUES.search(_TRAIL.sub("", t)))
