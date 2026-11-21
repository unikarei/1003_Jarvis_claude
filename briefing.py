"""会話の始めに、Jarvis が今使えるものの概要を作る（上位 spec §3.4.12 K、T-3200）。

ナレッジ（検索 DB）・Obsidian の Vault のノートの名前・会話の記憶の件数・Owner の言葉で動く機能を実測して、
会話の LLM に渡す短い文にする。読み取りだけ（ファイル・DB に書かない）。同じ会話では 1 回だけ作る（ctx.briefing）。
"""
import sqlite3
from contextlib import closing
from datetime import datetime
from pathlib import Path, PurePosixPath

import config

MAX_CHARS = 4000
NOTE_LIMITS = (60, 30, 15)          # Vault ごとのノートの名前の上限（全体が MAX_CHARS を超えたら短くする）
AUTO_FOLDER = "_jarvis"             # 自動の知識の置き場（件数だけ）

# Owner の言葉で動く機能（スキル名, できること, 言い方）。索引で有効なものだけを出す
CAPABILITIES = (
    ("obsidian-note", "Obsidian のノートの追加・作成・書き直し・削除（内容と保存先を確認してから書く。ToDo の登録もこれ）",
     "「Obsidian の大洗_最優先ToDo に〇〇を追加して」"),
    ("knowledge-capture", "会話の内容をナレッジとして Obsidian に保存（候補を確認してから保存）", "「ナレッジ化して」"),
    ("news-digest", "集めたニュースの主要なもの（点数の順）と件数", "「今日の主要なニュースは？」「昨日のニュースのトップ10」"),
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


def for_ctx(ctx) -> str:
    """その会話の概要（なければ作って ctx.briefing に置く）。作れなくても空の文字列で会話を続ける。"""
    if getattr(ctx, "briefing", None) is None:
        try:
            ctx.briefing = build()
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


def _memory() -> list[str]:
    if not config.MEMORY_ENABLED:
        return ["- 会話の記憶: 無効（設定）"]
    notes = _count("SELECT count(*) FROM memory_notes")
    sessions = _count("SELECT count(*) FROM memory_sessions WHERE summary_status='done'")
    return [f"- 記憶のメモ {notes} 件、要約のある会話 {sessions} 件。直近 3 日の会話の要約と記憶のメモは「以前の会話の記憶」で渡す"]


def _capabilities() -> list[str]:
    try:
        import skills
        active = skills.active(quiet=True)
    except Exception:  # noqa: BLE001
        active = {}
    lines = [f"- {what}: {say}" for name, what, say in CAPABILITIES if name in active]
    lines.append("- 会話のモデルの切り替え: 「Codex にして」「Claude にして」「Ollama にして」「今どれ？」")
    return lines


def build(now: datetime | None = None) -> str:
    now = now or datetime.now()
    vaults = _vault_notes()
    head = [f"# 今あなた（JARVIS）が使えるもの（会話の始めに Jarvis が実測 {now:%Y-%m-%d %H:%M}。"
            "どの書庫・ナレッジ・ノートにアクセスできるか、何ができるかを聞かれたら、この範囲で答える）",
            "## ナレッジ（検索 DB。質問の答えの資料に使う）", *_knowledge(),
            "## Obsidian の Vault（中身は検索 DB に取り込み済み。ノートの編集は下の機能で行う）"]
    tail = ["## ニュース（日次処理で集めた記事。上の知識の「Web の記事」）", *_news(),
            "## 会話の記憶", *_memory(),
            "## Owner の言葉で動く Jarvis の機能（答えの役とは別のスキルが実行する。頼まれたら「できない」と言わず、この言い方を案内する）",
            *_capabilities(),
            "## ないもの",
            "- ToDo・予定・リマインダーの専用の機能はない。ToDo は Obsidian のノート（例: 大洗_最優先ToDo）への追記で扱う。"]
    text = ""
    for limit in NOTE_LIMITS:
        text = "\n".join(head + _vault_lines(vaults, limit) + tail) + "\n"
        if len(text) <= MAX_CHARS:
            break
    return text[:MAX_CHARS]
