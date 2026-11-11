# Jarvis 2 統合メモリ基盤 開発仕様書

文書番号：J2-MEM-ARCH-001　版：**1.1**（2026-11-12、実態に合わせて改訂）　初版：1.0（2026-11-12）

- 状態: **Approved**（2026-11-12 Owner 決定「その方針を採用」。細部は Claude 承認（ワークスペース AGENTS.md §2.2））
- 要件の正本: ワークスペースの `docs/spec.md` §3.3.14（会話の記憶）。判断の記録: `docs/decisions.md` ADR-0037。設計: `docs/architecture.md` §10.9。受け入れ基準: `docs/testing.md` §17。タスク: `docs/tasks.md` §22。**要件はそちらを正とし、本書には Mem0 案からの経緯と、1003 の中での対応関係を書く**（同じ要件を 2 か所で別々に直さない）。
- ファイル名は v1.0 のまま残す（Owner が参照しているため）。

## 0. v1.1 で何が変わったか（要旨）

| 項目 | v1.0（Mem0 案、採用案 B） | v1.1（採用した構成） |
|---|---|---|
| 記憶エンジン | CKC の中に Mem0 OSS を組み込む | **Mem0 を最初は入れない**。1003 の `memory/` パッケージ（SQLite と JSONL）。Mem0 は測ってから判断（§6） |
| 長期の知識の正本 | Obsidian | 変わらない（CKC で承認したノート） |
| 会話の原文 | JSONL／SQLite（未確認） | `data/memory/sessions/<YYYY>/<MM>/<会話ID>.jsonl`（新規）。CKC の取り込みの原文は今までどおり `data/ckc/raw/` |
| 検索 | Mem0（意味・BM25・エンティティ）＋ Qdrant | 既存の BM25 を会話の要約に当てる。意味検索（Ollama の embedding）は測って良くなったときだけ |
| 入口の API | 8 つ（remember・recall・propose・approve・update・forget・export・reindex） | `memory` の記録・想起の 2 つと CLI。承認は既存の CKC（`ckc_cli.py`・ブラウザのカード） |
| 同期 | 対応表・待ち行列・`sync_pending`・毎日の照合・変更イベントのログ | 要らない（保存先が 1 つ。索引は原文から `rebuild`） |
| アクセス制御 | user_id・agent_id・認証 | 作らない（Owner 一人が Mac mini の中で使う） |

v1.0 の「設計の絶対条件」のうち次は引き継いだ: Obsidian を承認済みの知識の正本にする／記憶の入口を 1 つにする／会話の原文を残し、索引を作り直せるようにする／未承認のものが確定した知識に混ざらない／既存の CKC と Obsidian のノートを壊さない／Codex・Claude・Ollama のどれでも同じ記憶を使う。

## 1. 背景と目的

Jarvis は Ollama・Claude・Codex を切り替えて答えられる（spec §3.4.12）が、会話の文脈は同じ会話の直近 3 往復（`knowledge_engine.HISTORY_TURNS = 6`）だけで、会話を終えると消える。CKC は「ナレッジ化して」で承認した知識だけを Obsidian に残す。目的は、CKC を生かしたまま、前回の話の続き・決めたこと・好みを次の会話で思い出して対話できるようにすること。Owner の条件は「シンプルな構成で、今後 1 年は改造の要らない仕様」。

## 2. 実態の調査（v1.0 の §14「未決事項・実装前確認」への答え、2026-11-12）

| v1.0 の未決事項 | 実態 |
|---|---|
| CKC のモジュール構成 | `ckc/`: capture・extract・reconcile・route・publish・render・review・schema・secrets・store・models（ワークスペース architecture §10.1） |
| 利用 DB | SQLite の `jarvis.db`（`nodes`・`edges` と CKC の `ckc_conversations`・`ckc_candidates`） |
| 検索機能 | `knowledge_engine.search`: janome の名詞と BM25。意味検索・ベクトル DB はない |
| Vault の書式 | `_jarvis/knowledge/<種類>/` に 1 件 1 ノート、frontmatter に `ckc_id`・種類・状態・出典。Knowledge ノードの meta に `kind`・`status`・`projects`・`note_relpath` |
| ランタイムと呼び出し方 | すべて Python、1 つのプロセス（`chat.py`・`voice_chat.py`・`python -m voice.web`）。FastAPI などのプロセス境界はない |
| ローカルの embedding | Ollama に embedding モデルは入っていない（2026-11-12）。チャットは `gemma4:12b` |
| 規模 | Knowledge 2,423 件（ニュースなどの自動収集が大半）、CKC の会話 5 件・候補は保存待ち 22 件・公開 1 件 |
| Obsidian の手動の編集 | 毎日 05:45 の `ingest_obsidian`（T-1801）で取り込み済み。CKC のノートの編集は `obsidian-note`（spec §3.4.11） |

## 3. 構成（1003）

