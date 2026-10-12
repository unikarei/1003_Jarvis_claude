"""T-104 の検出・誤検出・漏えい防止をダミー値で確認する。"""

from dataclasses import FrozenInstanceError

import pytest

from ckc.secrets import SecretFinding, find_secrets, mask_secrets


_DUMMY_KEY = "-----BEGIN RSA PRIVATE KEY-----\n" + "dummy" * 10 + "\n-----END RSA PRIVATE KEY-----"
_CASES = [
    ("anthropic_api_key", "sk-ant-" + "x" * 40),
    ("openai_api_key", "sk-" + "x" * 40),
    ("openai_api_key", "sk-proj-" + "x" * 40),
    *[("github_token", prefix + "x" * 36) for prefix in ("ghp_", "gho_", "ghu_", "ghs_", "ghr_")],
    ("github_token", "github_pat_" + "x" * 60),
    *[("aws_access_key", prefix + "X" * 16) for prefix in ("AKIA", "ASIA")],
    ("google_api_key", "AIza" + "x" * 35),
    ("private_key", _DUMMY_KEY),
    *[("slack_token", prefix + "1234567890-" + "x" * 24)
      for prefix in ("xoxb-", "xoxp-", "xoxa-", "xoxr-", "xoxs-")],
    ("jwt", "eyJ" + "x" * 12 + "." + "y" * 12 + "." + "z" * 12),
]


@pytest.mark.parametrize("kind,value", _CASES)
def test_detects_each_kind_and_masks_body(kind, value):
    text = "前置き\n" + value + "\n後書き"
    findings = find_secrets(text)
    assert len(findings) == 1
    finding = findings[0]
    assert finding.kind == kind
    assert text[finding.start:finding.end] == value
    assert finding.start == len("前置き\n")
    assert finding.preview == value[:4] + "*" * min(len(value) - 4, 8)
    assert len(finding.preview) <= 12
    assert value not in finding.preview
    assert mask_secrets(text) == f"前置き\n[REDACTED:{kind}]\n後書き"
    assert value not in mask_secrets(text)


@pytest.mark.parametrize("name", ["password", "passwd", "secret", "api_key", "apikey", "token", "パスワード", "PASSWORD"])
@pytest.mark.parametrize("separator", ["=", ":"])
@pytest.mark.parametrize("quote", ["", "'", '"'])
def test_assignment_only_captures_value(name, separator, quote):
    value = "dummy" * 3
    prefix = f"{quote}{name}{quote} {separator} {quote}"
    text = prefix + value + quote
    assert find_secrets(text) == [SecretFinding("password_assignment", len(prefix), len(prefix) + len(value), "dumm********")]
    assert mask_secrets(text) == prefix + "[REDACTED:password_assignment]" + quote


@pytest.mark.parametrize("text", [
    "", "普通の日本語。パスワードを変更した。token の数を数える。",
    "Change the password and count tokens.", "https://example.com/path?x=1",
    "a" * 40, "12345678-1234-1234-1234-123456789abc", "sk-1",
    "password=short", "token='short'", "password_hash=" + "x" * 20,
    "mysecret=" + "x" * 20,
])
def test_does_not_detect_ordinary_text(text):
    assert find_secrets(text) == []
    assert mask_secrets(text) == text


def test_multiple_secrets_are_in_text_order():
    values = ["AIza" + "x" * 35, "sk-ant-" + "y" * 40, "ghp_" + "z" * 36]
    text = "\n".join(values)
    findings = find_secrets(text)
    assert [finding.kind for finding in findings] == ["google_api_key", "anthropic_api_key", "github_token"]
    assert [text[f.start:f.end] for f in findings] == values
    assert all(a.end <= b.start for a, b in zip(findings, findings[1:]))


def test_specific_kind_wins_assignment_overlap_and_masks_union():
    value = "sk-ant-" + "x" * 40
    text = "token=" + value + ",tail"
    findings = find_secrets(text)
    assert len(findings) == 1
    assert findings[0].kind == "anthropic_api_key"
    assert text[findings[0].start:findings[0].end] == value + ",tail"
    assert mask_secrets(text) == "token=[REDACTED:anthropic_api_key]"


def test_private_key_contains_other_secret_and_masks_entire_block():
    text = "-----BEGIN PRIVATE KEY-----\n" + "sk-" + "x" * 40 + "\n-----END PRIVATE KEY-----"
    assert len(find_secrets(text)) == 1
    assert find_secrets(text)[0].kind == "private_key"
    assert mask_secrets(text) == "[REDACTED:private_key]"


def test_assignment_masks_entire_value_with_key_like_suffix():
    key = "sk-" + "x" * 40
    value = "dummy" * 3
    text = "password=" + value + key
    # キーのような末尾だけでなく、代入値全体を伏せる。
    assert mask_secrets(text) == "password=[REDACTED:password_assignment]"


def test_finding_is_frozen():
    finding = SecretFinding("password_assignment", 0, 8, "dumm****")
    with pytest.raises(FrozenInstanceError):
        finding.kind = "jwt"
