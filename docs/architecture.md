# architecture.md — JARVIS アーキテクチャ

どう作るか。構成を変えるときは、このファイルを先に更新する。

## 1. 第0層＋5層構成

全体計画 JARVIS-MAP-002 §4 の層構成に合わせる。「現在のモジュール」は実装済みのもの、「今後」は全体計画・本リポジトリの予定。

| 層 | 役割 | 現在のモジュール | 今後 |
|---|---|---|---|
| L0 Architecture Registry | 全体構造の正本（別リポジトリ） | なし（JARVISは参照する側。`jarvis-architecture-registry` は未作成） | Registry の読み込み。Discovery / Guardian は本リポジトリの内部モジュールとして実装（MAP Phase 2〜3、ADR-0009） |
| L1 Orchestration | タスク分解・ルーティング・エージェント制御 | `run_daily.py` / `run_task.py` / `llm.py`（`ask_claude_tiered()`）/ `code_agent.py` / `agent_log.py`、定期実行（launchd / タスクスケジューラ） | Agent Manager、Task Manager、Codex / Claude 連携（MAP Phase 9） |
| L2 Data Foundation | 外部取得・Raw Data・DB・ログ | `fetch_news.py` / `fetch_search_console.py` / `fetch_ga4.py` / `ingest_insight_reporter.py` / `ingest_oarai_project_os.py` / `ingest_obsidian.py` / `github_auth.py`、SQLite（`db.py` / `schema.sql`）、`jarvis_agent.log` | 証券API、SNS API、スクレイピング、MCP |
| L3 Knowledge | 長期知識（Obsidian / Markdown / Git）・意思決定履歴 | `update_obsidian.py`（oarai-obsidian への書き込み）、`oarai_project_os.py`（Project OS への変更提案） | Obsidian 連携の拡大（MAP Phase 7） |
| L4 Intelligence | 要約・検索・分析・ナレッジグラフ | `summarize.py`、`db.py`（`search_knowledge()` / `extract_keywords()`、nodes/edges グラフ）、`ask.py`（`answer()`）、`piano_analytics.py` | Knowledge Engine（RAG・意味検索・鮮度管理）、スキルライブラリ。本リポジトリの内部モジュールとして開発（MAP Phase 7、ADR-0010） |
| L5 Presentation | 入出力 | `chat.py` / `ask.py`（CLI）、`talk.py` / `live_talk.py` / `speech_to_text.py` / `text_to_speech.py`（音声）、`generate_report.py`（Markdownレポート） | Web Summary / ACC（本リポジトリの内部モジュール、ADR-0010）、HTTP API（MAP Phase 5〜6） |

方針: L1〜L4 を固定し、L5（入出力）だけを段階的に差し替える。

旧「4層構成」との対応: インターフェース層 → L5、オーケストレーション層 → L1、ツール/統合層 → L2、メモリ層 → L3＋L4。

## 2. データモデル

- ノード型: Category / Task / Knowledge / Source / Skill / Agent
- エッジ型: belongs_to / produced / derived_from / depends_on / uses / executed_by
- 保存: SQLite（`nodes`、`edges` の2テーブル、定義は `schema.sql`）
- ID規則: `型_カテゴリ_日時_短縮ID`
- Categoryスラッグ: piano / news / trading / realestate / manufacturing / minpaku

## 3. モジュール構成（Phase 1）

- `config.py`: フィード一覧・設定
- `db.py`: DB初期化、ノード/エッジ操作、ID採番
- `llm.py`: Claude API呼び出しの共通ヘルパー（`ask_claude()`）。summarize.py / ask.py から利用
- `fetch_news.py`: RSS取得 → Sourceノード
- `summarize.py`: 要約 → Knowledgeノード、Taskノード
- `generate_report.py`: Markdownレポート生成
- `run_daily.py`: エントリポイント

## 3.1 モジュール構成（Phase 2）

- `db.py`: `search_knowledge()` を追加（キーワードLIKE検索＋直近N日フォールバック）
- `ask.py`: 質問文を受け取り、`search_knowledge()` の結果をコンテキストとして`llm.ask_claude()`に渡し、出典URL付きで回答するCLI

## 3.2 モジュール構成（Phase 3）

- `speech_to_text.py`: faster-whisperで音声ファイル（wav）を文字起こし
- `text_to_speech.py`: VOICEVOX ENGINE（ローカルHTTP API）でテキストを音声ファイル（wav）に合成
- `talk.py`: 音声ファイル → `speech_to_text` → `ask.answer()` → `text_to_speech` → 音声ファイル、を接続するCLI（ファイルベース検証用）
- `live_talk.py`: 実機マイクで固定時間録音 → `talk.talk()` を呼び出し → 既定スピーカーで再生するCLI（Windows実機検証用）

## 3.3 モジュール構成（Phase 4）

