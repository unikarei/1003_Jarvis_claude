"""ナレッジ回答の調整できる値（評価部門、../docs/spec.md §3.4.10、ADR-0030、T-1411）。

評価（T-1411）と改善（T-1412）で変える値を Tuning にまとめる。本番のコード（knowledge_engine・orchestrator）は
current() から値を読む。既定の値は今の動きと同じ。use(t) は評価の間だけ差し替える（ファイルに書かない）。
"""

import contextvars
from contextlib import contextmanager
from dataclasses import asdict, dataclass, field


@dataclass(frozen=True)
class Tuning:
    search_limit: int = 8                     # 答えの文脈に渡す検索結果の数
    context_chars: int = 1500                 # 1 件の知識から渡す本文の先頭の文字数
    score_threshold: float | None = None      # ナレッジの質問とみなす検索の点数（None なら config.KNOWLEDGE_SCORE_THRESHOLD）
    extra_knowledge_words: tuple[str, ...] = ()   # ナレッジの質問とみなす語に加える語
    synonyms: dict[str, list[str]] = field(default_factory=dict)   # 検索の語の展開 {"住所": ["所在地"]}
    answer_prompt: str = "default"            # knowledge_engine.ANSWER_PROMPTS のキー

    def to_dict(self) -> dict:
        data = asdict(self)
        data["extra_knowledge_words"] = list(self.extra_knowledge_words)
        return data


DEFAULT = Tuning()
_current: contextvars.ContextVar[Tuning | None] = contextvars.ContextVar("jarvis_tuning", default=None)


def current() -> Tuning:
    return _current.get() or DEFAULT


@contextmanager
def use(t: Tuning):
    """with の中だけ current() を t にする。終わったら（例外でも）元に戻す。"""
    token = _current.set(t)
    try:
        yield t
    finally:
        _current.reset(token)
