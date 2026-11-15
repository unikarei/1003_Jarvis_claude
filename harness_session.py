"""会話ごとのハーネスのセッションと引き継ぎ（../docs/spec.md §3.4.12 G・H、ADR-0038、T-2402〜T-2405・T-2407）。

- 会話の正本は Jarvis の記録（memory_turns。JARVIS_MEMORY=0 なら ctx.history）。セッションは捨ててよい作業台。
- 会話 ID ごとにセッションを 1 つ持つ: claude は会話の間保つ claude -p（llm.ClaudeSession）、codex は app-server の
  スレッド（llm.CodexThread）。ollama はセッションを持たず、毎回引き継ぎメモ付きの 1 回限りのプロンプト。
- 最初のメッセージ（新規・引き継ぎ）は引き継ぎメモ（それより前の機械的なまとめ＋直近 N 往復＋記憶パック）＋今回の発言。
  2 回目以降は今回の発言と、その間にセッションの外で答えた発言、まだ渡していない記憶の項目だけ。
- ハーネスが変わったら前のセッションを閉じ、切り替え先で新しく始める（前のハーネスに戻るときも新しく）。
- 取り消し・時間切れ・エラー・前の答えがまだ動いている・30 分の無発言では、セッションを捨てて次で作り直す。
"""

import atexit
import os
import re
import sys
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path

import config
import latency
import llm
import prompts

# プロバイダー → セッションを持つハーネス（orchestrator.HARNESS_PROVIDERS の名前）
SESSION_PROVIDERS = {"claude_warm": "claude", "codex": "codex"}
TURN_CHARS = 600            # 引き継ぎメモの直近の発言の上限（1 発言あたり）
OLDER_OWNER_CHARS = 100     # それより前の Owner の発言
OLDER_ANSWER_CHARS = 60     # それより前の答えの最初の 1 文
OLDER_TOTAL_CHARS = 1500    # それより前のまとめの上限（新しい方から）
_SENTENCE = re.compile(r"^.*?[。．！？!?](?=\s|$|[^。．！？!?])", re.S)

clock = time.monotonic      # テストで差し替える


@dataclass
class Session:
    session_id: str
    harness: str
    handle: object                         # llm.ClaudeSession / llm.CodexThread
    synced: int = 0                        # ctx.history のうち、このセッションが知っている数
    own: tuple | None = None               # 最後に答えた (Owner の発言, 答え)
    sent_memory: set = field(default_factory=set)
    last_used: float = 0.0
    busy: threading.Lock = field(default_factory=threading.Lock)
    asked: bool = False                    # 一度でも発言を送ったか（前もって起動しただけなら最初のメッセージを送る）


_lock = threading.Lock()
_sessions: dict[str, Session] = {}


def enabled() -> bool:
    return config.HARNESS_SESSION


def uses_session(provider: str) -> bool:
    """このプロバイダーの答えを会話のセッションで作るか（claude_warm・codex、JARVIS_HARNESS_SESSION=1）。"""
    return enabled() and provider in SESSION_PROVIDERS


# ---------------------------------------------------------------- 正本と引き継ぎメモ（T-2402）

def _speaker(turn) -> str:
    speaker = turn["speaker"] if isinstance(turn, dict) else getattr(turn.speaker, "value", turn.speaker)
    return "owner" if speaker == "owner" else "ai"


def _text(turn) -> str:
    return (turn["text"] if isinstance(turn, dict) else turn.text) or ""


def canonical_turns(ctx) -> list:
    """会話の発言の正本。記録（memory_turns）が ctx.history 以上に揃っていればそれ、なければ ctx.history。"""
    history = list(ctx.history or [])
    from memory import store
    if not (config.MEMORY_ENABLED and store.enabled()):
        return history
    try:
        store.flush(1.0)
        turns = store.session_turns(ctx.session_id)
    except Exception as error:  # noqa: BLE001  記録が読めなくても答える
        store.warn("引き継ぎ", error)
        return history
    return turns if len(turns) >= len(history) else history


