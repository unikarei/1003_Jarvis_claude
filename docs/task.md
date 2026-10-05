# task.md — JARVIS タスク

状態: [ ] 未着手 / [~] 進行中 / [x] 完了

## Phase 1: CLI／定期バッチ（②AIニュース）

- [x] ノード/エッジのスキーマ設計（schema.sql）
- [x] config.py（RSSフィード一覧・設定）
- [x] db.py（初期化・ノード/エッジ操作・ID採番）
- [x] fetch_news.py（RSS取得→Sourceノード、URL重複排除）— 実フィードで動作確認済み（50件取得、再実行で重複0件）
- [~] summarize.py（Claude APIで要約→Knowledge/Taskノード）— 実装済み、ANTHROPIC_API_KEY未設定のため未検証
- [x] generate_report.py（直近KnowledgeからMarkdownレポート生成）— 動作確認済み
- [~] run_daily.py（上記を一気通貫で実行するエントリポイント）— 実装済み、summarize部分が未検証のためエンドツーエンド未確認
- [ ] Windows環境で `python run_daily.py` を実行し、実フィードで動作確認（APIキー設定後に実施）
- [ ] README・手順をWindows向けに修正
- [ ] ANTHROPIC_API_KEY を設定して要約品質を確認・調整
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
