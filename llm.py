"""Claude APIへの問い合わせを共通化するヘルパー。"""

import atexit
import codecs
import contextlib
import http.client
import json
import os
import queue
import re
import selectors
import shutil
import socket
import subprocess
import tempfile
import threading
import time
import urllib.error
import urllib.parse
import urllib.request

import anthropic

import latency
from agent_log import log_event
from config import (ANTHROPIC_API_KEY, CHAT_MODEL, CLAUDE_BIN, CLAUDE_CLI_TIMEOUT_SECONDS, CODEX_BIN, CODEX_MODEL,
                    CODEX_REASONING, CODEX_TIMEOUT_SECONDS, ESCALATION_MODEL, LLM_PROVIDER, OLLAMA_TIMEOUT_SECONDS, OLLAMA_URL, SUMMARY_MODEL)

MIN_VALID_LENGTH = 5  # これより短い応答は検証失敗（空応答・応答エラー等）とみなす


def ask_claude(prompt: str, max_tokens: int = 1000, model: str = SUMMARY_MODEL) -> str:
    client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)
    message = client.messages.create(
        model=model,
        max_tokens=max_tokens,
        messages=[{"role": "user", "content": prompt}],
    )
    return message.content[0].text.strip()


def ask_claude_tiered(
    prompt: str,
    max_tokens: int = 1000,
    light_model: str = SUMMARY_MODEL,
    heavy_model: str = ESCALATION_MODEL,
    force_escalate: bool = False,
) -> str:
    """軽量レイヤーを既定とし、検証失敗時またはforce_escalate指定時のみ高精度レイヤーへ
    エスカレーションする（CLAUDE.md「エージェント・APIルーティング方針」参照）。
    エスカレーション発生時のみ jarvis_agent.log に記録する。
    """
    if not force_escalate:
        light_response = ask_claude(prompt, max_tokens=max_tokens, model=light_model)
        if len(light_response) >= MIN_VALID_LENGTH:
            return light_response
        reason = "light_response_too_short"
    else:
        reason = "force_escalate"

    heavy_response = ask_claude(prompt, max_tokens=max_tokens, model=heavy_model)
    log_event(
        "escalation",
        reason=reason,
        from_model=light_model,
        to_model=heavy_model,
        prompt_preview=prompt[:200],
    )
    return heavy_response


# --- プロバイダーを切り替えられる共通関数（../docs/decisions.md ADR-0020） ---
# 既存の ask_claude / ask_claude_tiered は変更しない。CKC など新しい処理は complete() を使う。


class LLMError(Exception):
    """LLM の呼び出しに失敗した（CLI の異常終了、上限到達、タイムアウトなど）。別のプロバイダーには切り替えない。"""


class LLMUnavailable(LLMError):
    """しばらく使えない失敗（利用枠の上限・ログインしていない・コマンドがない）。reason は "limit"・"auth"・"missing"。
    呼び出し側はこのプロバイダーを休止にできる（ADR-0035、../docs/spec.md §3.4.12 C）。"""

    def __init__(self, message: str, reason: str):
        super().__init__(message)
        self.reason = reason


# 失敗の文から、しばらく使えない理由を判定する（Claude Code・Codex の英語のメッセージ。2026-11-11 に Codex の実際の文を確認）
_LIMIT_PATTERN = re.compile(r"usage limit|rate limit|limit reached|hit your (?:usage )?limit|too many requests|"
                            r"quota|credit balance|out of credits|429", re.I)
_AUTH_PATTERN = re.compile(r"not logged in|please (?:log ?in|run .*login)|login required|unauthori[sz]ed|"
                           r"authentication|invalid api key|401", re.I)


def failure_reason(error: Exception) -> str | None:
    """失敗が「しばらく使えない」ものなら理由（"limit"・"auth"・"missing"）を、一時的な失敗なら None を返す。"""
    if isinstance(error, LLMCancelled):
        return None
    reason = getattr(error, "reason", None)
    if reason:
        return reason
    text = str(error)
    if "見つかりません" in text:
        return "missing"
    if _LIMIT_PATTERN.search(text):
        return "limit"
    if _AUTH_PATTERN.search(text):
        return "auth"
    return None


