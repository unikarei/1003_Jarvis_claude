"""JARVIS と声で会話する CLI（選んで使うモード。文字の対話は chat.py。FR-014、../docs/spec.md §3.4.9）。

既定はハンズフリー: JARVIS が話し終えたら自動で聞き、話し始めと無音で区切る（ADR-0026。`--push-to-talk` で
Enter を押してから話す従来の形）。JARVIS が話している間は聞かない。音声は Mac の中で文字にし（faster-whisper）、録音は保存しない。
答えは画面に逐次表示しながら文ごとに読み上げる（macOS の say）。読み上げ中に Enter を押すと止める。
書き込みや課金のあるスキルの確認は、キーボードの y だけで受け付ける。終了: q と Enter、または「終了」と話す。
"""

import os
import queue
import sys
import threading
import time

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


# 無音や雑音を Whisper が文字にしてしまう決まり文句（話していないのに返事をしない）
NOISE_TEXTS = ("ご視聴ありがとうございました", "ご清聴ありがとうございました", "チャンネル登録", "おやすみなさい。",
               "Thank you for watching", "字幕")


def is_noise(text: str) -> bool:
    t = text.strip(" 　。、.!！?？")
    return not t or any(n.strip("。") in t for n in NOISE_TEXTS) and len(t) <= 20


def keyboard() -> "queue.Queue[str]":
    """キーボードの入力を別のスレッドで受ける（Enter で読み上げを止める・q で終了・書き込みの確認）。"""
    lines: queue.Queue[str] = queue.Queue()

    def read() -> None:
        for line in sys.stdin:
            lines.put(line.rstrip("\n"))
        lines.put("q")   # 入力が閉じたら終了

    threading.Thread(target=read, daemon=True).start()
    return lines


def _poll_keys(keys: "queue.Queue[str]", speaker: Speaker) -> bool:
    """届いているキーを処理する。q なら True（終了）。それ以外の Enter は読み上げを止める。"""
    while True:
        try:
            line = keys.get_nowait()
        except queue.Empty:
            return False
        if line.strip().lower() == "q":
            return True
        speaker.stop()


def main(listen=None, transcribe=None, speaker: Speaker | None = None, push_to_talk: bool = False,
         keys: "queue.Queue[str] | None" = None) -> int:
    """既定はハンズフリー（JARVIS が話し終えたら自動で聞く、ADR-0026）。push_to_talk で Enter を押してから話す。
    listen・transcribe・speaker・keys はテストで差し替える。"""
    if listen is None:
        if not mic.has_input_device():
            print("マイクが見つかりません。ヘッドセットなどをつないでから起動してください（文字の対話は chat.py）。")
            return 1
        print("音声認識のモデルを読み込んでいます（初回はダウンロードに数分かかります）…")
        from speech_to_text import load_model, transcribe_array
        load_model()
        listen, transcribe = mic.record_utterance, transcribe_array
    speaker = speaker or Speaker(voice=VOICE, device=output_device())
    keys = keys if keys is not None else keyboard()
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
        print(prompt, end="", flush=True)
        return keys.get()

    def finish_speaking() -> bool:
        """読み上げが終わるまで待つ（その間は聞かない）。q なら True。"""
        while speaker.speaking():
            if _poll_keys(keys, speaker):
                return True
            time.sleep(0.05)
        return _poll_keys(keys, speaker)

    state = {"started": False}
    ctx = orchestrator.Context(stream=stream, voice=True)
    if push_to_talk:
        print("JARVIS と声で会話します。Enter を押してから話してください（q と Enter で終了）。")
    else:
        print("JARVIS と声で会話します。そのまま話しかけてください。"
              "（読み上げ中に Enter で止める／「終了」と話すか q と Enter で終了）")
    speaker.say("ジャービスです。どうぞ。")
    prompt_shown = False
    while True:
        try:
            if push_to_talk:             # 従来の形: Enter で読み上げを止めて話す
                print("\n[Enter で話す / q で終了] ", end="", flush=True)
                if keys.get().strip().lower() == "q":
                    break
                speaker.stop()
                print("（聞いています…）", flush=True)
            elif finish_speaking():
                break
            elif not prompt_shown:
                print("\n（聞いています…）", flush=True)
                prompt_shown = True
            audio = listen()
            if audio is None:
                if push_to_talk:
                    print("聞き取れませんでした。")
                continue            # ハンズフリーでは話し始めるまで聞き続ける
            text = transcribe(audio).strip()
        except KeyboardInterrupt:
            break
        if is_noise(text):
            if push_to_talk:
                print("聞き取れませんでした。")
            continue
        prompt_shown = False
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

    speaker.stop()
    speaker.say("終了します。")
    speaker.wait()
    speaker.close()
    print("終了します。")
    return 0


if __name__ == "__main__":
    sys.stdin.reconfigure(encoding="utf-8")
    sys.stdout.reconfigure(encoding="utf-8")
    sys.exit(main(push_to_talk="--push-to-talk" in sys.argv[1:]))