def _first_sentence(text: str) -> str:
    text = " ".join(text.split())
    match = _SENTENCE.match(text)
    return (match.group(0) if match else text)[:OLDER_ANSWER_CHARS]


def handoff_memo(turns, exchanges: int | None = None) -> str:
    """引き継ぎメモの会話の部分: それより前の短いまとめ（LLM を使わない）＋直近 N 往復はそのまま。"""
    exchanges = config.HANDOFF_EXCHANGES if exchanges is None else exchanges
    turns = [t for t in turns if _text(t).strip()]
    keep = max(0, exchanges) * 2
    recent = turns[-keep:] if keep else []
    older = turns[:-keep] if keep else turns
    lines = []
    if older:
        picked, used = [], 0
        for t in reversed(older):                  # 新しい方から上限まで
            if _speaker(t) == "owner":
                line = f"Owner: {' '.join(_text(t).split())[:OLDER_OWNER_CHARS]}"
            else:
                line = f"JARVIS: {_first_sentence(_text(t))}"
            if used + len(line) > OLDER_TOTAL_CHARS:
                break
            picked.append(line)
            used += len(line) + 1
        if picked:
            lines.append("（それより前の会話の短いまとめ。細部は memory_recall で探せる）" if config.HARNESS_TOOLS
                         else "（それより前の会話の短いまとめ）")
            lines.extend(reversed(picked))
            lines.append("（ここから直近の会話）")
    for t in recent:
        who = "Owner" if _speaker(t) == "owner" else "JARVIS"
        lines.append(f"{who}: {_text(t).strip()[:TURN_CHARS]}")
    return "\n".join(lines) or "（なし）"


# ---------------------------------------------------------------- 記憶パック

def _pack_items(pack) -> list[str]:
    if pack is None:
        return []
    items = [i.get("text", "") for i in getattr(pack, "items", []) or [] if i.get("text")]
    if not items and getattr(pack, "text", ""):
        items = [pack.text]
    return items


def _pack_text(pack) -> str:
    return getattr(pack, "text", "") if pack is not None and not isinstance(pack, str) else (pack or "")


# ---------------------------------------------------------------- セッション（T-2404・T-2405）

def _mcp_config(session_id: str) -> dict:
    """Jarvis の MCP サーバーの起動の設定。DB・記憶フォルダなどの場所を明示して渡す（評価・テストで本番を触らない）。"""
    import db
    blocked = set(llm.CODEX_BLOCKED_ENV)
    env = {k: v for k, v in os.environ.items() if k.startswith("JARVIS_") and k not in blocked}
    env.update({"JARVIS_DB_PATH": str(db.DB_PATH), "JARVIS_MEMORY_DIR": str(config.MEMORY_DIR),
                "JARVIS_CKC_DATA_DIR": str(config.CKC_DATA_DIR), "JARVIS_VAULT_ROOT": str(config.VAULT_ROOT),
                "JARVIS_SKILLS_JSON": str(config.SKILLS_JSON), "JARVIS_REGISTRY_JSON": str(config.REGISTRY_JSON),
                "JARVIS_MEMORY": "1" if config.MEMORY_ENABLED else "0",
                "JARVIS_WEB_SEARCH": "1" if config.WEB_SEARCH else "0"})
    for key in ("HOME", "PATH", "LANG"):
        if os.environ.get(key):
            env[key] = os.environ[key]
    script = Path(config.BASE_DIR) / "jarvis_mcp.py"
    return {"mcpServers": {"jarvis": {"command": sys.executable, "args": [str(script), "--session", session_id],
                                      "env": env}}}


def _start(ctx, harness: str):
    """新しいセッションの中身を作る。claude はプロセスを起動、codex はスレッドを作る。"""
    import conversation_tools
    tools = config.HARNESS_TOOLS
    rules = prompts.rules(tools=tools, voice=ctx.voice)
    if tools:
        rules += conversation_tools.guide()
    if harness == "claude":
        return llm.ClaudeSession(rules)    # 実行は共通の承認窓口だけ（spec §3.4.12 J）
    return llm.CodexThread(rules)


