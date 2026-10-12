"""会話の取り込み（Capture）: 貼り付け・テキストファイル・Jarvis の対話を Conversation にして保存する。

取り込みの前に秘密情報を検出し、見つかったら保存も LLM への送信もせずに止める（../docs/spec.md §3.3.5）。
同じ内容の会話は二重に取り込まない（既存の会話 ID を返す）。
"""

import re
from dataclasses import dataclass
from pathlib import Path

from ckc import store
from ckc.models import Conversation, Origin, Speaker, Turn, new_id, now_iso
from ckc.secrets import find_secrets

# 行頭の話者の表記（../docs/tasks.md §1.3 の取り込み形式）
_SPEAKER_LINE = re.compile(
    r"^\s*(owner|私|user|あなた|ai|assistant|claude|chatgpt|jarvis)\s*[:：>]\s?(.*)$", re.IGNORECASE)
_OWNER_LABELS = {"owner", "私", "user", "あなた"}


class CaptureError(Exception):
    """取り込みを中止した（秘密情報の検出など）。何も保存していない。"""


@dataclass
class CaptureResult:
    conversation_id: str
    is_new: bool


def parse_turns(text: str) -> list[Turn]:
    """話者の表記で発言に分ける。表記がなければ全体を Owner の発言1つとして扱う。"""
    turns: list[Turn] = []
    for line in text.replace("\r\n", "\n").replace("\r", "\n").split("\n"):
        m = _SPEAKER_LINE.match(line)
        if m:
            speaker = Speaker.OWNER if m.group(1).lower() in _OWNER_LABELS else Speaker.AI
            turns.append(Turn(speaker, m.group(2)))
        elif turns:
            turns[-1].text += "\n" + line
        elif line.strip():
            turns.append(Turn(Speaker.OWNER, line))
    for t in turns:
        t.text = t.text.strip()
    return [t for t in turns if t.text]


def build_conversation(turns: list[Turn], origin: Origin, title: str | None = None,
                       occurred_at: str | None = None, external_id: str | None = None) -> Conversation:
    if not turns:
        raise CaptureError("会話の本文が空です")
    title = title or turns[0].text.splitlines()[0][:40]
    return Conversation(new_id("conv"), origin, title, turns, now_iso(), occurred_at, external_id)


def capture(conv: Conversation) -> CaptureResult:
    """秘密情報を確認してから会話を保存する。取り込み済みなら既存の ID を返す。"""
    findings = find_secrets(conv.text())
    if findings:
        kinds = ", ".join(sorted({f.kind for f in findings}))
        raise CaptureError(f"秘密情報の可能性があるため取り込みを中止しました（{kinds}: "
                           f"{', '.join(f.preview for f in findings[:3])}）。該当部分を消してから再実行してください。")
    existing = store.find_conversation_by_hash(conv.content_hash())
    if existing:
        return CaptureResult(existing, False)
    store.save_conversation(conv)
    return CaptureResult(conv.conversation_id, True)


def capture_text(text: str, title: str | None = None, origin: Origin = Origin.PASTE) -> CaptureResult:
    return capture(build_conversation(parse_turns(text), origin, title))


def capture_file(path: Path, title: str | None = None) -> CaptureResult:
    if path.suffix.lower() not in (".txt", ".md"):
        raise CaptureError(f"対応していないファイル形式です（.txt / .md）: {path.name}")
    return capture_text(path.read_text(encoding="utf-8"), title or path.stem, Origin.FILE)
