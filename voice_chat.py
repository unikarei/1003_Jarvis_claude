"""JARVIS と声で会話する CLI（選んで使うモード。文字の対話は chat.py。FR-014、../docs/spec.md §3.4.9）。

既定はハンズフリーで割り込みあり: JARVIS が考えている間・話している間も聞き、Owner が話し始めたら読み上げを止めて
新しい質問に答える（ADR-0026・0028）。`--no-barge-in`（JARVIS_VOICE_BARGE_IN=0）で話している間は聞かない形、
`--push-to-talk` で Enter を押してから話す従来の形。音声は Mac の中で文字にし（faster-whisper）、録音は保存しない。
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
from voice import duplex
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


class Cancelled(Exception):
    """割り込まれた発言の答えの生成を止める（逐次表示の途中で投げ、claude -p を止めさせる）。"""


INTERRUPTED_MARK = "（ここで Owner が割り込み）"


def run_barge_in(session, transcribe, keys: "queue.Queue[str]") -> int:
    """割り込みのある会話（ADR-0028）。状態: 聞き取り → 考え中 → 読み上げ（考え中・読み上げ中に Owner が話すと
    割り込み → 聞き取り）。発言ごとに番号（speaker.turn）を付け、古い番号の生成・合成・再生は捨てる。
    session は voice.duplex.Session と同じ形（テストで差し替える）。"""
    speaker = session.speaker
    init_db()
    state = {"turn": 0, "started": False, "asking": False, "status": "IDLE"}

    def stream(chunk: str) -> None:
        if state["turn"] != speaker.turn:
            raise Cancelled()                 # 割り込まれた。claude -p は llm 側で止まる
        if not state["started"]:
            state["started"] = True
            state["status"] = "SPEAKING"
            print("\nJARVIS > ", end="", flush=True)
        print(chunk, end="", flush=True)
        speaker.feed(chunk)

    def ask(prompt: str) -> str:
        state["asking"] = True
        session.disarm()                      # 確認を待つ間は割り込みを受けない
        speaker.stop()
        speaker.say("書き込みがあります。キーボードで確認してください。")
        print(prompt, end="", flush=True)
        try:
            return keys.get()
        finally:
            state["asking"] = False

    ctx = orchestrator.Context(stream=stream, voice=True, cancel=lambda: state["turn"] != speaker.turn)
    print("JARVIS と声で会話します。そのまま話しかけてください。JARVIS が話している途中でも、話し始めれば止まります。"
          "（「終了」と話すか q と Enter で終了）")
    info = session.calibrate("ジャービスです。どうぞ。")
    print(f"（スピーカーからマイクへの回り込みを測りました: {info}）")
    from_barge, prompt_shown = False, False
    while True:
        if _poll_keys(keys, speaker):
            break
        if not from_barge and not prompt_shown:
            print("\n（聞いています…）", flush=True)
            prompt_shown = True
        state["status"] = "LISTENING"
        try:
            audio = session.listen(from_barge_in=from_barge)
            from_barge = False
            if audio is None:
                continue
            text = transcribe(audio).strip()
        except KeyboardInterrupt:
            break
        if is_noise(text):
            continue
        prompt_shown = False
        print(f"あなた（声）> {text}")
        if any(w in text for w in QUIT_WORDS) and len(text) <= 10:
            break

        turn = speaker.new_turn()
        state.update(turn=turn, started=False, status="THINKING")
        result: dict = {}
        worker = threading.Thread(target=lambda: result.update(outcome=orchestrator.handle(text, ctx, ask=ask)),
                                  daemon=True)
        session.arm()
        worker.start()
        finished, interrupted = False, False
        while True:
            if session.barged.is_set():
                interrupted = True
                break
            if not finished and not worker.is_alive():
                finished = True
                _finish_answer(result["outcome"], state, ctx, speaker)
            if finished and not speaker.speaking():
                break
            if not state["asking"] and _poll_keys(keys, speaker):
                session.disarm()
                speaker.stop()
                worker.join(timeout=5)
                return _end(speaker, session)
            time.sleep(0.02)
        session.disarm()

        if interrupted:
            state["status"] = "INTERRUPTED"
            heard = speaker.heard(turn)
            speaker.new_turn()                # 残りの合成・再生を捨てる（生成は stream で止まる）
            ev = session.events[-1] if getattr(session, "events", None) else None
            timing = (f"、話し始めから停止まで約 {ev['detect_ms'] + ev['stop_call_ms'] + ev['output_ms']:.0f} ms"
                      f"（検出 {ev['detect_ms']}・停止の指示 {ev['stop_call_ms']}・出力 {ev['output_ms']}）") if ev else ""
            print(f"\n（割り込み: 読み上げを止めました{timing}）")
            worker.join(timeout=10)            # 生成は取り消しの確認（0.1 秒ごと）で止まる
            if worker.is_alive():              # 止まらない処理（逐次表示のないスキル）は、文脈を分けて切り離す
                print("[前の処理は裏で終わるまで続きます。結果は使いません]")
                ctx = orchestrator.Context(stream=stream, voice=True, cancel=ctx.cancel, history=list(ctx.history))
            if "outcome" in result:
                _record(text, result["outcome"])
            ctx.history.append(Turn(Who.OWNER, text))
            if heard:                         # 聞いた文だけを履歴に残す
                ctx.history.append(Turn(Who.AI, "".join(heard) + INTERRUPTED_MARK))
            from_barge = True
            continue

        outcome = result["outcome"]
        _record(text, outcome)
        if not (outcome.skill == "knowledge-capture" and outcome.method == "rule"):
            ctx.history.append(Turn(Who.OWNER, text))
            if outcome.ok and outcome.error is None:
                ctx.history.append(Turn(Who.AI, outcome.text))
        state["status"] = "IDLE"
    return _end(speaker, session)


def _finish_answer(outcome, state: dict, ctx, speaker) -> None:
    """答えの残り（出典など）を表示し、逐次表示しなかった答えは短い要約を読む。"""
    if state["started"] and ctx.shown and outcome.text.startswith(ctx.shown):
        speaker.flush()
        rest = outcome.text[len(ctx.shown):].strip()
        print(("\n\n" + rest if rest else "") + "\n")
        return
    if state["started"]:
        speaker.flush()
        print()
    print(f"\nJARVIS > {outcome.text}\n")
    if outcome.skill not in STREAMED_SKILLS or not state["started"]:
        state["status"] = "SPEAKING"
        speaker.say(speech_summary(outcome.skill, outcome.text))


def _record(text: str, outcome) -> None:
    try:
        record(text, outcome)
    except Exception as e:  # 記録の失敗で会話を止めない
        print(f"[記録できませんでした] {type(e).__name__}: {e}")


def _end(speaker, session) -> int:
    session.disarm()
    speaker.new_turn()
    speaker.say("終了します。")
    speaker.wait()
    speaker.close()
    session.close()
    print("終了します。")
    return 0


def main(listen=None, transcribe=None, speaker: Speaker | None = None, push_to_talk: bool = False,
         keys: "queue.Queue[str] | None" = None, barge_in: bool | None = None) -> int:
    """既定はハンズフリー（JARVIS が話し終えたら自動で聞く、ADR-0026）。push_to_talk で Enter を押してから話す。
    listen・transcribe・speaker・keys はテストで差し替える。"""
    if listen is None:
        if not mic.has_input_device():
            print("マイクが見つかりません。ヘッドセットなどをつないでから起動してください（文字の対話は chat.py）。")
            return 1
        print("音声認識のモデルを読み込んでいます（初回はダウンロードに数分かかります）…")
        from speech_to_text import load_model, transcribe_array
        load_model()
        if barge_in is None:
            barge_in = duplex.barge_in_enabled()
        if barge_in and not push_to_talk:     # 割り込みのある会話（ADR-0028）
            player = duplex.Player(device=output_device())
            session = duplex.Session(player, duplex.DuplexSpeaker(player, voice=VOICE))
            return run_barge_in(session, transcribe_array, keys if keys is not None else keyboard())
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
    sys.exit(main(push_to_talk="--push-to-talk" in sys.argv[1:],
                  barge_in=False if "--no-barge-in" in sys.argv[1:] else None))
