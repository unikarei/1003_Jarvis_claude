"""自律改善・継続運用（MAP Phase 10、../docs/architecture.md §18）。

全体を監査し、改善の候補を**根拠付き**で優先度順に出す。変更は行わない（読み取りと提案だけ）。
- 構成: Discovery / Guardian（Registry と実態の差分）
- 知識: Knowledge Engine の監査（重複・鮮度）
- 開発: governance の開発ボード（着手できるタスク・承認待ち・PR の注意点）
- 技術的負債: リポジトリの TODO / FIXME、長すぎるファイル、マージ済みのまま残るリモートのブランチ
- バックアップ: バックアップのないデータ、最後の手動バックアップの日付
- 開発の再開: 統合ワークスペースの progress.md の「現在の状態」と、次に着手できるタスク

    python improve.py [--registry-dir DIR] [--out-dir reports/improvement] [--skip-prs]
"""

import argparse
import json
import re
import subprocess
import sys
from dataclasses import asdict, dataclass
from datetime import datetime
from pathlib import Path

import config
import governance
import knowledge_engine
from discovery import settings
from discovery.collect import collect, expand, locator_for_host
from discovery.guardian import evaluate

DEFAULT_OUT_DIR = config.REPORTS_DIR / "improvement"
BACKUP_DIR = Path.home() / "Backups"
LONG_FILE_LINES = 400
PRIORITY_ORDER = {"高": 0, "中": 1, "低": 2}


@dataclass
class Proposal:
    priority: str      # 高 / 中 / 低
    area: str          # 構成 / バックアップ / 開発 / 知識 / 技術的負債
    title: str
    evidence: str      # 根拠（数値と出典）
    action: str        # 推奨する対応


def _git(path: Path, *args: str) -> str:
    out = subprocess.run(["git", "--no-optional-locks", *args], cwd=path, capture_output=True, text=True, timeout=60)
    return out.stdout if out.returncode == 0 else ""


def tech_debt(registry: dict) -> dict:
    """Registry に登録されたローカルのリポジトリ（Vault 以外）の技術的負債の指標。"""
    result = {}
    for rid, r in sorted(registry["resources"].items()):
        if r.get("resource_type") != "repository" or not locator_for_host(r):
            continue
        path = expand(locator_for_host(r))
        if not (path / ".git").exists():
            continue
        files = [f for f in _git(path, "ls-files", "*.py").splitlines() if f]
        todos, long_files = 0, []
        for f in files:
            try:
                text = (path / f).read_text(encoding="utf-8")
            except (OSError, UnicodeDecodeError):
                continue
            todos += len(re.findall(r"\b(TODO|FIXME|XXX)\b", text))
            if text.count("\n") > LONG_FILE_LINES:
                long_files.append(f)
        merged = [b.strip() for b in _git(path, "branch", "-r", "--merged", "origin/main").splitlines()
                  if b.strip() and "->" not in b and not b.strip().endswith("/main")]
        result[rid] = {"python_files": len(files), "todo": todos, "long_files": sorted(long_files),
                       "merged_remote_branches": merged}
    return result


def backup_status(registry: dict) -> dict:
    no_backup = sorted(rid for rid, r in registry["resources"].items()
                       if r.get("resource_type") == "data_store" and r.get("backup") == "none")
    manual = sorted(p.name for p in BACKUP_DIR.iterdir() if p.is_dir()) if BACKUP_DIR.is_dir() else []
    return {"no_backup": no_backup, "manual_backups": manual, "latest_manual": manual[-1] if manual else None}


def resume_summary() -> str:
    path = governance.WORKSPACE_DIR / "docs" / "progress.md"
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return ""
    # 見出しには「（2026-10-13 朝に更新）」のような注記が付く。最後の節でも読めるようにする
    m = re.search(r"^## 現在の状態[^\n]*\n(.*?)(?=^## |\Z)", text, re.S | re.M)
    return m.group(1).strip() if m else ""