- `llm.py`: `ask_claude_tiered()` を追加（軽量レイヤー既定、検証失敗時または`force_escalate=True`時のみ高精度レイヤーへエスカレーション）
- `agent_log.py`: エスカレーション・サブエージェント委譲の履歴を `jarvis_agent.log` に記録する共通ヘルパー
- `code_agent.py`: コードタスクをOpenAI APIに委譲するサブエージェント（`OPENAI_API_KEY`使用）
- `db.py`: `get_or_create_agent()` / `find_skill_by_title()` を追加（Agent/Skillノードの取得・再利用）
- `run_task.py`: タスク説明文をコード関連／ナレッジ関連に簡易分類し、`code_agent.py` または `ask.answer()` に振り分けて実行。Task→uses→Skill、Task→executed_by→Agentのグラフを記録するCLI

## 3.4 モジュール構成（他カテゴリ横展開: ③④⑤⑥）

- `config.py`: `RSS_FEEDS` に trading / realestate / manufacturing / minpaku のフィードを追加
- `fetch_news.py` / `summarize.py`: 変更なし（category列ベースで既に汎用的）
- `generate_report.py`: `write_report()` がカテゴリをファイル名に含めるよう変更（`reports/{category}_{日時}.md`）
- `run_daily.py`: フィードから対象カテゴリ一覧を取得し、カテゴリごとに `write_report(category=...)` を呼び出すループに変更

## 3.5 モジュール構成（民泊: 個人Obsidianボルト取り込み）

- `ingest_obsidian.py`: GitHub API（`gh auth token` で認証、privateリポジトリ対応）経由で `unikarei/oarai-obsidian` の `民泊@大洗/` 配下Markdownノートを取得し、Source/Knowledgeノード（category=minpaku）として保存する。要約は行わず全文をそのまま保存する。既存ノートの内容が変わっていれば新規作成せずcontentを更新する
- `update_obsidian.py`: `create_note()` / `append_note()` でボルトへ書き込む（手動トリガーのみ）。GitHub Contents APIのsha突き合わせで同時編集による上書きを防止する
- `db.py`: `extract_keywords()`（janomeで質問文から名詞を抽出）を追加し、`search_knowledge()` は抽出したキーワードで検索するよう変更（全履歴対象。直近N日フォールバックはキーワード不一致時のみ）。`update_node_content()` / `find_knowledge_by_url()` を追加（既存ノート更新の反映用）

## 3.6 モジュール構成（①ピアノ教室: アクセス分析〜改善提案）

- `config.py`: `GOOGLE_APPLICATION_CREDENTIALS` / `SEARCH_CONSOLE_SITE_URL` / `GA4_PROPERTY_ID`（実装済み）に加え、`PIANO_LOCAL_KEYWORDS`（地域クエリ判定用）/ `PIANO_BRAND_KEYWORDS`（ブランドクエリ判定用）を追加
- `fetch_search_console.py`: `get_search_console_summary()`（実装済み）に加え、`get_query_performance()` / `get_page_performance()` / `get_device_performance()` / `get_search_console_report()`（期間比較付き）を追加
- `fetch_ga4.py`: `get_ga4_summary()`（実装済み）に加え、`get_ga4_report()`（期間比較付き）/ `get_ga4_top_pages()` / `get_ga4_traffic_sources()` / `discover_ga4_events()`（実装済みイベントの発見用）を追加
- `piano_analytics.py`（新規）: クエリ分類（表示回数多×CTR低、掲載順位4〜15位、順位・クリック数の増減、地域/ブランドクエリ）、レポート文の組み立て、`llm.ask_claude_tiered()`による優先度順Top3改善提案の生成、Source/Knowledge保存、エラー時のフォールバック（片方のAPIのみで継続）を行う`run_piano_analysis()`を提供
- `run_daily.py`: 既存のRSSパイプラインに加え`piano_analytics.run_piano_update()`を呼び出す（失敗しても全体は止めない）。レポート対象カテゴリに`piano`を追加
- `generate_report.py`: 変更なし（Knowledgeをそのまま表示する既存の仕組みを流用）

## 3.7 モジュール構成（外部プロジェクト連携: 1004_Insight_Reporter）

- `ingest_insight_reporter.py`（新規）: `1004_Insight_Reporter/reports/<report_id>/latest.json` を読み取り、`is_sufficient()` で鮮度・必須フィールドを判定した上でJARVISのSource/Knowledge（category=対応カテゴリ）として取り込む`ingest_report(report_id)`を提供。JARVISからInsight Reporter配下への書き込み・importは行わない
- `piano_analytics.py`: `run_piano_update()` を追加。`ingest_insight_reporter.ingest_report("piano_growth")` を優先し、不十分な場合のみ既存の`run_piano_analysis()`（JARVIS独自取得）にフォールバックする
- `config.py`: `REPORT_CATEGORY_MAP`（report_id→categoryの対応）、`MAX_REPORT_AGE_DAYS`（既定10日）等、Insight Reporter連携用の設定を追加

## 3.8 モジュール構成（民泊: oarai-project-os連携）

