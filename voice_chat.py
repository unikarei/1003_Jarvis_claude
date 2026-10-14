"""JARVIS と声で会話する CLI（選んで使うモード。文字の対話は chat.py。FR-014、../docs/spec.md §3.4.9）。

Enter で話し始め、無音で区切る。音声は Mac の中で文字にし（faster-whisper）、録音は保存しない。
答えは画面に逐次表示しながら文ごとに読み上げる（macOS の say）。Enter を押すと読み上げを止めて次を話せる。
書き込みや課金のあるスキルの確認は、キーボードの y だけで受け付ける。終了: q と Enter、または「終了」と話す。
"""

import os
import sys

import orchestrator
from ckc.models import Speaker as Who, Turn
from db import init_db
from run_task import record
from voice import record as mic
from voice.speak import Speaker, speech_summary

VOICE = os.environ.get("JARVIS_VOICE", "Kyoko")
QUIT_WORDS = ("終了", "おしまい", "さようなら")
STREAMED_SKILLS = ("knowledge-answer",)   # 答えを逐次表示しながら読み上げるスキル


def output_device() -> str | None:
    """読み上げの出力先。JARVIS_VOICE_OUTPUT、なければ既定の入力機器と同じ名前の機器（ヘッドセット）。"""
    if os.environ.get("JARVIS_VOICE_OUTPUT"):
        return os.environ["JARVIS_VOICE_OUTPUT"]
    try:
        import sounddevice as sd
        device = sd.query_devices(kind="input")
        return device["name"] if device.get("max_output_channels", 0) > 0 else None
    except Exception:
        return None


def main(listen=None, transcribe=None, speaker: Speaker | None = None) -> int:
    """listen・transcribe・speaker はテストで差し替える。"""
    if listen is None:
        if not mic.has_input_device():
            print("マイクが見つかりません。ヘッドセットなどをつないでから起動してください（文字の対話は chat.py）。")
            return 1
        print("音声認識のモデルを読み込んでいます（初回はダウンロードに数分かかります）…")
        from speech_to_text import load_model, transcribe_array
        load_model()
        listen, transcribe = mic.record_utterance, transcribe_array
    speaker = speaker or Speaker(voice=VOICE, device=output_device())
    init_db()

    def stream(chunk: str) -> None:
        if not state["started"]:
            state["started"] = True
            print("\nJARVIS > ", end="", flush=True)
        print(chunk, end="", flush=True)
        speaker.feed(chunk)

    def ask(prompt: str) -> str:
        speaker.stop()
        speaker.say("書き込みがあります。キーボードで確認してください。")
        return input(prompt)

    state = {"started": False}
    ctx = orchestrator.Context(stream=stream)
    print("JARVIS と声で会話します。Enter を押してから話してください（q と Enter で終了）。")
    speaker.say("ジャービスです。どうぞ。")
    while True:
        try:
            key = input("\n[Enter で話す / q で終了] ").strip().lower()
        except (EOFError, KeyboardInterrupt):
            break
        speaker.stop()                      # Enter で読み上げを止めて次を話す
        if key == "q":
            break
        print("（聞いています…）", flush=True)
        audio = listen()
        if audio is None:
            print("聞き取れませんでした。")
            continue
        text = transcribe(audio).strip()
        if not text:
            print("聞き取れませんでした。")
            continue
        print(f"あなた（声）> {text}")
        if any(w in text for w in QUIT_WORDS) and len(text) <= 10:
            break

        state["started"] = False
        outcome = orchestrator.handle(text, ctx, ask=ask)
        if state["started"] and ctx.shown and outcome.text.startswith(ctx.shown):
            speaker.flush()
            rest = outcome.text[len(ctx.shown):].strip()      # 出典は表示だけ
            print(("\n\n" + rest if rest else "") + "\n")
        else:
            if state["started"]:
                speaker.flush()
                print()
            print(f"\nJARVIS > {outcome.text}\n")
            if outcome.skill not in STREAMED_SKILLS or not state["started"]:
                speaker.say(speech_summary(outcome.skill, outcome.text))   # 一覧やレポートは短い要約だけ読む
        try:
            record(text, outcome)
        except Exception as e:  # 記録の失敗で会話を止めない
            print(f"[記録できませんでした] {type(e).__name__}: {e}")
        if not (outcome.skill == "knowledge-capture" and outcome.method == "rule"):
            ctx.history.append(Turn(Who.OWNER, text))
            if outcome.ok and outcome.error is None:
                ctx.history.append(Turn(Who.AI, outcome.text))

    speaker.say("終了します。")
    speaker.wait()
    speaker.close()
    print("終了します。")
    return 0


if __name__ == "__main__":
    sys.stdin.reconfigure(encoding="utf-8")
    sys.stdout.reconfigure(encoding="utf-8")
    sys.exit(main())