def _close(session: Session | None) -> None:
    if session is None:
        return
    try:
        session.handle.close()
    except Exception:  # noqa: BLE001  閉じる失敗で会話を止めない
        pass


def close(session_id: str, keep: str | None = None) -> None:
    """会話のセッションを閉じる（keep のハーネスのものは残す）。会話の終わり・切り替えで呼ぶ。"""
    with _lock:
        session = _sessions.get(session_id)
        if session is None or session.harness == keep:
            return
        del _sessions[session_id]
    _close(session)


def _discard(session: Session) -> None:
    """このセッションだけを閉じる（同じ会話で新しく作られたものは閉じない。裏に残った前の答えの失敗のとき）。"""
    with _lock:
        if _sessions.get(session.session_id) is session:
            del _sessions[session.session_id]
    _close(session)


def close_all() -> None:
    with _lock:
        items = list(_sessions.values())
        _sessions.clear()
    for session in items:
        _close(session)


atexit.register(close_all)


def current(session_id: str) -> Session | None:
    with _lock:
        return _sessions.get(session_id)


def _take(ctx, harness: str) -> Session | None:
    """使い続けられるセッション。ない・違うハーネス・落ちた・長く使っていない・使用中なら閉じて None。"""
    idle = max(0.0, float(config.SESSION_IDLE_MINUTES)) * 60
    with _lock:
        session = _sessions.get(ctx.session_id)
        if session is None:
            return None
        stale = (session.harness != harness or not session.handle.alive()
                 or (idle and clock() - session.last_used > idle) or session.busy.locked())
        if not stale:
            return session
        del _sessions[ctx.session_id]
    _close(session)
    return None


def prewarm(ctx) -> threading.Thread | None:
    """会話の始めに、最初に使うハーネス（claude）のセッションを裏で起動しておく（最初の答えを速くする）。
    発言は送らない（最初の答えで引き継ぎメモ付きの最初のメッセージを送る）。失敗しても何も出さない。"""
    if not enabled() or getattr(ctx, "harness", None) != "claude":
        return None

    def run():
        try:
            with _lock:
                if ctx.session_id in _sessions:
                    return
            handle = _start(ctx, "claude")
            with _lock:
                if ctx.session_id in _sessions:      # 先に答えが始まっていた
                    other = True
                else:
                    _sessions[ctx.session_id] = Session(ctx.session_id, "claude", handle, last_used=clock())
                    other = False
            if other:
                handle.close()
        except Exception:  # noqa: BLE001  起動できなくても会話は始める（最初の答えでもう一度試す）
            pass
    thread = threading.Thread(target=run, name="jarvis-session-prewarm", daemon=True)
    thread.start()
    return thread


def _between(ctx, session: Session) -> str:
    """前の答えの後に、セッションの外で答えた発言（経路 C・スキル・切り替えの命令など）。"""
    history = list(ctx.history or [])[session.synced:]
    own_q, own_a = session.own or (None, None)
    lines, skipped_q, skipped_a = [], False, False
    for t in history:
        text = _text(t).strip()
        if not skipped_q and own_q is not None and _speaker(t) == "owner" and text == own_q.strip():
            skipped_q = True
            continue
        if skipped_q and not skipped_a and own_a and _speaker(t) == "ai" and (own_a.strip()[:40] in text or text[:40] in own_a):
            skipped_a = True
            continue
        who = "Owner" if _speaker(t) == "owner" else "JARVIS"
        lines.append(f"{who}: {text[:TURN_CHARS]}")
    return "\n".join(lines)