# claude -p に渡さない環境変数。.env の ANTHROPIC_API_KEY などが見えると、Claude Code は Pro のサブスクリプションでは
# なく API の従量課金で動く（ADR-0020 の「サブスクリプションの CLI を使う」に反する。2026-10-17 に検出）。
CLI_BLOCKED_ENV = ("ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN", "ANTHROPIC_BASE_URL", "CLAUDE_CODE_USE_BEDROCK",
                   "CLAUDE_CODE_USE_VERTEX", "CLAUDE_CODE_USE_FOUNDRY")


def _cli_env() -> dict[str, str]:
    """claude -p に渡す環境（API キー・API の接続先を除く）。"""
    return {k: v for k, v in os.environ.items() if k not in CLI_BLOCKED_ENV}


_claude_model: str | None = None   # claude -p に渡すモデル（None なら CLI の既定）。use_claude_model で決める


def use_claude_model(model: str | None) -> None:
    """このプロセスで起動する claude -p のモデルを決める（声の会話の起動時、2026-11-03 Owner 指示）。
    空・None なら CLI の既定。待機中の claude -p が別のモデルなら止める（次の答えで新しいモデルで起動する）。"""
    global _claude_model
    model = (model or "").strip() or None
    with _warm_lock:
        changed = model != _claude_model
        _claude_model = model
    if changed:
        stop_warm_claude()


def _model_options() -> list[str]:
    return ["--model", _claude_model] if _claude_model else []


def _complete_claude_cli(prompt: str, timeout: int | None = None) -> str:
    """Claude Code CLI（claude -p）をツールなしで実行し、最終応答のテキストを返す。"""
    timeout = timeout or CLAUDE_CLI_TIMEOUT_SECONDS
    cmd = [CLAUDE_BIN, "-p", "--tools", "", "--no-session-persistence", "--output-format", "json", *_model_options()]
    with tempfile.TemporaryDirectory(prefix="jarvis-llm-") as workdir:
        try:
            proc = subprocess.run(cmd, input=prompt, capture_output=True, text=True,
                                  cwd=workdir, timeout=timeout, env=_cli_env())
        except FileNotFoundError:
            raise LLMError(f"Claude Code CLI が見つかりません: {CLAUDE_BIN}") from None
        except subprocess.TimeoutExpired:
            raise LLMError(f"Claude Code CLI が {timeout} 秒でタイムアウトしました") from None

    if proc.returncode != 0:
        detail = (proc.stderr or proc.stdout).strip()[:500]
        raise LLMError(f"Claude Code CLI が終了コード {proc.returncode} で終了しました（利用上限の可能性あり）: {detail}")
    try:
        envelope = json.loads(proc.stdout)
    except json.JSONDecodeError:
        raise LLMError("Claude Code CLI の出力が JSON ではありません") from None
    if envelope.get("is_error") or envelope.get("subtype") != "success":
        raise LLMError(f"Claude Code CLI がエラーを返しました: subtype={envelope.get('subtype')} "
                       f"result={str(envelope.get('result', ''))[:200]}")
    return envelope.get("result", "")


def _complete_anthropic_api(prompt: str, timeout: int | None = None) -> str:  # timeout は SDK の既定に任せる
    try:
        return ask_claude(prompt, max_tokens=4000, model=ESCALATION_MODEL)
    except anthropic.APIError as e:
        raise LLMError(f"Claude API の呼び出しに失敗しました: {e}") from None


PROVIDERS = {"claude_cli": _complete_claude_cli, "anthropic_api": _complete_anthropic_api}


def complete(prompt: str, provider: str | None = None, timeout: int | None = None) -> str:
    """設定（JARVIS_LLM_PROVIDER）のプロバイダーでプロンプトを実行する。失敗時は LLMError。

    timeout（秒）は claude -p の上限。省略すると従来の既定（CLAUDE_CLI_TIMEOUT_SECONDS）。"""
    name = provider or LLM_PROVIDER
    if name not in PROVIDERS:
        raise LLMError(f"未知の LLM プロバイダーです: {name}（{', '.join(PROVIDERS)}）")
    return PROVIDERS[name](prompt, timeout=timeout) if timeout else PROVIDERS[name](prompt)


