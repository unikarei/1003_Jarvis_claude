"""会話に含まれる秘密情報の検出と表示用マスク。"""

import re
from dataclasses import dataclass


# 順番は重複時の優先順位。秘密鍵全体と具体的な種類を優先する。
_PATTERNS = (
    ("private_key", re.compile(
        r"-----BEGIN (?P<label>(?:[A-Z0-9]+ )*PRIVATE KEY)-----"
        r"[\s\S]*?-----END (?P=label)-----")),
    ("anthropic_api_key", re.compile(r"(?<![A-Za-z0-9_-])sk-ant-[A-Za-z0-9_-]{20,}")),
    ("openai_api_key", re.compile(
        r"(?<![A-Za-z0-9_-])(?=[A-Za-z0-9_-]{20,}(?![A-Za-z0-9_-]))"
        r"sk-(?!ant-)(?:proj-)?[A-Za-z0-9_-]+")),
    ("github_token", re.compile(
        r"(?<![A-Za-z0-9_])(?:gh[pousr]_[A-Za-z0-9]{36,}|github_pat_[A-Za-z0-9_]{20,})")),
    ("aws_access_key", re.compile(r"(?<![A-Za-z0-9_])(?:AKIA|ASIA)[A-Z0-9]{16}(?![A-Za-z0-9_])")),
    ("google_api_key", re.compile(r"(?<![A-Za-z0-9_-])AIza[A-Za-z0-9_-]{35}(?![A-Za-z0-9_-])")),
    ("slack_token", re.compile(r"(?<![A-Za-z0-9_-])xox[bpars]-[A-Za-z0-9_-]{8,}")),
    ("jwt", re.compile(
        r"(?<![A-Za-z0-9_.-])eyJ[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+"
        r"(?![A-Za-z0-9_.-])")),
    ("password_assignment", re.compile(
        r"(?<!\w)(?:password|passwd|secret|api_key|apikey|token|パスワード)"
        r"(?!\w)[\"']?\s*[=:]\s*"
        r"(?:\"(?P<double>[^\"\s]{8,})\"|'(?P<single>[^'\s]{8,})'|(?P<bare>[^\s\"']{8,}))",
        re.IGNORECASE)),
)


@dataclass(frozen=True)
class SecretFinding:
    """検出範囲と、秘密の先頭4文字だけを残した表示用文字列。"""

    kind: str
    start: int
    end: int
    preview: str


def find_secrets(text: str) -> list[SecretFinding]:
    """秘密情報を出現順に返す。重なる検出は全範囲をまとめる。"""
    matches = []
    for priority, (kind, pattern) in enumerate(_PATTERNS):
        for match in pattern.finditer(text):
            if kind == "password_assignment":
                group = next(name for name in ("double", "single", "bare")
                             if match.group(name) is not None)
                start, end = match.span(group)
            else:
                start, end = match.span()
            matches.append((start, end, priority, kind))

    merged = []
    for start, end, priority, kind in sorted(matches):
        if merged and start < merged[-1][1]:
            previous_start, previous_end, previous_priority, previous_kind = merged[-1]
            if previous_priority < priority:
                priority, kind = previous_priority, previous_kind
            merged[-1] = (previous_start, max(previous_end, end), priority, kind)
        else:
            merged.append((start, end, priority, kind))

    return [SecretFinding(kind, start, end,
                          text[start:start + 4] + "*" * min(end - start - 4, 8))
            for start, end, _, kind in merged]


def mask_secrets(text: str) -> str:
    """検出範囲を種類付きの伏せ字に置き換える。"""
    parts = []
    cursor = 0
    for finding in find_secrets(text):
        parts.extend((text[cursor:finding.start], f"[REDACTED:{finding.kind}]"))
        cursor = finding.end
    parts.append(text[cursor:])
    return "".join(parts)
