"""会話の始めに、Jarvis が今使えるものの概要を作る（上位 spec §3.4.12 K、T-3200）。

ナレッジ（検索 DB）・Obsidian の Vault のノートの名前・会話の記憶の件数・Owner の言葉で動く機能を実測して、
会話の LLM に渡す短い文にする。読み取りだけ（ファイル・DB に書かない）。同じ会話では 1 回だけ作る（ctx.briefing）。
"""
import sqlite3
import subprocess
from contextlib import closing
from datetime import datetime
from pathlib import Path, PurePosixPath

import config

MAX_CHARS = 7000                    # 仕組みのダイジェストを足して 4,000 → 7,000（T-3400）
AUTO_FOLDER = "_jarvis"             # 自動の知識の置き場（件数だけ）

# Owner の言葉で動く機能（スキル名, できること, 言い方）。索引で有効なものだけを出す
CAPABILITIES = (
    ("obsidian-note", "Obsidian のノートの追加・作成・書き直し・削除（内容と保存先を確認してから書く。ToDo の登録もこれ）",
     "「Obsidian の大洗_最優先ToDo に〇〇を追加して」"),
    ("knowledge-capture", "会話の内容をナレッジとして Obsidian に保存（候補を確認してから保存）", "「ナレッジ化して」"),
    ("news-digest", "集めたニュースの主要なもの（点数の順）と件数", "「今日の主要なニュースは？」「昨日のニュースのトップ10」"),
    ("report-digest", "Insight Reporter のレポートの一覧・回ごとの概要と題目", "「インサイトレポートの一覧」「11月20日のフィジカルAIスキャンの題目を全部」"),
    ("knowledge-search", "ナレッジの検索の一覧", "「〇〇を探して」"),
    ("knowledge-stats", "ナレッジの件数・接続の状態", "「ナレッジは何件？」「ナレッジの接続状況」"),
    ("knowledge-trace", "答えの出典", "「その出典は？」"),
    ("system-status", "システムの状態", "「システムの状態は？」"),
    ("control-center", "管理画面", "「管理画面を開いて」"),
    ("dev-board", "開発で次にやること", "「次にやること」"),
    ("theme-register", "定期収集のテーマの登録", "「テーマ登録: 〇〇」"),
    ("code-task", "コードの作成（Codex に任せる）", "「コードを書いて」"),
)
VAULT_LABELS = {"vault-oarai-obsidian": "大洗（oarai-obsidian）", "vault-jarvis-knowledge": "汎用（jarvis-knowledge）"}


# 長すぎるときに短くする順: (ノートの一覧の上限, 最近の変更の上限, 定期の仕事を短くするか)（T-3400）
SHRINK_STEPS = ((60, 15, False), (30, 15, False), (15, 15, False), (15, 5, False), (15, 5, True), (15, 0, True))
DIGEST_PATH = config.BASE_DIR / "docs" / "self-digest.md"
CHANGE_DAYS = 7


def for_ctx(ctx) -> str:
    """その会話の概要（なければ作って ctx.briefing に置く）。作れなくても空の文字列で会話を続ける。"""
    if getattr(ctx, "briefing", None) is None:
        try:
            ctx.briefing = build(session_id=getattr(ctx, "session_id", None))
        except Exception:  # noqa: BLE001  概要の失敗で会話を止めない
            ctx.briefing = ""
    return ctx.briefing


def _ro(path: Path):
    return closing(sqlite3.connect(Path(path).resolve().as_uri() + "?mode=ro", uri=True, timeout=0.1))


def _knowledge() -> list[str]:
    try:
        import knowledge_engine as ke
        s = ke.stats()
        sources = "、".join(f"{x['name']} {x['count']}" for x in s["by_source"]) or "なし"
        lines = [f"- 知識 {s['total']} 件（データソース別: {sources}）"]
        lines += [f"- 未取得: {x['name']}" for x in s["unavailable"]]
    except Exception:  # noqa: BLE001
        lines = ["- 知識の件数: 未確認"]
    pending = _count("SELECT count(*) FROM ckc_candidates WHERE status='pending'")
    lines.append(f"- 保存待ちの知識の候補: {pending} 件（「ナレッジ化して」で 1 件ずつ確認できる）")
    return lines


