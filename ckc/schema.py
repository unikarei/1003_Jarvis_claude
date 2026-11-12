"""LLM が抽出した知識候補の形と会話の根拠を検証する。"""

import re
from dataclasses import dataclass

from ckc.models import Confidence, Conversation, Evidence, Kind, Speaker


MAX_ITEMS = 20
MAX_TITLE_LEN = 80


@dataclass
class ValidatedItem:
    """会話の根拠と必須項目を確認した知識候補。"""

    kind: Kind
    title: str
    body: str
    projects: list[str]
    category: str | None
    confidence: Confidence
    evidence: list[Evidence]


@dataclass
class ValidationResult:
    """合格候補、不合格の位置と理由、処理の警告。"""

    items: list[ValidatedItem]
    rejected: list[tuple[int, list[str]]]
    warnings: list[str]


def _normalize(text: str) -> str:
    return re.sub(r"\s+", "", text)


def _enum(value, enum, message, reasons):
    if isinstance(value, str):
        try:
            return enum(value)
        except ValueError:
            pass
    reasons.append(message)
    return None


def _validate_item(item, turns, known_projects):
    """1件の違反を集め、ほかの候補に影響させず検証する。"""
    reasons = []
    if not isinstance(item, dict):
        return None, ["候補がオブジェクトではない"], None
    kind = _enum(item.get("kind"), Kind, "種類が6種類のいずれでもない", reasons)
    confidence = _enum(item.get("confidence"), Confidence, "confidence が confirmed / unverified ではない", reasons)
    title = item.get("title")
    if not isinstance(title, str) or not title.strip():
        reasons.append("題名が空、または文字列ではない")
    else:
        title = title.strip()
        if len(title) > MAX_TITLE_LEN:
            reasons.append(f"題名が {MAX_TITLE_LEN} 文字を超えている")
    body = item.get("body")
    if not isinstance(body, str) or not body.strip():
        reasons.append("本文が空、または文字列ではない")
    projects = item.get("projects")
    if not isinstance(projects, list) or any(not isinstance(project, str) for project in projects):
        reasons.append("projects が文字列の配列ではない")
    else:
        for project in projects:
            if project not in known_projects:
                reasons.append(f"未知のプロジェクト ID: {project}")
    category = item.get("category")
    if category is not None and not isinstance(category, str):  # 省略は null と同じに扱う
        reasons.append("category が文字列または null ではない")
    evidence = []
    raw_evidence = item.get("evidence")
    if not isinstance(raw_evidence, list) or not raw_evidence:
        reasons.append("根拠が1件以上の配列ではない")
    else:
        for position, raw in enumerate(raw_evidence):
            if not isinstance(raw, dict):
                reasons.append(f"根拠 {position + 1} がオブジェクトではない")
                continue
            quote = raw.get("quote")
            speaker = _enum(raw.get("speaker"), Speaker, f"根拠 {position + 1} の話者が owner / ai ではない", reasons)
            if not isinstance(quote, str) or not quote.strip():
                reasons.append(f"根拠 {position + 1} の引用が空、または文字列ではない")
                continue
            normalized_quote = _normalize(quote)
            matching_speakers = [turn_speaker for turn_speaker, text in turns if normalized_quote in text]
            if not matching_speakers:
                reasons.append(f"引用が会話の中にない: {quote[:20]}")
            elif speaker is not None and speaker not in matching_speakers:
                reasons.append(f"引用の話者が会話の発言と違う: {quote[:20]}")
            elif speaker is not None:
                evidence.append(Evidence(quote, speaker))
    if kind in (Kind.DECISION, Kind.PREFERENCE) and not any(e.speaker == Speaker.OWNER for e in evidence):
        reasons.append(f"{kind.value} に Owner の発言の根拠がない")
    # 発言にあるというだけでは実測にならない。AI だけのシステム状態は
    # Task/Hypothesis 等への言い換えでも長期知識へ持ち込まない（T-2303）。
    if evidence and all(e.speaker == Speaker.AI for e in evidence):
        statement = " ".join([str(title), str(body)] + [e.quote for e in evidence])
        if (re.search(r"ツール|接続|連携|権限|保存|記録|Obsidian|オブシ[デリ]ィ?アン|ナレッジ|Jarvis|ジャービス", statement, re.I)
                and re.search(r"無効|有効|未接続|未設定|設定済|でき[なまる]|出来|使え|保存され|接続され|失敗|利用不可", statement)):
            reasons.append("システムの接続・権限・保存状態が AI の発言だけを根拠としている")
    if reasons:
        return None, reasons, None
    warning = None
    if kind == Kind.FINDING and confidence == Confidence.CONFIRMED and all(e.speaker == Speaker.AI for e in evidence):
        confidence = Confidence.UNVERIFIED
        warning = "Finding の根拠が AI の発言だけなので confidence を unverified に補正した"
    return ValidatedItem(kind, title, body, list(projects), category, confidence, evidence), [], warning


def validate_extraction(data: object, conversation: Conversation, known_projects: set[str]) -> ValidationResult:
    """全体の形式を確認し、候補を個別に検証する。不正な全体形式は ValueError。"""
    if not isinstance(data, dict) or not isinstance(data.get("items"), list):
        raise ValueError("抽出結果は items 配列を持つオブジェクトである必要がある")
    warnings = data.get("warnings", [])
    if not isinstance(warnings, list) or any(not isinstance(warning, str) for warning in warnings):
        raise ValueError("warnings は文字列の配列である必要がある")
    result = ValidationResult([], [], list(warnings))
    if len(data["items"]) > MAX_ITEMS:
        result.warnings.append(f"候補が {MAX_ITEMS} 件を超えたため、先頭 {MAX_ITEMS} 件だけを検証した")
    turns = [(turn.speaker, _normalize(turn.text)) for turn in conversation.turns]
    for position, raw in enumerate(data["items"][:MAX_ITEMS]):
        item, reasons, warning = _validate_item(raw, turns, known_projects)
        if reasons:
            result.rejected.append((position, reasons))
        else:
            result.items.append(item)
            if warning:
                result.warnings.append(f"候補 {position + 1}: {warning}")
    return result