class LLMCancelled(LLMError):
    """呼び出し側の取り消し（声の割り込み、ADR-0028）で生成を止めた。"""


_STREAM_OPTIONS = ["-p", "--tools", "", "--no-session-persistence", "--output-format", "stream-json",
                   "--verbose", "--include-partial-messages"]


def _read_json_lines(proc, handle, deadline: float, timeout: int, cancel, label: str) -> None:
    """子プロセスの標準出力の JSON の行を終わり（EOF）まで読み、1 行ずつ handle(event) に渡す。
    改行を待たずに読み、行ごとと 0.1 秒ごとに取り消しを、0.1 秒ごとに時間切れを確かめる。
    プロセスは止めない（呼び出し側で止める）。"""
    selector = selectors.DefaultSelector()
    selector.register(proc.stdout, selectors.EVENT_READ)
    fd = proc.stdout.fileno()
    os.set_blocking(fd, False)                 # 改行を待たずに読む（行の途中で止まっても取り消せる）
    decoder = codecs.getincrementaldecoder("utf-8")()
    pending, lines, eof = "", [], False
    try:
        while True:
            if cancel is not None and cancel():   # 一度に届いた複数の行の間でも確かめ、以後の差分を渡さない
                raise LLMCancelled("取り消されました")   # 呼び出し側の finally でプロセスを止める
            if lines:
                line = lines.pop(0)
                try:
                    event = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if isinstance(event, dict):
                    handle(event)
                continue
            if eof:
                break
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise LLMError(f"{label} が {timeout} 秒でタイムアウトしました")
            if not selector.select(timeout=min(remaining, 0.1)):
                continue
            try:
                chunk = os.read(fd, 65536)
            except BlockingIOError:
                continue
            if not chunk:
                eof = True
                pending += decoder.decode(b"", final=True)
                if pending:
                    lines.append(pending)
                continue
            pending += decoder.decode(chunk)
            *complete, pending = pending.split("\n")
            lines.extend(complete)
    finally:
        selector.close()


def _read_claude_stream(proc, on_text, deadline: float, timeout: int, cancel) -> dict | None:
    """claude -p の stream-json を終わりまで読み、差分を on_text に渡して result のイベントを返す。"""
    found = {}

    def handle(event):
        if event.get("type") == "stream_event":
            delta = (event.get("event") or {}).get("delta") or {}
            if delta.get("type") == "text_delta" and delta.get("text"):
                on_text(delta["text"])
        elif event.get("type") == "result":
            found["result"] = event

    _read_json_lines(proc, handle, deadline, timeout, cancel, "Claude Code CLI")
    return found.get("result")


def _claude_result_text(proc, result: dict | None) -> str:
    """stream-json の result のイベントから全文を取り出す。終わりまで届かなかった・エラーなら LLMError。"""
    if result is None:
        detail = (proc.stderr.read() if proc.stderr else "").strip()[:500]
        raise LLMError(f"Claude Code CLI が終了コード {proc.returncode} で終了しました（利用上限の可能性あり）: {detail}")
    if result.get("is_error") or result.get("subtype") != "success":
        raise LLMError(f"Claude Code CLI がエラーを返しました: subtype={result.get('subtype')} "
                       f"result={str(result.get('result', ''))[:200]}")
    return result.get("result", "")


def _stream_claude_cli(prompt: str, on_text, timeout: int | None = None, cancel=None) -> str:
    """claude -p の逐次出力（stream-json）。差分のテキストを on_text に渡し、最後に全文を返す（FR-014、T-1212）。"""
    timeout = timeout or CLAUDE_CLI_TIMEOUT_SECONDS
    cmd = [CLAUDE_BIN, *_STREAM_OPTIONS, *_model_options()]
    deadline = time.monotonic() + timeout
    with tempfile.TemporaryDirectory(prefix="jarvis-llm-") as workdir:
        try:
            proc = subprocess.Popen(cmd, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                    text=True, cwd=workdir, env=_cli_env())
        except FileNotFoundError:
            raise LLMError(f"Claude Code CLI が見つかりません: {CLAUDE_BIN}") from None
        latency.mark("llm_ready")                  # 起動した claude -p に送れる状態（遅延の記録、T-1405）
        proc.stdin.write(prompt)
        proc.stdin.close()
        try:
            result = _read_claude_stream(proc, on_text, deadline, timeout, cancel)
        finally:
            if proc.poll() is None:
                proc.kill()
            proc.wait()
    return _claude_result_text(proc, result)


