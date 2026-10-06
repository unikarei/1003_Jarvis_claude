# architecture.md — JARVIS アーキテクチャ

どう作るか。構成を変えるときは、このファイルを先に更新する。

## 1. 4層構成

| 層 | 役割 | Phase 1 | Phase 2以降 |
|---|---|---|---|
| インターフェース層 | 入出力 | Markdownレポート | テキスト質問応答 → 音声（Phase 3） |
| オーケストレーション層 | タスク分解・制御 | `run_daily.py` | エージェント制御 |
| ツール/統合層 | 外部取得・投稿 | RSS取得 | 証券API、SNS API、スクレイピング、MCP |
| メモリ層 | 蓄積・検索 | SQLite（nodes/edges） | RAG（ベクトルDB）、スキルライブラリ |

方針: 下位3層を固定し、インターフェース層だけを段階的に差し替える。

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

## 4. 実行基盤

- 開発: Windows、本番: Mac mini（48GB）
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