- `ingest_oarai_project_os.py`（新規）: GitHub API（`gh auth token`認証）で`unikarei/oarai-project-os-since2026`の`data/*.json`・`docs/**/*.md`を取得し、Source/Knowledge（category=minpaku）として保存する。`ingest_obsidian.py`と同じ更新検知ロジック（内容が変わっていればcontentを更新）を使う
- `oarai_project_os.py`（新規）: `build_ingestion_envelope()` / `propose_change()`でingestion envelope（oarai-project-os側API仕様と同スキーマ）を構築し、`check_api_health()`でAPI疎通を確認した上で、疎通可能なら`POST /api/ingest`、不可能ならJARVISのDBにTaskノード（category=minpaku、「未送信」）として保持する`submit_or_hold()`を提供。`resubmit_pending_proposals()`で保留分の再送信を行う
- `config.py`: `OARAI_PROJECT_OS_API_BASE`（`https://oarai.rebirth-lab.site`）等を追加

## 3.9 モジュール構成（CKC Phase 1、ローカル Vault 書き込み）

設計の詳細は `../docs/architecture.md` §10（モジュールの一覧、データの正本、競合の防止、品質管理、本番環境の保護）。

- `vault.py`（新規）: Vault の resource_id からパスを解決し、ローカル Vault に安全に書き込む。`update_obsidian.py` と `ckc/publish.py` が使う
- `update_obsidian.py`（改修）: 書き込み先をローカル Vault に変更。関数名・CLI は変えない
- `llm.py`（追加）: `complete(prompt)` とプロバイダー（`claude_cli` / `anthropic_api`）の選択。既存の関数は変えない
- `ckc/`（新規パッケージ）: `models` / `secrets` / `capture` / `extract` / `schema` / `route` / `reconcile` / `render` / `store` / `publish`
- `ckc_cli.py`（新規）: CKC の CLI。`chat.py` に「ナレッジ化」のコマンドを追加
- `config.py`: `JARVIS_DB_PATH`・`JARVIS_VAULT_ROOT`・`JARVIS_CKC_DATA_DIR`・`JARVIS_LLM_PROVIDER` の環境変数での差し替え、`VAULTS`・`PROJECTS`
- `schema.sql`: CKC 用のテーブル（`ckc_conversations`・`ckc_candidates`）を `CREATE TABLE IF NOT EXISTS` で追加。既存の `nodes`・`edges` は変えない
- テスト: `tests/`（pytest、開発用依存 `requirements-dev.txt`）

## 4. 実行基盤

- 開発・実行の中心: Mac mini M4 Pro（48GB）。Windows PC は Tailscale / SSH 経由の操作端末（JARVIS-MAP-002 §3）
- これまでの実装・動作確認は Windows 上で実施。Phase 1・2 は Mac mini で動作確認済み（2026-10-11、`run_daily.py`・`ask.py`・Google API 接続）。2026-10-11 から Mac mini の `jarvis.db` を正本とし、定期実行も Mac mini（launchd）に移した。Windows の定期実行は停止済み
- macOS（python.org 版 Python）では「Install Certificates.command」で CA ファイルを用意しておく（Mac mini は実行済み）。feedparser は証明書エラーを例外にせず空の結果を返すため、未実行だと RSS 取得が 0 件になる
- 今後: Docker 化（独立した `Dockerfile` / `compose.yaml`、永続データは Volume）と HTTP API 化（`/health`、`/api/v1/`）を全体計画に沿って行う（MAP Phase 4〜5）
- LLM: 機密性の高い処理はローカルLLM、高精度が必要な処理はクラウドAPI（Claude）のハイブリッド
- 音声（Phase 3）: 認識はfaster-whisper等、合成はVOICEVOX等をローカル実行

## 5. エージェント・APIルーティング（コスト最適化優先）

方針: 運用コストの最小化を最優先し、タスクの複雑度に応じてモデルを自動選択する。モデル名は固定せず「その時点で最も安価な軽量モデル／最も高精度なモデル」という相対的な位置づけで選定する。

- 司令塔（オーケストレーター）: 対話解析・意図理解・タスク分解・Tool Use制御
  - 基本レイヤー: 最も安価な軽量モデルで日常応答・単純な意図解析を処理
  - 高度レイヤー: 検証失敗／多段推論／意図が曖昧／ユーザーの明示要求、のいずれかに該当する場合のみ最も高精度なモデルへエスカレーション
- コード実行サブエージェント: コード生成・リファクタリング・自動デバッグ・スクリプト実行を専任。コード特化・低単価・高速なAPI（例: OpenAI Codex系）を使用し、オーケストレーターからの委譲を受けて結果のみを返す。複雑度に関わらず常にこのサブエージェントへ委譲する（オーケストレーターの管轄外）
- エスカレーション・委譲の履歴はログに残し、コストを追跡できるようにする
- 外部API: Claude API（`ANTHROPIC_API_KEY`、必須） / コード実行サブエージェント用API（`OPENAI_API_KEY`、導入時に追加）

## 6. セキュリティ設計

- 秘密情報は環境変数、`.gitignore` で `jarvis.db` / `reports/` / `.env` / `*.log` を除外
- 最小権限、読み取り専用をデフォルトにする
- 取り返しのつかない操作は承認キューを挟む
- 出所不明のMCPサーバ・パッケージは導入しない