# --- 待機させた claude -p（"claude_warm"、ADR-0027、T-1402） ---
# claude -p の起動（約 3 秒）を答えの前に済ませておく。1 回の答えに 1 つのプロセスを使い（会話の文脈をためない）、
# 使い終えたら次のプロセスを起動しておく。待機させるのは 1 つだけで、終了時（atexit）に止める。

_WARM_OPTIONS = [*_STREAM_OPTIONS, "--input-format", "stream-json"]
_warm_lock = threading.Lock()
_warm = None                    # 待機中の (プロセス, 作業フォルダ)
_atexit_registered = False


def _spawn_claude_warm():
    """stream-json の入力を待つ claude -p を起動する。"""
    global _atexit_registered
    workdir = tempfile.TemporaryDirectory(prefix="jarvis-llm-")
    try:
        proc = subprocess.Popen([CLAUDE_BIN, *_WARM_OPTIONS, *_model_options()], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                stderr=subprocess.PIPE, text=True, cwd=workdir.name, env=_cli_env())
    except FileNotFoundError:
        workdir.cleanup()
        raise LLMError(f"Claude Code CLI が見つかりません: {CLAUDE_BIN}") from None
    if not _atexit_registered:
        atexit.register(stop_warm_claude)
        _atexit_registered = True
    return proc, workdir


def _end_claude_process(proc, workdir) -> None:
    if proc.poll() is None:
        proc.kill()
    proc.wait()
    for stream in (proc.stdin, proc.stdout, proc.stderr):
        try:
            if stream:
                stream.close()
        except OSError:
            pass
    workdir.cleanup()


def warm_up_claude() -> bool:
    """次の答えに使う claude -p を前もって起動しておく（待機中のものが生きていれば何もしない）。
    失敗しても例外を出さず False を返す。"""
    global _warm
    with _warm_lock:
        if _warm is not None and _warm[0].poll() is None:
            return True
        dead, _warm = _warm, None
        if dead is not None:
            _end_claude_process(*dead)
        try:
            _warm = _spawn_claude_warm()
        except (LLMError, OSError):
            return False
        return True


def stop_warm_claude() -> None:
    """待機中の claude -p を止める（atexit に登録する）。"""
    global _warm
    with _warm_lock:
        taken, _warm = _warm, None
    if taken is not None:
        _end_claude_process(*taken)


def _take_claude_warm():
    """待機中の claude -p を受け取る。ない・死んでいるときはその場で起動する。
    待機中のものを使えたかを遅延の記録の warm に入れる（T-1405）。"""
    global _warm
    with _warm_lock:
        taken, _warm = _warm, None
    if taken is not None:
        if taken[0].poll() is None:
            latency.set("warm", True)
            return taken
        _end_claude_process(*taken)
    latency.set("warm", False)
    return _spawn_claude_warm()


def _send_message(proc, message: str) -> bool:
    try:
        proc.stdin.write(message)
        proc.stdin.close()
        return True
    except OSError:                                # 確かめた後で死んでいた（BrokenPipeError など）
        return False


def _stream_claude_warm(prompt: str, on_text, timeout: int | None = None, cancel=None) -> str:
    """待機させた claude -p に発言を 1 行送り、_stream_claude_cli と同じ方法で結果まで読む。"""
    timeout = timeout or CLAUDE_CLI_TIMEOUT_SECONDS
    message = json.dumps({"type": "user", "message": {"role": "user", "content": prompt}}, ensure_ascii=False) + "\n"
    proc, workdir = _take_claude_warm()
    latency.mark("llm_ready")                      # 待機中の claude を受け取った・起動した（T-1405）
    try:
        if not _send_message(proc, message):
            _end_claude_process(proc, workdir)
            latency.set("warm", False)             # 受け取ったものは死んでいた。その場で起動したものを使う
            proc, workdir = _spawn_claude_warm()
            if not _send_message(proc, message):
                raise LLMError("Claude Code CLI に発言を送れませんでした（起動直後に終了）")
        deadline = time.monotonic() + timeout
        try:
            result = _read_claude_stream(proc, on_text, deadline, timeout, cancel)
        finally:
            if proc.poll() is None:
                proc.kill()
            proc.wait()
        return _claude_result_text(proc, result)
    finally:
        _end_claude_process(proc, workdir)
        warm_up_claude()                           # 次の答えのために起動しておく