def _count(sql: str) -> str:
    if not Path(config.DB_PATH).is_file():
        return "未確認"
    try:
        with _ro(config.DB_PATH) as conn:
            return str(conn.execute(sql).fetchone()[0])
    except sqlite3.Error:
        return "未確認"


def _vault_notes() -> list[tuple[str, int, list[str], int]]:
    """[(Vault の表示名, ノート数, 自動の知識以外のノートの名前, 自動の知識の数)]。"""
    import registry
    import vault
    out = []
    for rid, path in registry.vault_paths().items():
        label = VAULT_LABELS.get(rid, rid)
        try:
            notes = vault.list_notes(rid)
        except Exception:  # noqa: BLE001
            out.append((label, -1, [], 0))
            continue
        auto = [n for n in notes if PurePosixPath(n).parts[0] == AUTO_FOLDER]
        names = [str(PurePosixPath(n).with_suffix("")) for n in notes if n not in auto]
        out.append((label, len(notes), names, len(auto)))
    return out


def _vault_lines(vaults, limit: int) -> list[str]:
    lines = []
    for label, count, names, auto in vaults:
        if count < 0:
            lines.append(f"- {label}: 未確認（読めませんでした）")
            continue
        shown = "、".join(names[:limit]) or "なし"
        more = f" ほか {len(names) - limit} 件" if len(names) > limit else ""
        lines.append(f"- {label}: ノート {count} 件。ノート: {shown}{more}。自動の知識（{AUTO_FOLDER}/）{auto} 件")
    return lines


def _news() -> list[str]:
    try:
        import news_rank
        return news_rank.briefing_lines()
    except Exception:  # noqa: BLE001
        return ["- ニュース: 未確認"]


def _reports() -> list[str]:
    try:
        import ir_reports
        return ir_reports.briefing_lines()
    except Exception:  # noqa: BLE001
        return ["- Insight Reporter のレポート: 未確認"]


def _memory(session_id: str | None = None) -> list[str]:
    if not config.MEMORY_ENABLED:
        return ["- 会話の記憶: 無効（設定）"]
    notes = _count("SELECT count(*) FROM memory_notes")
    sessions = _count("SELECT count(*) FROM memory_sessions WHERE summary_status='done'")
    return [f"- 記憶のメモ {notes} 件、要約のある会話 {sessions} 件。直近 3 日の会話の要約と記憶のメモは「以前の会話の記憶」で渡す",
            *_last_sessions(session_id)]


def _last_sessions(session_id: str | None, limit: int = 3) -> list[str]:
    """直前の会話の始めと終わりの時刻（今の会話を除く、T-3400）。"""
    if not Path(config.DB_PATH).is_file():
        return ["- 直前の会話の時刻: 未確認"]
    try:
        from memory.recall import CHANNEL_LABELS, session_span
        with _ro(config.DB_PATH) as conn:
            rows = conn.execute("SELECT channel, started_at, last_at, ended_at, owner_turns FROM memory_sessions "
                                "WHERE session_id != ? ORDER BY last_at DESC LIMIT ?", (session_id or "", limit)).fetchall()
    except (sqlite3.Error, ImportError):
        return ["- 直前の会話の時刻: 未確認"]
    if not rows:
        return ["- 直前の会話: なし"]
    shown = "、".join(f"{session_span(started, ended or last)}（{CHANNEL_LABELS.get(channel, '会話')}、Owner の発言 {turns} 回）"
                     for channel, started, last, ended, turns in rows)
    return [f"- 直前の会話の時刻（新しい順）: {shown}"]


def _self_digest() -> list[str]:
    """人が書く仕組みのダイジェスト（docs/self-digest.md、見出しの行を除く）。"""
    try:
        lines = DIGEST_PATH.read_text(encoding="utf-8").splitlines()
    except OSError:
        return ["- 仕組みのダイジェスト: 未確認（読めませんでした）"]
    return [line for line in lines if line.strip() and not line.startswith("#")]


def _jobs(short: bool = False) -> list[str]:
    """定期の仕事と常駐のサービス（Architecture Registry が正本）。"""
    try:
        import registry
        data = registry.load() or {}
        resources = data.get("resources", {})
    except Exception:  # noqa: BLE001
        resources = {}
    lines = []
    for kind, label in (("scheduled_job", "定期"), ("service", "常駐")):
        for x in resources.values():
            if x.get("resource_type") != kind:
                continue
            purpose = " ".join(str(x.get("purpose") or "").split())
            if short:
                purpose = purpose[:40] + ("…" if len(purpose) > 40 else "")
            lines.append(f"- {label}（{x.get('schedule') or '常時'}）: {purpose}")
    return lines or ["- 定期の仕事と常駐のサービス: 未確認（Registry を読めませんでした）"]