def _one_shot(ctx, provider, kind, question, context, name, pack, stream):
    """ollama・予備の 1 回限りの呼び出し: 引き継ぎメモ付きの 1 つのプロンプト（道具なし）。"""
    prompt = prompts.one_shot(kind, question, context, handoff_memo(canonical_turns(ctx)), _pack_text(pack),
                              ctx.voice, name)
    if stream is not None:
        extra = {"cancel": ctx.cancel} if ctx.cancel is not None else {}   # 取り消しのない呼び出しは従来どおり
        return llm.complete_stream(prompt, stream, provider=provider, **extra)
    return llm.complete(prompt, provider=provider)


def answer(ctx, provider: str, kind: str, question: str, owner_text: str, pack=None, context: str | None = None,
           name: str = "default", stream=None) -> str:
    """経路 A（kind="chat"）・B（kind="answer"）の答え。provider は orchestrator.HARNESS_PROVIDERS の名前。
    セッションを持つプロバイダーでは会話のセッションに送り、それ以外は 1 回限りの呼び出し。使った道具は ctx.llm_tools に。"""
    ctx.llm_tools = []
    harness = SESSION_PROVIDERS.get(provider)
    if harness is None or not enabled():
        close(ctx.session_id)                      # 別のハーネスで答える。前のセッションは閉じる（引き継ぎ）
        return _one_shot(ctx, provider, kind, question, context, name, pack, stream)
    close(ctx.session_id, keep=harness)            # ハーネスが変わった → 前のセッションを閉じる
    turn = prompts.answer_turn(question, context or "（なし）", name) if kind == "answer" else prompts.chat_turn(question)
    items = _pack_items(pack)
    for attempt in (1, 2):                         # 送る前に使えなくなったときは、1 回だけ作り直す
        session = _take(ctx, harness)
        fresh = session is None or not session.asked
        created = session is None
        if session is None:
            try:
                handle = _start(ctx, harness)
            except llm.CodexSessionUnavailable:
                return _one_shot(ctx, "codex", kind, question, context, name, pack, stream)   # codex exec で答える
            session = Session(ctx.session_id, harness, handle, last_used=clock())
            with _lock:
                old = _sessions.get(ctx.session_id)
                _sessions[ctx.session_id] = session
            if old is not None and old is not session:
                _close(old)
        tools = config.HARNESS_TOOLS
        if fresh:
            message = prompts.first_message(handoff_memo(canonical_turns(ctx)), _pack_text(pack), turn, tools)
            new_items = set(items)
        else:
            unsent = [i for i in items if i not in session.sent_memory]
            message = prompts.next_message(_between(ctx, session), "\n".join(unsent), turn, tools)
            new_items = set(unsent)
        latency.set("warm", not created)          # 使い続けた・前もって起動したもの（True）か、その場で起動した（False）
        if not session.busy.acquire(blocking=False):
            _discard(session)
            continue
        sent = []

        def track(text, _sent=sent):
            if text:
                _sent.append(True)
            if stream is not None:
                stream(text)
        try:
            if tools:
                import conversation_tools
                text, used = conversation_tools.answer(session.handle, message, ctx, track)
            else:
                text, used = session.handle.ask(message, track, cancel=ctx.cancel)
        except llm.CodexSessionUnavailable:
            session.busy.release()
            _discard(session)
            if sent or ctx.llm_tools:
                raise llm.LLMError("Codex のセッションが答えの途中で使えなくなりました") from None
            if attempt == 1:
                continue
            return _one_shot(ctx, "codex", kind, question, context, name, pack, stream)
        except llm.ClaudeSessionLost:
            session.busy.release()
            _discard(session)
            if sent or ctx.llm_tools or attempt == 2:
                raise
            continue
        except BaseException:
            session.busy.release()
            _discard(session)                      # 取り消し・時間切れ・エラー → 次の答えで作り直す
            raise
        session.busy.release()
        session.asked = True
        session.synced = len(ctx.history or [])
        session.own = (owner_text, text)
        session.sent_memory |= new_items
        session.last_used = clock()
        ctx.llm_tools = used
        return text
    raise llm.LLMError("ハーネスのセッションを作れませんでした")
