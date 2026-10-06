# task.md — JARVIS タスク

状態: [ ] 未着手 / [~] 進行中 / [x] 完了

## Phase 1: CLI／定期バッチ（②AIニュース）

- [x] ノード/エッジのスキーマ設計（schema.sql）
- [x] config.py（RSSフィード一覧・設定）
- [x] db.py（初期化・ノード/エッジ操作・ID採番）
- [x] fetch_news.py（RSS取得→Sourceノード、URL重複排除）— 実フィードで動作確認済み（50件取得、再実行で重複0件）
- [x] summarize.py（Claude APIで要約→Knowledge/Taskノード）— 動作確認済み（Source50件→Knowledge50件、Haiku 4.5で要約品質良好）
- [x] generate_report.py（直近KnowledgeからMarkdownレポート生成）— 動作確認済み
- [x] run_daily.py（上記を一気通貫で実行するエントリポイント）— 通し実行確認済み（exit 0、fetch→summarize→reportの順で正常動作）
- [x] ANTHROPIC_API_KEY を設定して要約品質を確認・調整 — `.env`＋`python-dotenv`で読込、Haiku 4.5で良好な要約を確認
- [x] Windows環境で `python run_daily.py` を実行し、通しで動作確認 — reports/に50件のKnowledgeを含むレポートを出力
- [x] README・手順をWindows向けに修正 — セットアップ/実行/タスクスケジューラ登録手順を記載
- [x] 定期実行の設定（タスクスケジューラ）— タスク名`JARVIS_DailyNews`、毎朝6:00に登録済み。作業ディレクトリ非依存であることも確認済み
- [ ] Mac miniへ移行（clone、定期実行をlaunchdに）

## Phase 2: テキスト質問応答

- [x] 検索対象とクエリ仕様を spec.md に追記
- [x] db.py に search_knowledge()（キーワードLIKE検索＋直近N日フォールバック）を実装
- [x] ask.py（質問受付→検索→Claude APIで回答生成、出典URL付き）を実装
- [x] 実際の質問で動作確認 — 「OpenAIに関する最近のニュース」で出典URL付き回答、「東京の天気」で該当なし・ハルシネーションなしを確認
- [ ] 必要ならRAG（ベクトルDB）を導入

## Phase 3: 音声対話

- [x] 検証範囲をファイルベースI/Oに限定し spec.md に明記（実機マイク/スピーカーは本番環境で確認）
- [x] speech_to_text.py（faster-whisperで音声ファイル→テキスト）の実装と動作確認 — Windows SAPIで生成した日本語音声で文字起こし成功
- [x] text_to_speech.py（VOICEVOX ENGINEでテキスト→音声ファイル）の実装 — ENGINE未起動時に明確なエラーを返すことを確認（実際の音声合成はENGINE起動後にユーザー側で確認）
- [x] talk.py（音声ファイル→文字起こし→ask.answer()→音声合成）の実装 — STT→ask部分は動作確認済み。TTS部分はVOICEVOX ENGINE起動後に確認
- [x] VOICEVOX ENGINEをWindows開発機にセットアップ — v0.25.2（windows-cpu版、.vvppはzip形式）を`voicevox_engine/`に展開、`run.exe`起動でHTTP API稼働確認（既定スピーカーIDをid=3 ずんだもん・ノーマルに修正）
- [x] live_talk.py（マイク録音→talk相当処理→スピーカー再生）の実装
- [x] Windows実機でのマイク入力・スピーカー出力の動作確認 — 実際に話しかけた内容が文字起こしされ、Claude APIの回答がスピーカーから再生されることを確認（「生成AI」が「生成屋」と誤認識される等、小型Whisperモデルの精度課題はあるが、音声対話のループ自体は成立）
- [ ] Mac miniへの移行（Windowsでの検証完了後）

## Phase 4以降

- [ ] 他カテゴリ（①③④⑤⑥）への横展開
- [ ] 自律タスク実行・スキル蓄積の仕組み化
- [ ] エージェント・APIルーティング（コスト最適化・階層型エスカレーション）の実装。詳細はCLAUDE.md/architecture.md参照
  - [ ] 軽量レイヤー／高精度レイヤーの切替ロジック
  - [ ] コード実行サブエージェントの導入（`OPENAI_API_KEY`を環境変数に追加）
  - [ ] エスカレーション・委譲のログ記録（コスト追跡）