def _git_merges(days: int) -> str:
    return subprocess.run(["git", "-C", str(config.BASE_DIR), "log", "--merges", f"--since={days}.days",
                           "--format=%ci%x1f%s%x1f%b%x1e"], capture_output=True, text=True, timeout=3, check=True).stdout


def _changes(limit: int) -> list[str]:
    """1003 の直近のマージ（日時と PR の題名）。「今日何を入れた？」に答えるため（T-3400）。"""
    if limit <= 0:
        return []
    try:
        out = _git_merges(CHANGE_DAYS)
    except (OSError, subprocess.SubprocessError):
        return ["- 最近の変更: 未確認"]
    items = []
    for record in out.split("\x1e"):
        if "\x1f" not in record:
            continue
        at, subject, body = (record.strip().split("\x1f", 2) + ["", ""])[:3]
        if not subject.startswith("Merge pull request"):     # PR のマージだけ（手元のマージは除く）
            continue
        title = next((line.strip() for line in body.splitlines()
                      if line.strip() and not line.startswith(("#", "Co-Authored-By"))), "")
        if title:
            items.append(f"- {at[:16]} {title}")
    if not items:
        return [f"- 直近 {CHANGE_DAYS} 日の変更: なし"]
    more = [f"- ほか {len(items) - limit} 件"] if len(items) > limit else []
    return items[:limit] + more


def _capabilities() -> list[str]:
    try:
        import skills
        active = skills.active(quiet=True)
    except Exception:  # noqa: BLE001
        active = {}
    lines = [f"- {what}: {say}" for name, what, say in CAPABILITIES if name in active]
    lines.append("- 会話のモデルの切り替え: 「Codex にして」「Claude にして」「Ollama にして」「今どれ？」")
    return lines


def build(now: datetime | None = None, session_id: str | None = None) -> str:
    now = now or datetime.now()
    vaults = _vault_notes()
    digest, memory = _self_digest(), _memory(session_id)
    head = [f"# 今あなた（JARVIS）が使えるもの（会話の始めに Jarvis が実測 {now:%Y-%m-%d %H:%M}。"
            "どの書庫・ナレッジ・ノートにアクセスできるか、何ができるかを聞かれたら、この範囲で答える）",
            "## ナレッジ（検索 DB。質問の答えの資料に使う）", *_knowledge(),
            "## Obsidian の Vault（中身は検索 DB に取り込み済み。ノートの編集は下の機能で行う）"]
    tail = ["## ニュース（日次処理で集めた記事。上の知識の「Web の記事」）", *_news(),
            "## Insight Reporter のレポート（1004 が毎週作る。中身は検索 DB にも入っている）", *_reports(),
            "## 会話の記憶", *memory,
            "## Owner の言葉で動く Jarvis の機能（答えの役とは別のスキルが実行する。頼まれたら「できない」と言わず、この言い方を案内する）",
            *_capabilities(),
            "## ないもの",
            "- ToDo・予定・リマインダーの専用の機能はない。ToDo は Obsidian のノート（例: 大洗_最優先ToDo）への追記で扱う。"]
    text = ""
    for note_limit, change_limit, short in SHRINK_STEPS:
        system = ["## Jarvis の仕組み（管理画面・ランチャー・ニュース・Insight Reporter・記憶などを聞かれたら、これで答える）",
                  *digest,
                  "## 定期の仕事と常駐のサービス（Architecture Registry。何がいつ動くかを聞かれたらこれで答える）",
                  *_jobs(short)]
        changes = ([f"## 最近入った変更（Jarvis 本体の直近 {CHANGE_DAYS} 日のマージ。「今日何を入れた？」に答える）",
                    *_changes(change_limit)] if change_limit else [])
        text = "\n".join(head + _vault_lines(vaults, note_limit) + tail + system + changes) + "\n"
        if len(text) <= MAX_CHARS:
            break
    return text[:MAX_CHARS]
