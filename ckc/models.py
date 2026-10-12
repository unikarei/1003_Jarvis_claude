"""CKC のデータ型（../docs/tasks.md §1.3 の契約）。

項目名・列挙の値を変えるときは、tasks.md と tests/ckc/test_contract.py を同時に変える
（Codex が並行して実装するモジュールがこの契約に依存する）。
"""

import hashlib
import uuid
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum


class Kind(str, Enum):
    """知識の種類（../docs/spec.md §3.3.3）。"""
    DECISION = "Decision"
    PREFERENCE = "Preference"
    HYPOTHESIS = "Hypothesis"
    FINDING = "Finding"
    TASK = "Task"
    QUESTION = "Question"


class Speaker(str, Enum):
    OWNER = "owner"
    AI = "ai"


class Confidence(str, Enum):
    CONFIRMED = "confirmed"
    UNVERIFIED = "unverified"


class CandidateStatus(str, Enum):
    """候補の状態（jarvis.db）。"""
    PENDING = "pending"
    APPROVED = "approved"
    REJECTED = "rejected"
    PUBLISHED = "published"


class NoteStatus(str, Enum):
    """Vault のノートの状態。"""
    ACTIVE = "active"
    SUPERSEDED = "superseded"


class Origin(str, Enum):
    """会話の取得元。"""
    JARVIS = "jarvis"
    PASTE = "paste"
    FILE = "file"
    CHATGPT = "chatgpt"
    CLAUDE = "claude"


def new_id(prefix: str) -> str:
    """'<prefix>_<YYYYmmdd_HHMMSS>_<4桁16進>' の ID を作る（例: conv_20261012_153000_a1b2）。"""
    return f"{prefix}_{datetime.now().strftime('%Y%m%d_%H%M%S')}_{uuid.uuid4().hex[:4]}"


def now_iso() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


def _normalize(text: str) -> str:
    return text.replace("\r\n", "\n").replace("\r", "\n").strip()


@dataclass
class Turn:
    speaker: Speaker
    text: str
    timestamp: str | None = None


@dataclass
class Conversation:
    conversation_id: str
    origin: Origin
    title: str
    turns: list[Turn]
    captured_at: str
    occurred_at: str | None = None
    external_id: str | None = None

    def text(self) -> str:
        """全発言を 'owner: ...' / 'ai: ...' の形で連結する（LLM に渡す本文）。"""
        return "\n".join(f"{t.speaker.value}: {_normalize(t.text)}" for t in self.turns)

    def content_hash(self) -> str:
        """話者と本文から SHA-256 を計算する。前後の空白・改行コードの違いでは変わらない。"""
        payload = "\x1e".join(f"{t.speaker.value}\x1f{_normalize(t.text)}" for t in self.turns)
        return hashlib.sha256(payload.encode("utf-8")).hexdigest()


@dataclass
class Evidence:
    quote: str
    speaker: Speaker


@dataclass
class SourceRef:
    origin: Origin
    conversation_id: str
    title: str
    captured_at: str
    occurred_at: str | None = None


@dataclass
class Candidate:
    """承認前の候補。"""
    candidate_id: str
    conversation_id: str
    kind: Kind
    title: str
    body: str
    projects: list[str]
    category: str | None
    confidence: Confidence
    evidence: list[Evidence]
    status: CandidateStatus = CandidateStatus.PENDING
    resource_id: str | None = None
    similar: list[str] = field(default_factory=list)


@dataclass
class KnowledgeNote:
    """Vault に保存する知識（../docs/spec.md §3.3.9）。"""
    ckc_id: str
    kind: Kind
    status: NoteStatus
    title: str
    body: str
    projects: list[str]
    category: str | None
    confidence: Confidence
    source: SourceRef
    evidence: list[Evidence]
    created: str
    approved: str
    supersedes: list[str] = field(default_factory=list)
    superseded_by: list[str] = field(default_factory=list)
