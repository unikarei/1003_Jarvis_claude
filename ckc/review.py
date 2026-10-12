"""候補の表示・修正・承認（ckc_cli.py と chat.py が使う）。"""

from collections.abc import Callable

import config
from ckc import store
from ckc.models import Candidate, CandidateStatus, Kind
from ckc.publish import PublishError, publish
from ckc.reconcile import find_similar
from ckc.route import default_category, route_vault
from ckc.schema import validate_extraction

KIND_LABELS = {Kind.DECISION: "決定", Kind.PREFERENCE: "好み", Kind.HYPOTHESIS: "仮説",
               Kind.FINDING: "知見", Kind.TASK: "作業", Kind.QUESTION: "疑問"}


class ReviewError(Exception):
    pass


def short_id(candidate_id: str) -> str:
    return candidate_id.rsplit("_", 1)[-1]


def resolve(ref: str) -> Candidate:
    """候補 ID の全体、または末尾の短い ID から候補を探す。"""
    matches = [c for c in store.list_candidates() if c.candidate_id == ref or c.candidate_id.endswith("_" + ref)]
    if len(matches) != 1:
        raise ReviewError(f"候補を特定できません: {ref}（{len(matches)} 件が一致）")
    return matches[0]


def format_candidate(c: Candidate, detail: bool = False) -> str:
    vault_name = config.VAULTS.get(c.resource_id or "", "?")
    lines = [f"[{short_id(c.candidate_id)}] {c.kind.value}（{KIND_LABELS[c.kind]}）{c.title}",
             f"    状態: {c.status.value} / 確度: {c.confidence.value} / プロジェクト: {', '.join(c.projects) or '-'}"
             f" / 保存先: {vault_name}"]
    if c.similar:
        lines.append(f"    似た既存の知識: {', '.join(c.similar)}")
    if detail:
        lines.append("    本文: " + c.body.replace("\n", "\n          "))
        for e in c.evidence:
            lines.append(f"    根拠（{'Owner' if e.speaker.value == 'owner' else 'AI'}）: {e.quote}")
    return "\n".join(lines)


def edit(c: Candidate, *, title: str | None = None, body: str | None = None, kind: str | None = None,
         projects: list[str] | None = None) -> Candidate:
    """候補を修正する。修正後も抽出時と同じ検証に通し、保存先・カテゴリ・似た知識を計算し直す。"""
    item = {"kind": kind or c.kind.value, "title": title if title is not None else c.title,
            "body": body if body is not None else c.body,
            "projects": projects if projects is not None else c.projects, "category": None,
            "confidence": c.confidence.value,
            "evidence": [{"quote": e.quote, "speaker": e.speaker.value} for e in c.evidence]}
    result = validate_extraction({"items": [item]}, store.load_conversation(c.conversation_id), set(config.PROJECTS))
    if not result.items:
        raise ReviewError("修正した内容が検証に通りません: " + "; ".join(result.rejected[0][1]))
    v = result.items[0]
    c.kind, c.title, c.body, c.projects, c.confidence = v.kind, v.title, v.body, v.projects, v.confidence
    c.category = default_category(c.projects)
    c.resource_id = route_vault(c.projects)
    c.similar = [s["ckc_id"] for s in find_similar(c.title, c.body)]
    store.update_candidate(c)
    return c


def approve(c: Candidate, supersedes: str | None = None) -> None:
    if supersedes and supersedes not in c.similar:
        raise ReviewError(f"置き換え先は似た既存の知識から選んでください: {', '.join(c.similar) or 'なし'}")
    store.set_status(c.candidate_id, CandidateStatus.APPROVED, supersedes=supersedes)


def reject(c: Candidate) -> None:
    store.set_status(c.candidate_id, CandidateStatus.REJECTED)


def publish_approved(candidates: list[Candidate], out: Callable[[str], None] = print) -> int:
    """承認済みの候補を保存する。失敗したものは承認済みのまま残す。保存した件数を返す。"""
    done = 0
    for c in candidates:
        try:
            result = publish(c.candidate_id)
        except PublishError as e:
            out(f"[{short_id(c.candidate_id)}] 保存できませんでした: {e}")
            continue
        done += 1
        out(f"[{short_id(c.candidate_id)}] 保存しました: {config.VAULTS[result.resource_id]}/{result.note_relpath}")
        if result.warning:
            out(f"    注意: {result.warning}")
    return done


def interactive_review(candidates: list[Candidate], ask: Callable[[str], str] | None = None,
                       out: Callable[[str], None] = print) -> int:
    """候補を1件ずつ確認し、最後に承認したものを保存する。保存した件数を返す。"""
    ask = ask or input
    approved = []
    for c in candidates:
        while True:
            out("\n" + format_candidate(c, detail=True))
            choice = ask("[a] 承認 / [r] 却下 / [e] 修正 / [s] あとで / [q] 終了 > ").strip().lower()
            if choice == "a":
                supersedes = None
                if c.similar:
                    supersedes = ask(f"既存の知識を置き換える場合はその ID（{', '.join(c.similar)}）。"
                                     "新規なら Enter > ").strip() or None
                try:
                    approve(c, supersedes)
                except (ReviewError, store.StoreError) as e:
                    out(f"承認できません: {e}")
                    continue
                approved.append(c)
            elif choice == "r":
                reject(c)
            elif choice == "e":
                try:
                    edit(c, title=ask(f"題名（Enter で変更なし）[{c.title}] > ").strip() or None,
                         body=ask("本文（Enter で変更なし）> ").strip() or None,
                         kind=ask(f"種類（{'/'.join(k.value for k in Kind)}、Enter で変更なし）> ").strip() or None,
                         projects=_projects(ask(f"プロジェクト（カンマ区切り、- で空、Enter で変更なし）"
                                                f"[{', '.join(c.projects)}] > ")))
                except ReviewError as e:
                    out(str(e))
                continue
            elif choice == "q":
                break
            elif choice != "s":
                continue
            break
        if choice == "q":
            break
    if not approved:
        return 0
    if ask(f"\n承認した {len(approved)} 件を Obsidian に保存しますか？ [y/N] > ").strip().lower() != "y":
        out("承認済みのまま残しました（`ckc_cli.py publish` で保存できます）。")
        return 0
    return publish_approved(approved, out)


def _projects(answer: str) -> list[str] | None:
    answer = answer.strip()
    if not answer:
        return None
    if answer == "-":
        return []
    return [p.strip() for p in answer.split(",") if p.strip()]
