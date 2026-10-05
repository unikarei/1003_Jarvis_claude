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
- `fetch_news.py`: RSS取得 → Sourceノード
- `summarize.py`: 要約 → Knowledgeノード、Taskノード
- `generate_report.py`: Markdownレポート生成
- `run_daily.py`: エントリポイント

## 4. 実行基盤

- 開発: Windows、本番: Mac mini（48GB）
- LLM: 機密性の高い処理はローカルLLM、高精度が必要な処理はクラウドAPI（Claude）のハイブリッド
- 音声（Phase 3）: 認識はfaster-whisper等、合成はVOICEVOX等をローカル実行

## 5. セキュリティ設計

- 秘密情報は環境変数、`.gitignore` で `jarvis.db` / `reports/` / `.env` / `*.log` を除外
- 最小権限、読み取り専用をデフォルトにする
- 取り返しのつかない操作は承認キューを挟む
- 出所不明のMCPサーバ・パッケージは導入しない
