# task.md — JARVIS タスク

状態: [ ] 未着手 / [~] 進行中 / [x] 完了

## Phase 1: CLI／定期バッチ（②AIニュース）

- [x] ノード/エッジのスキーマ設計（schema.sql）
- [x] config.py（RSSフィード一覧・設定）
- [x] db.py（初期化・ノード/エッジ操作・ID採番）
- [x] fetch_news.py（RSS取得→Sourceノード、URL重複排除）— 実フィードで動作確認済み（50件取得、再実行で重複0件）
- [x] summarize.py（Claude APIで要約→Knowledge/Taskノード）— 動作確認済み（Source50件→Knowledge50件、Haiku 4.5で要約品質良好）
- [x] generate_report.py（直近KnowledgeからMarkdownレポート生成）— 動作確認済み
- [x] run_daily.py（上記を一気通貫で実行するエントリポイント）— fetch→summarize→reportの各関数を個別に動作確認済み（run_daily.py自体の通し実行は未実施）
- [x] ANTHROPIC_API_KEY を設定して要約品質を確認・調整 — `.env`＋`python-dotenv`で読込、Haiku 4.5で良好な要約を確認
- [ ] Windows環境で `python run_daily.py` を実行し、通しで動作確認
- [ ] README・手順をWindows向けに修正
- [ ] 定期実行の設定（タスクスケジューラ）
- [ ] Mac miniへ移行（clone、定期実行をlaunchdに）

## Phase 2: テキスト質問応答

- [ ] 検索対象とクエリ仕様を spec.md に追記
- [ ] Knowledge検索（SQLite）の実装
- [ ] 回答に出典URLを付ける
- [ ] 必要ならRAG（ベクトルDB）を導入

## Phase 3: 音声対話

- [ ] 音声認識（faster-whisper等）の選定と動作確認
- [ ] 音声合成（VOICEVOX等）の選定と動作確認
- [ ] Phase 2の窓口に入出力を接続

## Phase 4以降

- [ ] 他カテゴリ（①③④⑤⑥）への横展開
- [ ] 自律タスク実行・スキル蓄積の仕組み化
- [ ] エージェント・APIルーティング（コスト最適化・階層型エスカレーション）の実装。詳細はCLAUDE.md/architecture.md参照
  - [ ] 軽量レイヤー／高精度レイヤーの切替ロジック
  - [ ] コード実行サブエージェントの導入（`OPENAI_API_KEY`を環境変数に追加）
  - [ ] エスカレーション・委譲のログ記録（コスト追跡）