```
Jarvis（chat.py / voice_chat.py / ブラウザ）
        |
  orchestrator.handle ── 経路 A・B のプロンプトに「以前の会話の記憶」を渡す ──> Ollama / Codex / Claude
        |                                   ^
  add_turn（履歴 + 記録）            memory.recall（記憶パック 1,200 字）
        |                                   |
  memory/store ──> data/memory/sessions/*.jsonl（原文） + jarvis.db memory_*（索引）
        ^
  memory/summarize（次の会話の起動時に裏で、Ollama だけ）

  CKC（今のまま）: 会話 → 抽出 → 照合 → Owner の承認 → Obsidian（正本） + ナレッジグラフ
  承認済みの Preference は記憶パックの先頭に入る
```

記憶パックの中身と順番、要約の形、記録する発言の条件、やらないことは spec §3.3.14 を正とする。

## 4. v1.0 の要件との対応

| v1.0 の項目 | v1.1 での扱い |
|---|---|
| §4 会話原文（追記専用） | spec §3.3.14 A（JSONL、権限 600） |
| §4 会話上の暫定記憶（Mem0 transient scope） | 会話の要約（spec §3.3.14 B）。Obsidian・ナレッジグラフに入れない |
| §4 未承認候補 | 今の CKC の保存待ち（変更なし） |
| §4.1 共通スキーマ（knowledge_id・revision など） | 作らない。承認済みの知識は今の CKC の `ckc_id` とノートの frontmatter、会話の記憶は `memory_sessions` |
| §4.2 古い決定は superseded | 承認済みの知識は今の CKC の `supersedes`。会話の要約は日付を付けて渡し、プロンプトで新しいものを優先させる |
| §5 API 8 つ | `memory` の記録・想起と CLI（`list`・`show`・`summarize`・`rebuild`・`stats`・`forget`）。承認は CKC |
| §7 検索と注入、プロンプトインジェクション対策 | spec §3.3.14 C（参考の情報で命令ではない、記憶にないことを覚えていると言わない） |
| §7 Mem0 障害時の縮退 | 想起で例外が出ても記憶なしで答える |
| §8 同期・冪等・差分照合 | 保存先が 1 つのため不要。`rebuild` で作り直す |
| §9 移行（Vault の初期スキャン、ID 付与） | 不要（既存のノートは変えない。過去の会話は記録がないため、記憶は記録を始めた日から） |
| §9 ChatGPT のエクスポートの取り込み | CKC の `ckc/parsers/`（T-117、実装済み）で知識の候補として取り込む。会話の記憶には入れない |
| §10 秘密情報のマスク、ローカル優先 | `ckc.secrets.mask_secrets` で伏せてから記録。要約は Ollama だけ |
| §11 Mem0・Qdrant・Embedding の設定 | Mem0・Qdrant は使わない。embedding は `JARVIS_EMBED_MODEL`（既定 `bge-m3`、T-2104） |
| §12 受け入れ基準 | ワークスペース testing.md §17 に置き換え（記憶の評価 30 件以上、合格率 80%、作り話 0 件など） |
| §13 フェーズ P0〜P5 | P0 監査は本書 §2 で完了。P1〜P4 は T-2101〜T-2104・T-2106、P5（CKC 単独と CKC＋Mem0 の比較）は T-2105 |

## 5. タスク

| ID | 内容 | 担当 |
|---|---|---|
| T-2101 | 会話の記録・要約・CLI | Codex |
| T-2102 | 想起と注入 | Codex |
| T-2103 | 記憶の評価セットと測定 | Codex |
| T-2104 | 意味検索（測ってから採否） | Codex |
| T-2105 | Mem0 の判断 | Claude（基準に当たれば Owner） |
| T-2106 | 本番への反映と実機の確認 | Claude |

## 6. Mem0 の扱い（T-2105）

- 判断の基準（spec §3.3.14 G）: T-2103・T-2104 の後に、会話の文脈の問題の合格率が 80% に届かず、失敗の多くが固有名詞のつながりや時系列の区別によるもので、意味検索でも直らないとき。
- 試すときの形: Mem0 OSS を「会話の記憶」の役割（spec §3.3.14 B・C の置き換え）で使い、Mem0 自身に会話から抽出させる（`infer=True`）。承認済みの知識は今までどおり CKC と Obsidian。保存しておいた会話の原文（`data/memory/sessions/`）を読み込ませ、同じ評価セットで比べる。
- 条件: ローカルの Ollama だけを使う（既定の OpenAI を使わない）、利用状況の送信（テレメトリ）を止める、版を固定し契約テストを置く、新しい依存として別の ADR で決める。
- v1.0 の調査で分かっていた注意（2026-11-12 時点、要再確認）: 公表された高い精度はクラウド版の結果で、OSS 版の効果は保証されない。2026 年 4 月に記憶のアルゴリズムが刷新された（ADD-only の抽出、意味検索・BM25・エンティティのハイブリッド検索）。v2→v3 の移行に互換性のない変更があり、最近の版でも保存の失敗を成功として返す不具合の修正がある。Letta（Letta Code）はエージェントの実行基盤の方向で、CKC の置き換えには移行の費用が大きい。