def _complete_claude_warm(prompt: str, timeout: int | None = None) -> str:
    return _stream_claude_warm(prompt, lambda _text: None, timeout)


# --- ローカルの Ollama（"ollama"、ADR-0027、T-1402） ---
# 失敗しても別のプロバイダー（有料の API を含む）には切り替えない。切り替えは呼び出し側が決める。

OLLAMA_KEEP_ALIVE = "30m"
# ollama_options() の中だけ Ollama の要求に足すオプション（評価の temperature・seed、T-1414）。
# 逐次出力の読み取りは別のスレッドで動くため、スレッドごとの値ではなくモジュールの変数で持つ
_ollama_extra_options: dict | None = None


@contextlib.contextmanager
def ollama_options(options: dict):
    """この中で送る Ollama の要求に "options" を足す。出るとき（例外でも）元に戻す。入れ子は外側の値に戻す。"""
    global _ollama_extra_options
    previous = _ollama_extra_options
    _ollama_extra_options = dict(options)
    try:
        yield
    finally:
        _ollama_extra_options = previous


def _ollama_chat_payload(prompt: str, stream: bool) -> dict:
    payload = {"model": CHAT_MODEL, "messages": [{"role": "user", "content": prompt}], "stream": stream,
               "think": False, "keep_alive": OLLAMA_KEEP_ALIVE}
    if _ollama_extra_options:                      # 設定していないときは今までと同じ要求
        payload["options"] = dict(_ollama_extra_options)
    return payload


def _http_error_detail(error) -> str:
    try:
        return error.read()[:200].decode("utf-8", "replace")
    except Exception:  # noqa: BLE001  本文が読めなくても状態コードだけで伝える
        return ""


def _ollama_post(path: str, payload: dict, timeout: float) -> dict:
    request = urllib.request.Request(OLLAMA_URL + path, data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
                                     headers={"Content-Type": "application/json"}, method="POST")
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            body = response.read()
    except urllib.error.HTTPError as e:
        raise LLMError(f"Ollama が HTTP {e.code} を返しました: {_http_error_detail(e)}") from None
    except OSError as e:                           # 接続できない・時間切れ（URLError・TimeoutError を含む）
        raise LLMError(f"Ollama に接続できません（{OLLAMA_URL}）: {e}") from None
    try:
        data = json.loads(body)
    except ValueError:
        raise LLMError("Ollama の応答が JSON ではありません") from None
    if not isinstance(data, dict):
        raise LLMError("Ollama の応答の形が想定と違います")
    if data.get("error"):
        raise LLMError(f"Ollama がエラーを返しました: {str(data['error'])[:200]}")
    return data


def _complete_ollama(prompt: str, timeout: int | None = None) -> str:
    latency.mark("llm_ready")                      # 要求を送る直前（遅延の記録、T-1405）
    data = _ollama_post("/api/chat", _ollama_chat_payload(prompt, stream=False), timeout or OLLAMA_TIMEOUT_SECONDS)
    return (data.get("message") or {}).get("content") or ""


