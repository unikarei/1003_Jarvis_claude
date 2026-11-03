"""スキルの索引の読み込み（FR-014、../docs/architecture.md §19.4）。

スキルの宣言の正本は jarvis-skills リポジトリ。その生成物 generated/skills.json（§19.2）を標準ライブラリで読む。
読めないときは予備の索引（FR-014 より前から chat.py にあった機能だけ）で動き続け、警告を1回だけ出す（registry.py と同じ考え方）。
本モジュールはスキルの宣言を書き換えない。
"""

import json
import os
import warnings
from dataclasses import dataclass, field
from pathlib import Path

import config
import registry

STATUSES = ("candidate", "active", "deprecated")
PERMISSIONS = ("read", "local_write", "external")


@dataclass(frozen=True)
class Skill:
    name: str
    description: str
    status: str
    permission: str
    entry: dict
    inputs: dict = field(default_factory=dict)      # 入力名 → {"type", "required", "description"}
    phrases: tuple[str, ...] = ()
    verify: str | None = None
    confirm: bool = False                            # true なら read でも実行のたびに確認する（課金・外部への送信）

    def required_inputs(self) -> list[str]:
        return [k for k, v in self.inputs.items() if v.get("required")]


# 索引を読めないときの予備。FR-014 より前から chat.py にあった機能（普通の質問・ナレッジ化・テーマ登録・コードの依頼）は、
# 索引がなくても同じように使えるようにする（後方互換）
_R1003 = "repo-1003-jarvis-claude"
FALLBACK = {s.name: s for s in (
    Skill("knowledge-answer", "蓄積した知識を検索し、検索結果だけを根拠に出典付きで答える。ほかのスキルに当たらない質問のとき。",
          "active", "read", {"repo": _R1003, "function": "knowledge_engine.answer"},
          {"question": {"type": "string", "required": True, "description": "質問"}}),
    Skill("knowledge-capture", "それまでの対話から知識を抽出し、確認のうえ Obsidian に保存する。「ナレッジ化して」のとき。",
          "active", "local_write", {"repo": _R1003, "function": "chat_actions.run_ckc"},
          phrases=("ナレッジ化して", "ナレッジ化する", "/ckc")),
    Skill("theme-register", "新しいテーマについて、既存資産の再利用と構成を提案し、確認のうえ適用する。「テーマ登録: 〜」のとき。",
          "active", "local_write", {"repo": _R1003, "function": "chat_actions.run_architect"},
          {"theme": {"type": "string", "required": True, "description": "テーマ"}},
          ("テーマ登録:", "テーマ登録：", "/theme ")),
    Skill("code-task", "コードの生成・修正・デバッグをコード用のサブエージェントに任せる。「〜する関数を書いて」のとき。",
          "active", "read", {"repo": _R1003, "function": "code_agent.run_code_task"},
          {"task": {"type": "string", "required": False, "description": "依頼の内容"}},
          ("コードを書いて", "関数を書いて", "スクリプトを書いて", "デバッグして", "リファクタして", "バグを直して"),
          confirm=True),   # OpenAI API（従量課金）に発言を送るため、毎回確認する
)}

_cache: dict[str, Skill] | None = None


def reset_cache() -> None:
    global _cache
    _cache = None


def index_path() -> Path:
    """skills.json の場所: 環境変数 JARVIS_SKILLS_JSON → Registry の repo-jarvis-skills → 既定の場所。"""
    if os.environ.get("JARVIS_SKILLS_JSON"):
        return Path(os.environ["JARVIS_SKILLS_JSON"])
    data = registry.load() or {}
    res = (data.get("resources") or {}).get("repo-jarvis-skills") or {}
    locator = registry._locator_for_host(res.get("locator"))
    if locator:
        return Path(os.path.expanduser(locator)) / "generated" / "skills.json"
    return config.SKILLS_JSON


def _parse(name: str, raw: dict) -> Skill:
    if raw.get("status") not in STATUSES:
        raise ValueError(f"status が不正: {raw.get('status')}")
    if raw.get("permission") not in PERMISSIONS:
        raise ValueError(f"permission が不正: {raw.get('permission')}")
    entry = raw.get("entry")
    if not isinstance(entry, dict) or not entry.get("repo"):
        raise ValueError("entry がない")
    inputs = raw.get("inputs") or {}
    if not isinstance(inputs, dict) or not all(isinstance(v, dict) for v in inputs.values()):
        raise ValueError("inputs が不正")
    phrases = raw.get("phrases") or []
    if not isinstance(phrases, list) or not all(isinstance(p, str) and p for p in phrases):
        raise ValueError("phrases が不正")
    confirm = raw.get("confirm", False)
    if not isinstance(confirm, bool):
        raise ValueError("confirm が真偽値ではない")
    return Skill(name, str(raw.get("description") or ""), raw["status"], raw["permission"], dict(entry),
                 {k: dict(v) for k, v in inputs.items()}, tuple(phrases), raw.get("verify"), confirm)


def load(quiet: bool = False) -> dict[str, Skill]:
    """name → Skill（状態を問わずすべて）。読めなければ予備の索引（警告を1回だけ出す）。
    quiet=True は警告を出さず、読んだ結果を残さない（裏のスレッドの warm_up 用。警告は会話の側の読み込みで出す）。"""
    global _cache
    if _cache is None and quiet:
        try:
            data = json.loads(index_path().read_text(encoding="utf-8"))
            raws = data["skills"] if isinstance(data.get("skills"), dict) else None
        except (OSError, ValueError, AttributeError):
            raws = None
        if raws is None:
            return dict(FALLBACK)
        quiet_skills = {}
        for name, raw in raws.items():
            try:
                quiet_skills[name] = _parse(name, raw if isinstance(raw, dict) else {})
            except ValueError:
                pass
        return quiet_skills
    if _cache is None:
        path = index_path()
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            if not isinstance(data.get("skills"), dict):
                raise ValueError("skills がありません")
        except (OSError, ValueError, AttributeError) as e:
            warnings.warn(f"スキルの索引を読めないため、予備の索引で動きます（{path}: {e}）", stacklevel=2)
            _cache = dict(FALLBACK)
            return _cache
        skills = {}
        for name, raw in data["skills"].items():
            try:
                skills[name] = _parse(name, raw if isinstance(raw, dict) else {})
            except ValueError as e:
                warnings.warn(f"スキル {name} を読み飛ばします: {e}", stacklevel=2)
        _cache = skills
    return _cache


def active(quiet: bool = False) -> dict[str, Skill]:
    """使ってよいスキル（status が active）だけ。quiet は load() と同じ。"""
    return {name: s for name, s in load(quiet).items() if s.status == "active"}