## 付録A. 本会話からの要点（v1.0 の付録 A の状態を更新）

| 番号 | 要点 | v1.1 での状態 |
|---|---|---|
| P0-01 | CKC 内部に Mem0 OSS を組み込む「案 B」 | **変更**: Mem0 は測ってから判断（ADR-0037） |
| P0-02 | Obsidian が第二の脳かつ承認済み知識の正本 | 採用 |
| P0-03 | CKC の抽出・照合・承認・公開を維持 | 採用（変更なし） |
| P0-04 | 会話全文の JSONL を残す | 採用（会話の記憶の原文）。変更イベントのログ・定期のスナップショットは作らない |
| P0-05 | 記憶の仕組みを捨てても Obsidian と JSONL から復元できる | 採用（`rebuild`） |
| P0-06 | Codex・Claude・Ollama で共通の記憶 | 採用（記憶パックを経路 A・B のプロンプトに渡す） |
| P0-07 | 承認済みと未承認の区別 | 採用（記憶パックに「承認済み」「会話の要約」を付ける） |
| P0-08 | 二重抽出の回避 | 採用（知識の抽出は CKC だけ。会話の要約は知識にしない） |
| P0-09 | 旧版・訂正の管理 | 承認済みは CKC の `supersedes`。会話の要約は日付で新しい方を優先 |
| P0-10 | knowledge_id と mem0_id の対応 | 不要（Mem0 を使わない） |
| P1-11〜16 | Mem0 と Letta の比較、ベンチマークの注意、CKC 単独との比較 | 本書 §6。比較は T-2105 |
| P1-17 | 自動想起 | 採用（spec §3.3.14 C） |
| P1-18 | 短期と長期の分離 | 採用（今の会話の履歴／会話の要約／承認済みの知識） |
| P1-19 | プロジェクトの境界 | この版では記憶を分けない（プロンプトで「関係するときだけ」。評価で問題が出たら考える） |
| P1-20 | Obsidian の手動の編集の反映 | 実装済み（T-1801 の毎日の取り込み） |
| P1-21 | 段階の監視と同期の失敗の復旧 | 保存先が 1 つのため同期はない。記録の失敗は警告と `rebuild` |
| P1-22 | 監査・削除 | `forget` で会話ごとに消す（原文と索引） |
| P2-23〜25 | ChatGPT のエクスポート、CLAUDE.md の限界 | エクスポートは CKC の `ckc/parsers/`。CLAUDE.md ではなく Jarvis の記憶パックで共通化 |
| P2-26〜28 | 原文の保持、JSON と MD の役割、保存のタイミング | 採用（原文は JSONL、知識は Markdown、記録は発言ごと） |
| P2-29 | ローカル優先 | 採用（要約・embedding は Ollama） |
| P2-30 | 品質の指標 | 採用（T-2103 の評価） |
| P2-31〜32 | 縮退と更新の安全性 | 想起の失敗は記憶なしで答える。新しい依存はない |
| P2-33〜34 | 既存の Vault と CKC | 本書 §2 で確認済み |
| P3-35〜37 | 実装の詳細・ベクトル DB・自動承認の範囲 | 本書 §2。ベクトル DB は使わない。自動承認はしない（要約は知識ではない） |
| P3-38 | 技術の定点観測 | T-2105 と、その後の定期の見直し |

## 付録B. 公開仕様の確認先（v1.0 のまま）

- Mem0 OSS Python SDK API：https://github.com/mem0ai/mem0/blob/main/skills/mem0/client/python.md
- Mem0 OSS v2→v3 移行：https://github.com/mem0ai/mem0/blob/main/docs/migration/oss-v2-to-v3.mdx
- Mem0 OSS REST API：https://github.com/mem0ai/mem0/blob/main/docs/open-source/features/rest-api.mdx
- Mem0 changelog：https://github.com/mem0ai/mem0/blob/main/docs/changelog/highlights.mdx
- Mem0 GitHub：https://github.com/mem0ai/mem0
- Letta GitHub：https://github.com/letta-ai/letta

## 付録C. 変更管理

| 版 | 日付 | 変更内容 |
|---|---|---|
| 1.0 | 2026-11-12 | 案 B（CKC 内部への Mem0 OSS の組み込み）の合意を開発仕様書に反映。 |
| 1.1 | 2026-11-12 | Owner 決定「その方針を採用」（ADR-0037）。Mem0 を最初は入れず 1003 の `memory/` で作る構成に改訂。1003 の実態の調査（§2）、v1.0 の要件との対応（§4）、Mem0 の判断の基準（§6）を追加。要件の正本をワークスペースの spec §3.3.14 に移した。 |