def _stream_ollama(prompt: str, on_text, timeout: int | None = None, cancel=None) -> str:
    """Ollama の逐次出力（1 行 1 つの JSON）。差分を on_text に渡し、最後に全文を返す。
    読み取りは別のスレッドで行い、行ごとと 0.1 秒ごとに取り消しと時間切れを確かめる。取り消したら接続を閉じる。"""
    timeout = timeout or OLLAMA_TIMEOUT_SECONDS
    deadline = time.monotonic() + timeout
    url = urllib.parse.urlsplit(OLLAMA_URL)
    connection_class = http.client.HTTPSConnection if url.scheme == "https" else http.client.HTTPConnection
    conn = connection_class(url.hostname, url.port, timeout=timeout)
    body = json.dumps(_ollama_chat_payload(prompt, stream=True), ensure_ascii=False).encode("utf-8")
    items = queue.Queue()
    stop = threading.Event()
    sock_lock = threading.Lock()
    socks = []      # 接続のソケット。HTTP/1.0・Connection: close の応答では getresponse() の後に conn.sock が None に
                    # なり応答の側がソケットを持つので、接続した時点で控えておき、取り消し・終わりのときにこれを閉じる

    def reader():
        response = None
        try:
            conn.connect()
            with sock_lock:
                if stop.is_set():                  # 接続する前に取り消された・時間切れになった
                    return
                socks.append(conn.sock)
            conn.request("POST", url.path.rstrip("/") + "/api/chat", body=body,
                         headers={"Content-Type": "application/json"})
            response = conn.getresponse()
            if response.status != 200:
                detail = response.read(200).decode("utf-8", "replace")
                items.put(LLMError(f"Ollama が HTTP {response.status} を返しました: {detail}"))
                return
            for line in response:
                if stop.is_set():
                    return
                items.put(line)
            items.put(None)
        except Exception as e:  # noqa: BLE001  受け取る側で LLMError にする
            items.put(e)
        finally:
            if response is not None:
                response.close()
            conn.close()

    latency.mark("llm_ready")                      # 要求を送る直前（送信は読み取りのスレッド。遅延の記録、T-1405）
    threading.Thread(target=reader, name="jarvis-ollama-reader", daemon=True).start()
    parts = []
    done = False
    try:
        while True:
            if cancel is not None and cancel():
                raise LLMCancelled("取り消されました")   # finally で接続を閉じる
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise LLMError(f"Ollama が {timeout} 秒でタイムアウトしました")
            try:
                item = items.get(timeout=min(remaining, 0.1))
            except queue.Empty:
                continue
            if item is None:                       # 接続が終わった
                if not done:                       # done: true の前に切れた（途中の答えを成功にしない）
                    raise LLMError("Ollama の応答が途中で切れました（done を受け取れませんでした）")
                break
            if isinstance(item, LLMError):
                raise item
            if isinstance(item, Exception):
                raise LLMError(f"Ollama に接続できません（{OLLAMA_URL}）: {item}")
            try:
                event = json.loads(item)
            except ValueError:
                continue
            if not isinstance(event, dict):
                continue
            if event.get("error"):
                raise LLMError(f"Ollama がエラーを返しました: {str(event['error'])[:200]}")
            text = (event.get("message") or {}).get("content") or ""
            if text:
                parts.append(text)
                on_text(text)
            if cancel is not None and cancel():        # 最後の差分の on_text の中で取り消された
                raise LLMCancelled("取り消されました")
            if event.get("done"):
                done = True
                break
    finally:
        with sock_lock:
            stop.set()
            taken = list(socks)
        for sock in taken:                         # 読み取りのスレッドの待ちを解く（接続を閉じる）
            try:
                sock.shutdown(socket.SHUT_RDWR)
            except OSError:                        # すでに閉じている
                pass
    return "".join(parts)


def warm_up_ollama(timeout: float | None = None) -> bool:
    """モデルを読み込ませておく（空の会話で keep_alive だけ送る）。失敗しても例外を出さず False を返す。"""
    try:
        _ollama_post("/api/chat", {"model": CHAT_MODEL, "messages": [], "stream": False,
                                   "keep_alive": OLLAMA_KEEP_ALIVE}, timeout or OLLAMA_TIMEOUT_SECONDS)
    except LLMError:
        return False
    return True


def ollama_available(timeout: float = 1.0) -> bool:
    """Ollama が応答し、設定のモデル（JARVIS_CHAT_MODEL）があるか。"""
    try:
        with urllib.request.urlopen(OLLAMA_URL + "/api/tags", timeout=timeout) as response:
            data = json.loads(response.read())
        names = {model.get("name") or model.get("model") for model in data.get("models") or []}
    except Exception:  # noqa: BLE001  確かめるだけなので、どんな失敗も「ない」とする
        return False
    wanted = {CHAT_MODEL} if ":" in CHAT_MODEL else {CHAT_MODEL, CHAT_MODEL + ":latest"}
    return bool(names & wanted)


