"""T-105 の往復変換と不正入力を確認する。"""

import json
from dataclasses import replace

import pytest

from ckc.models import Confidence, Evidence, Kind, KnowledgeNote, NoteStatus, Origin, SourceRef, Speaker
from ckc.render import note_filename, note_relpath, parse_note, render_note


def _note(**changes):
    note = KnowledgeNote(
        "ckc_dummy", Kind.DECISION, NoteStatus.ACTIVE, '日本語: "題名"\n---',
        '本文: "引用"\n---\n\n## 根拠\n\n本文中の見出し\n# その他',
        ["大洗", 'project:"x"'], None, Confidence.CONFIRMED,
        SourceRef(Origin.PASTE, "conv_dummy", '会話:\n---\n## 根拠\n"題"', "2026-10-12T12:34:56+09:00", None),
        [Evidence('引用:\n"内容"\n---', Speaker.OWNER), Evidence("了解\n\n続き", Speaker.AI)],
        "2026-10-12T12:34:56+09:00", "2026-10-12T13:00:00+09:00",
    )
    return replace(note, **changes)


@pytest.mark.parametrize("kind", list(Kind))
@pytest.mark.parametrize("status", list(NoteStatus))
@pytest.mark.parametrize("confidence", list(Confidence))
def test_roundtrip_enums(kind, status, confidence):
    note = _note(kind=kind, status=status, confidence=confidence)
    assert parse_note(render_note(note)) == note


@pytest.mark.parametrize("origin", list(Origin))
@pytest.mark.parametrize("supersedes", [[], ["ckc_a", "ckc_b"]])
def test_roundtrip_optional_fields(origin, supersedes):
    note = _note(category="カテゴリ", supersedes=supersedes, superseded_by=["ckc_next"])
    note.source = replace(note.source, origin=origin, occurred_at="2026-10-11T00:00:00Z")
    assert parse_note(render_note(note)) == note


def test_frontmatter_order_and_json_values():
    markdown = render_note(_note())
    lines = markdown[4:].split("\n---\n", 1)[0].splitlines()
    keys = [line.partition(": ")[0] for line in lines]
    assert keys == ["ckc_id", "kind", "status", "title", "projects", "category", "confidence", "source", "evidence", "supersedes", "superseded_by", "created", "approved", "tags"]
    values = dict((key, json.loads(value)) for key, _, value in (line.partition(": ") for line in lines))
    assert values["tags"] == ["ckc", "ckc/decision"]
    assert values["source"]["origin"] == "paste"
    assert values["evidence"][0]["speaker"] == "owner"
    assert '\\n' in lines[3]


def test_body_edits_are_read_without_changing_metadata():
    note = _note()
    edited_body = "変更した本文\n---\n\n## 根拠\n\n本文の見出し"
    markdown = render_note(note).replace(note.body, edited_body)
    assert parse_note(markdown) == replace(note, body=edited_body)


def test_multiline_quotes_and_source_date():
    markdown = render_note(_note())
    assert '> 引用:\n> "内容"\n> ---\n> — Owner' in markdown
    assert '> 了解\n> \n> 続き\n> — AI' in markdown
    assert "paste、2026-10-12T12:34:56+09:00" in markdown


def test_body_whitespace_is_stripped_and_empty_evidence_roundtrips():
    note = _note(body="本文", evidence=[])
    assert parse_note(render_note(note)) == note
    assert parse_note(render_note(replace(note, body=" \n本文\n "))).body == "本文"
    assert parse_note(render_note(replace(note, body=""))) == replace(note, body="")


@pytest.mark.parametrize("title,limit,expected", [
    (' /\\:*?"<>|#[題名]^\x00\x1f\x7f .', 60, "題名"),
    ("abcdef", 3, "abc"), ("", 60, "untitled"), ("...  ", 60, "untitled"),
    ("abc. xyz", 4, "abc"), ("日本語題名", 3, "日本語"), ("abc", 0, "untitled"),
    ("\u3000. 題名 .\u3000", 60, "題名"),
])
def test_safe_filename(title, limit, expected):
    assert note_filename("2026-10-12T12:34:56+09:00", title, limit) == f"2026-10-12_{expected}.md"


@pytest.mark.parametrize("kind", list(Kind))
def test_relative_path(kind):
    assert note_relpath(_note(kind=kind), "test.md") == f"_jarvis/knowledge/{kind.value}/test.md"


def _change_field(markdown, key, value):
    lines = markdown.splitlines()
    index = next(i for i, line in enumerate(lines) if line.startswith(key + ": "))
    lines[index] = key + ": " + json.dumps(value, ensure_ascii=False)
    return "\n".join(lines) + "\n"


@pytest.mark.parametrize("field,value", [
    ("ckc_id", None), ("title", 3), ("projects", "wrong"), ("projects", [1]),
    ("category", []), ("kind", "Unknown"), ("status", "pending"), ("confidence", "high"),
    ("source", []), ("source", {}), ("evidence", {}), ("evidence", [1]),
    ("evidence", [{}]), ("evidence", [{"quote": 4, "speaker": "owner"}]),
    ("evidence", [{"quote": "x", "speaker": "system"}]),
    ("supersedes", [None]), ("superseded_by", {}), ("created", 1), ("approved", False),
])
def test_invalid_types_and_enums_raise_value_error(field, value):
    with pytest.raises(ValueError):
        parse_note(_change_field(render_note(_note()), field, value))


@pytest.mark.parametrize("field,value", [("origin", "unknown"), ("conversation_id", 1), ("title", None), ("captured_at", []), ("occurred_at", {})])
def test_invalid_nested_source_fields(field, value):
    source = {"origin": "paste", "conversation_id": "dummy", "title": "t", "captured_at": "date", "occurred_at": None}
    source[field] = value
    with pytest.raises(ValueError):
        parse_note(_change_field(render_note(_note()), "source", source))


@pytest.mark.parametrize("markdown", ["# plain", "---\nwrong\n---\n", "---\nckc_id: invalid\n---\n", "---\nckc_id: 1\n", "---\n---\n"])
def test_malformed_frontmatter(markdown):
    with pytest.raises(ValueError):
        parse_note(markdown)


def test_missing_required_field_and_duplicate_key():
    markdown = render_note(_note())
    missing = "\n".join(line for line in markdown.split("\n") if not line.startswith("approved: "))
    with pytest.raises(ValueError):
        parse_note(missing)
    with pytest.raises(ValueError):
        parse_note(markdown.replace("---\n", '---\nckc_id: "duplicate"\n', 1))


def test_tags_are_ignored():
    markdown = _change_field(render_note(_note()), "tags", {"arbitrary": True})
    assert parse_note(markdown) == _note()


def test_renamed_heading_in_obsidian_still_reads_body():
    """Owner が Obsidian で見出しを書き換えても本文を読める（題名は frontmatter を正とする）。"""
    note = _note(title="元の題名", body="本文の内容")
    edited = render_note(note).replace("\n# 元の題名\n", "\n# 書き換えた見出し\n")
    parsed = parse_note(edited)
    assert (parsed.title, parsed.body) == ("元の題名", "本文の内容")


def test_unverified_notice_visible_and_roundtrips_once():
    note = _note(confidence=Confidence.UNVERIFIED)
    markdown = render_note(note)
    assert '※ 未検証の知識です' in markdown
    assert parse_note(markdown) == note
    assert render_note(parse_note(markdown)) == markdown
