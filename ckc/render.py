"""CKC ノートの Markdown 生成と読み取り。"""

import json
import re
from dataclasses import asdict
from datetime import datetime

from ckc.models import Confidence, Evidence, Kind, KnowledgeNote, NoteStatus, Origin, SourceRef, Speaker


_FIELDS = (
    "ckc_id", "kind", "status", "title", "projects", "category", "confidence",
    "source", "evidence", "supersedes", "superseded_by", "created", "approved", "tags",
)
_FORBIDDEN = re.compile(r'[/\\:*?"<>|#\[\]^\x00-\x1f\x7f-\x9f]')


def _footer(note: KnowledgeNote) -> str:
    """根拠と出典の本文を組み立てる。"""
    quotes = []
    for evidence in note.evidence:
        quote = "\n".join("> " + line for line in evidence.quote.split("\n"))
        speaker = "Owner" if evidence.speaker == Speaker.OWNER else "AI"
        quotes.append(f"{quote}\n> — {speaker}")
    source = note.source
    date = source.occurred_at or source.captured_at
    return ("\n\n## 根拠\n\n" + "\n\n".join(quotes)
            + f"\n\n## 出典\n\n- 会話: {source.title}（{source.origin.value}、{date}）"
            + f"\n- 会話 ID: {source.conversation_id}\n")


def render_note(note: KnowledgeNote) -> str:
    """JSON 値の frontmatter を付けた Markdown を返す。"""
    values = asdict(note)
    values["tags"] = ["ckc", "ckc/" + note.kind.value.lower()]
    lines = [f"{key}: {json.dumps(values[key], ensure_ascii=False)}" for key in _FIELDS]
    return "---\n" + "\n".join(lines) + f"\n---\n\n# {note.title}\n\n{note.body}" + _footer(note)


def _string(value):
    if not isinstance(value, str):
        raise ValueError("文字列の項目が不正です")
    return value


def _optional_string(value):
    return None if value is None else _string(value)


def _strings(value):
    if not isinstance(value, list):
        raise ValueError("配列の項目が不正です")
    return [_string(item) for item in value]


def parse_note(markdown: str) -> KnowledgeNote:
    """属性と編集可能な本文からノートを復元する。不正な形式は ValueError。"""
    try:
        if not isinstance(markdown, str) or not markdown.startswith("---\n"):
            raise ValueError("frontmatter がありません")
        metadata, separator, content = markdown[4:].partition("\n---\n")
        if not separator:
            raise ValueError("frontmatter が閉じられていません")
        values = {}
        for line in metadata.split("\n"):
            key, separator, value = line.partition(": ")
            if not separator or key in values:
                raise ValueError("frontmatter の項目が不正です")
            values[key] = json.loads(value)
        if any(key not in values for key in _FIELDS if key != "tags"):
            raise ValueError("必須項目がありません")
        source = values["source"]
        if not isinstance(source, dict):
            raise ValueError("出典が不正です")
        source_ref = SourceRef(
            Origin(_string(source["origin"])), _string(source["conversation_id"]),
            _string(source["title"]), _string(source["captured_at"]),
            _optional_string(source["occurred_at"]),
        )
        if not isinstance(values["evidence"], list):
            raise ValueError("根拠が不正です")
        evidence = []
        for item in values["evidence"]:
            if not isinstance(item, dict):
                raise ValueError("根拠が不正です")
            evidence.append(Evidence(_string(item["quote"]), Speaker(_string(item["speaker"]))))
        note = KnowledgeNote(
            ckc_id=_string(values["ckc_id"]), kind=Kind(_string(values["kind"])),
            status=NoteStatus(_string(values["status"])), title=_string(values["title"]),
            body="", projects=_strings(values["projects"]), category=_optional_string(values["category"]),
            confidence=Confidence(_string(values["confidence"])), source=source_ref, evidence=evidence,
            supersedes=_strings(values["supersedes"]), superseded_by=_strings(values["superseded_by"]),
            created=_string(values["created"]), approved=_string(values["approved"]),
        )
        header = f"\n# {note.title}\n\n"
        if not content.startswith(header):
            raise ValueError("本文の題名が不正です")
        content = content[len(header):]
        footer = _footer(note)
        # 生成した末尾を先に照合し、本文内の同じ見出しを保持する。
        if content.endswith(footer):
            note.body = content[:-len(footer)].strip()
        else:
            body, separator, _ = content.rpartition("\n\n## 根拠\n")
            if not separator:
                raise ValueError("根拠の見出しがありません")
            note.body = body.strip()
        return note
    except (KeyError, TypeError, json.JSONDecodeError) as error:
        raise ValueError("ノートの形式が不正です") from error


def note_filename(created: str, title: str, max_title_len: int = 60) -> str:
    """作成日と安全な題名を組み合わせたファイル名を返す。"""
    date = datetime.fromisoformat(created).date().isoformat()
    title = re.sub(r"^[\s.]+|[\s.]+$", "", _FORBIDDEN.sub("", title))
    title = re.sub(r"^[\s.]+|[\s.]+$", "", title[:max(0, max_title_len)]) or "untitled"
    return f"{date}_{title}.md"


def note_relpath(note: KnowledgeNote, filename: str) -> str:
    """Vault 内の CKC ノートの相対パスを返す。"""
    return f"_jarvis/knowledge/{note.kind.value}/{filename}"