# --- Codex（"codex"、ADR-0035、T-2001） ---
# codex exec を 1 回の答えごとに起動する。答えるだけにする（Claude の --tools "" と同じ扱い）: シェルを無効にし、
# 読み取り専用・ユーザー設定（MCP など）を読まない・Web 検索なし・承認を求めない・空の一時フォルダ・セッションを保存しない。
# それでもツールの実行のイベントが来たら止めて失敗にする。API キーは渡さず、ChatGPT のサブスクリプションのログインだけを使う。

CODEX_BLOCKED_ENV = (*CLI_BLOCKED_ENV, "OPENAI_API_KEY", "OPENAI_BASE_URL", "OPENAI_ORG_ID", "OPENAI_ORGANIZATION",
                     "OPENAI_PROJECT", "CODEX_API_KEY")
# 無効にする Codex の機能（ツールを出すもの）。2026-11-11 の実機で、shell_tool と web_search の無効だけでは
# codex_apps の Web 検索（mcp_tool_call）・コードの実行（functions.exec）・別のエージェントの起動などが使えた
CODEX_DISABLED_FEATURES = ("shell_tool", "apps", "browser_use", "browser_use_external", "computer_use", "code_mode_host",
                           "goals", "sleep_tool", "skill_search", "tool_suggest", "multi_agent", "image_generation",
                           "view_image")
_CODEX_MESSAGE_ITEMS = {"agent_message", "assistant_message"}
# 答えの文以外で受け付ける項目（考えの要約、無効にした機能についての知らせ）。これ以外の項目（ツールの呼び出しなど）が
# 来たら止める（許可の一覧。Codex が新しいツールを足しても、名前を知らずに止められる）
_CODEX_PASSIVE_ITEMS = {"reasoning", "error"}


def find_codex_bin() -> str | None:
    """JARVIS_CODEX_BIN → PATH → VS Code の拡張機能に同梱の版（最新）。開発オーケストレーターと同じ探し方。"""
    if CODEX_BIN:
        return CODEX_BIN if os.path.exists(CODEX_BIN) else None
    found = shutil.which("codex")
    if found:
        return found
    ext = os.path.join(os.path.expanduser("~"), ".vscode", "extensions")
    try:
        names = [n for n in os.listdir(ext) if n.startswith("openai.chatgpt-")]
    except OSError:
        return None
    def version(name):
        return [int(x) if x.isdigit() else x for x in re.split(r"[.-]", name)]
    for name in sorted(names, key=version, reverse=True):
        path = os.path.join(ext, name, "bin", "macos-aarch64", "codex")
        if os.path.exists(path):
            return path
    return None


def _codex_env() -> dict[str, str]:
    return {k: v for k, v in os.environ.items() if k not in CODEX_BLOCKED_ENV}


def _codex_command(binary: str, workdir: str) -> list[str]:
    cmd = [binary, "exec", "--json", "--ephemeral", "--sandbox", "read-only", "--skip-git-repo-check",
           "--ignore-user-config", *[x for name in CODEX_DISABLED_FEATURES for x in ("--disable", name)],
           "-c", 'web_search="disabled"',
           "-c", 'approval_policy="never"', "-C", workdir]
    if CODEX_REASONING:
        cmd += ["-c", f'model_reasoning_effort="{CODEX_REASONING}"']
    if CODEX_MODEL:
        cmd += ["--model", CODEX_MODEL]
    return [*cmd, "-"]                             # プロンプトは標準入力から


def _codex_failure(message: str) -> LLMError:
    message = (message or "").strip()[:500]
    text = f"Codex がエラーを返しました: {message}"
    reason = "limit" if _LIMIT_PATTERN.search(message) else "auth" if _AUTH_PATTERN.search(message) else None
    return LLMUnavailable(text, reason) if reason else LLMError(text)


