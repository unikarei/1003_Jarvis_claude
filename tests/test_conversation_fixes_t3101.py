"""2026-11-20 の会話でかみ合わなかった点の修正（T-3101、ワークスペースの testing §29）。"""
import pytest

import orchestrator as orc
import skills
from voice.speak import speech_summary


def _s(name, phrases):
    return skills.Skill(name=name, description=name, status="active", permission="read", entry={},
                        inputs={"query": {"type": "string", "required": True}}, phrases=tuple(phrases))


SEARCH = {"knowledge-search": _s("knowledge-search", ["について知ってること", "を探して", "を検索して"])}


REPORT = """ナレッジの接続状況（実測・読み取りのみ）
スキル knowledge-answer: 有効
検索DB: 読み取り成功（知識 1234 件）
oarai-obsidian: 存在・Git操作中の印なし / 検索DBの知識 800 件
jarvis-knowledge: Vault がありません / 検索DBの知識 未確認 件
CKC保存待ち: 36 件
会話の記憶: 有効（設定値）"""


def test_connection_report_is_spoken_with_contents():
    spoken = speech_summary("knowledge-stats", REPORT)
    assert spoken == ("検索DBは知識 1234 件。oarai obsidianは使えます、800 件。"
                      "jarvis knowledgeはVault がありません、件数は未確認。保存待ちは 36 件です。")


def test_connection_report_unknown_db():
    text = "ナレッジの接続状況（実測・読み取りのみ）\n検索DB: 未接続（ファイルなし）\nCKC保存待ち: 未確認 件"
    assert speech_summary("knowledge-stats", text) == "検索DBは未接続。保存待ちは未確認です。"


def test_stats_top3_unchanged():
    text = "# ナレッジの件数\n1. ニュース: 10 件\n2. 大洗: 5 件"
    assert speech_summary("knowledge-stats", text) == "上位は、ニュース、10 件。大洗、5 件です。"


@pytest.mark.parametrize("text", ["まずはマッハ数が異常に高いところを探して、最大マッハ数のところのメッシュをマス嗅覚とかを改善するような、そういう感じ。"])
def test_phrase_in_the_middle_of_explanation_is_not_rule(text):
    assert orc.route_by_rule(text, SEARCH) is None


@pytest.mark.parametrize("text, query", [("大洗 空調を探して", "大洗 空調"), ("大洗を探してください", "大洗"),
                                         ("大洗について知ってることある？", "大洗")])
def test_short_commands_still_match(text, query):
    r = orc.route_by_rule(text, SEARCH)
    assert (r.skill, r.inputs) == ("knowledge-search", {"query": query})


@pytest.mark.parametrize("text, want", [
    ("オーケー、じゃあまずはクローデンして。", "claude"),
    ("まずはクローデンして", "claude"),
    ("クロードにして", "claude"),
    ("ところで今喋ってるのはCodex君それともCloud君誰だろう", "status"),
    ("今答えてるのはどのモデル？", "status"),
    ("Claude Code について教えて", None),
    ("Codex にして何ができる？", None),
    ("誰がClaudeを使ってるの？", None),
    ("このモデルを使って何を作れる？", None),
])
def test_harness_command(text, want):
    assert orc.harness_command(text) == want