def proposals(report, ke_audit: dict, groups: dict, pr_notes: list[str], debt: dict, backup: dict) -> list[Proposal]:
    out = []
    for f in report.findings:
        if f.severity == "error":
            out.append(Proposal("高", "構成", f"{f.resource_id or '未登録の資産'}: {f.message}",
                                f"Guardian（{report.checked_at}）", "原因を調べて復旧する、または Registry の status を見直す"))
    if backup["no_backup"]:
        out.append(Proposal("高", "バックアップ", f"バックアップのないデータが {len(backup['no_backup'])} 件",
                            f"Registry の backup: none（{', '.join(backup['no_backup'])}）。手動のバックアップは最新 "
                            f"{backup['latest_manual'] or 'なし'}",
                            "バックアップ用ディスクを用意し、T-207 で定期バックアップを設定する"))
    unregistered = [f for f in report.findings if f.kind == "unregistered"]
    if unregistered:
        out.append(Proposal("中", "構成", f"Registry に未登録の資産が {len(unregistered)} 件",
                            "; ".join(f.message for f in unregistered[:5]), "Registry に登録するか、対象外の一覧に加える"))
    if groups["waiting_approval"]:
        out.append(Proposal("中", "開発", f"Owner の確認・承認待ちのタスクが {len(groups['waiting_approval'])} 件",
                            ", ".join(t.task_id for t in groups["waiting_approval"]),
                            "まとめて確認する（開発ボードの「Owner の承認・レビュー待ち」）"))
    if pr_notes:
        out.append(Proposal("中", "開発", f"PR の注意点が {len(pr_notes)} 件（積み重ね・同じファイル・古い PR）",
                            "; ".join(pr_notes[:3]), "マージの順番を決め、土台の PR から片付ける"))
    if ke_audit["duplicate_titles"]:
        sizes = [len(g) for g in ke_audit["duplicate_titles"]]
        out.append(Proposal("中", "知識", f"題名が重複した知識が {len(sizes)} 組（最大 {max(sizes)} 件）",
                            f"Knowledge Engine の監査（Knowledge {ke_audit['knowledge']} 件）",
                            "重複を確認し、統合・削除の案を作る（T-803。削除は Owner の承認後）"))
    if ke_audit["stale"]:
        out.append(Proposal("低", "知識", "鮮度の期限を過ぎた知識がある",
                            ", ".join(f"{k} {v} 件" for k, v in ke_audit["stale"].items()),
                            "古い知識を更新するか、置き換え済みにする"))
    for rid, d in debt.items():
        if d["merged_remote_branches"]:
            out.append(Proposal("低", "技術的負債", f"{rid}: マージ済みのリモートのブランチが {len(d['merged_remote_branches'])} 本",
                                ", ".join(d["merged_remote_branches"][:5]), "不要なブランチを削除する（Owner の確認後）"))
        if d["long_files"]:
            out.append(Proposal("低", "技術的負債", f"{rid}: {LONG_FILE_LINES} 行を超えるファイルが {len(d['long_files'])} 件",
                                ", ".join(d["long_files"][:5]), "責務ごとに分ける"))
        if d["todo"]:
            out.append(Proposal("低", "技術的負債", f"{rid}: TODO / FIXME が {d['todo']} 件", f"{d['python_files']} ファイル中",
                                "タスクにするか片付ける"))
    if groups["ready"]:
        out.append(Proposal("低", "開発", f"着手できるタスクが {len(groups['ready'])} 件",
                            ", ".join(t.task_id for t in groups["ready"]), "優先度を決めて着手する（Codex に渡せるものは渡す）"))
    out.sort(key=lambda p: PRIORITY_ORDER[p.priority])
    return out


def to_markdown(data: dict) -> str:
    lines = ["# 改善候補（自律改善・継続運用）", "",
             f"> 1003 の `improve.py` が生成する（{data['generated_at']}）。変更は行わず、根拠付きで提案する。", "",
             "## 開発の再開", "", data["resume"] or "（progress.md を読めませんでした）", "",
             "## 改善候補（優先度順）", ""]
    for i, p in enumerate(data["proposals"], 1):
        lines += [f"{i}. **[{p['priority']}・{p['area']}] {p['title']}**", f"   - 根拠: {p['evidence']}",
                  f"   - 対応: {p['action']}"]
    return "\n".join(lines) + "\n"


def build(registry_dir: Path, skip_prs: bool = False) -> dict:
    registry = json.loads((registry_dir / "generated" / "registry.json").read_text(encoding="utf-8"))
    snapshot = collect(registry)
    report = evaluate(registry, snapshot)
    tasks = governance.parse_tasks(governance.TASKS_MD.read_text(encoding="utf-8"))
    prs, _ = ([], []) if skip_prs else governance.collect_prs()
    groups = governance.classify(tasks)
    items = proposals(report, knowledge_engine.audit(), groups, governance.pr_findings(prs),
                      tech_debt(registry), backup_status(registry))
    return {"generated_at": datetime.now().astimezone().isoformat(timespec="seconds"),
            "resume": resume_summary(), "proposals": [asdict(p) for p in items],
            "ready_tasks": [t.task_id for t in groups["ready"]]}


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="全体を監査して改善候補を出す")
    parser.add_argument("--registry-dir", type=Path, default=settings.REGISTRY_DIR)
    parser.add_argument("--out-dir", type=Path, default=DEFAULT_OUT_DIR)
    parser.add_argument("--skip-prs", action="store_true", help="GitHub の PR を取りに行かない")
    args = parser.parse_args(argv)
    try:
        data = build(args.registry_dir, skip_prs=args.skip_prs)
    except (OSError, ValueError) as e:
        print(f"監査に必要なファイルを読めません: {e}", file=sys.stderr)
        return 2
    args.out_dir.mkdir(parents=True, exist_ok=True)
    (args.out_dir / "latest.json").write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    md = to_markdown(data)
    (args.out_dir / "latest.md").write_text(md, encoding="utf-8")
    print(md)
    return 0



def weekly() -> int:
    """週次の定期実行（launchd）: 改善の監査 → ACC の生成。失敗したら通知する。"""
    from acc import build
    from discovery.notify import notify_failure
    try:
        rc = main([]) or build.main([])
    except Exception as e:
        notify_failure("改善の監査", f"{type(e).__name__}: {e}")
        raise
    if rc != 0:
        notify_failure("改善の監査", f"終了コード {rc}")
    return rc


if __name__ == "__main__":
    sys.exit(main())