def _stream_codex(prompt: str, on_text, timeout: int | None = None, cancel=None) -> str:
    """codex exec --json の出力（1 行 1 つの JSON）を読み、答えの文の増えた分を on_text に渡し、最後に全文を返す。
    途中の文のイベントがない版では、答えができた時点で全文を 1 回渡す。"""
    timeout = timeout or CODEX_TIMEOUT_SECONDS
    binary = find_codex_bin()
    if not binary:
        raise LLMUnavailable("Codex CLI が見つかりません（JARVIS_CODEX_BIN・PATH・VS Code の拡張機能）", "missing")
    deadline = time.monotonic() + timeout
    sent: dict[str, str] = {}                      # 答えの項目ごとに、渡し終えた文
    state = {"answer": None, "done": False, "error": None}

    def handle(event):
        kind = event.get("type")
        if kind in ("error", "turn.failed"):
            error = event.get("error") if isinstance(event.get("error"), dict) else {}
            state["error"] = state["error"] or event.get("message") or error.get("message") or "不明なエラー"
            return
        if kind == "turn.completed":
            state["done"] = True
            return
        item = event.get("item") if isinstance(event.get("item"), dict) else None
        if not item or not str(kind).startswith("item."):
            return
        kind_of_item = item.get("type")
        if kind_of_item in _CODEX_PASSIVE_ITEMS:
            return
        if kind_of_item not in _CODEX_MESSAGE_ITEMS:
            raise LLMError(f"Codex がツール（{kind_of_item}）を使おうとしたため止めました")
        key = str(item.get("id") or "")
        text = item.get("text") or ""
        before = sent.get(key, "")
        if text.startswith(before) and len(text) > len(before):
            on_text(text[len(before):])
            sent[key] = text
        if kind == "item.completed":
            state["answer"] = text

    with tempfile.TemporaryDirectory(prefix="jarvis-codex-") as workdir:
        try:
            proc = subprocess.Popen(_codex_command(binary, workdir), stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                    stderr=subprocess.PIPE, text=True, cwd=workdir, env=_codex_env())
        except FileNotFoundError:
            raise LLMUnavailable(f"Codex CLI が見つかりません: {binary}", "missing") from None
        latency.mark("llm_ready")
        try:
            try:
                proc.stdin.write(prompt)
                proc.stdin.close()
            except OSError:
                pass                               # 起動直後に終わった。下で終了コードと出力から判断する
            _read_json_lines(proc, handle, deadline, timeout, cancel, "Codex CLI")
        finally:
            if proc.poll() is None:
                proc.kill()
            proc.wait()
        stderr = (proc.stderr.read() if proc.stderr else "").strip()
        for stream in (proc.stdout, proc.stderr):
            try:
                stream.close()
            except (OSError, AttributeError):
                pass
    if state["answer"] is not None and state["done"]:
        return state["answer"]                     # 途中の一時的なエラー（再接続など）があっても、答えが届けば成功
    raise _codex_failure(state["error"] or stderr or f"終了コード {proc.returncode}（答えを受け取れませんでした）")


def _complete_codex(prompt: str, timeout: int | None = None) -> str:
    return _stream_codex(prompt, lambda _text: None, timeout)


PROVIDERS.update({"ollama": _complete_ollama, "claude_warm": _complete_claude_warm, "codex": _complete_codex})


def complete_stream(prompt: str, on_text, provider: str | None = None, timeout: int | None = None,
                    cancel=None) -> str:
    """complete() の逐次表示版。claude_cli・claude_warm・ollama では生成中の差分を on_text に渡す。
    逐次出力のないプロバイダーは、全文を一度だけ on_text に渡す。失敗時は LLMError（別のプロバイダーに切り替えない）。"""
    name = provider or LLM_PROVIDER
    if name == "claude_cli" and PROVIDERS.get(name) is _complete_claude_cli:
        return _stream_claude_cli(prompt, on_text, timeout, cancel)
    if name == "claude_warm" and PROVIDERS.get(name) is _complete_claude_warm:
        return _stream_claude_warm(prompt, on_text, timeout, cancel)
    if name == "ollama" and PROVIDERS.get(name) is _complete_ollama:
        return _stream_ollama(prompt, on_text, timeout, cancel)
    if name == "codex" and PROVIDERS.get(name) is _complete_codex:
        return _stream_codex(prompt, on_text, timeout, cancel)
    # 逐次出力のないプロバイダーは、生成の途中では取り消せない（前と後で確かめ、取り消されていれば結果を渡さない）
    if cancel is not None and cancel():
        raise LLMCancelled("取り消されました")
    text = complete(prompt, provider=provider, timeout=timeout)
    if cancel is not None and cancel():
        raise LLMCancelled("取り消されました")
    on_text(text)
    return text
