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
- [x] Mac miniへ移行（clone、定期実行をlaunchdに）— 2026-10-11。jarvis.db・.env をWindowsから移し、Mac miniを正本に。launchd `com.ohide.jarvis.daily-news`（毎日6:00）で新規33件の取得・要約・レポート出力を確認

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

### 4-4. 他カテゴリへの横展開（③④⑤⑥、①ピアノ教室は対象外）

- [x] config.pyのRSS_FEEDSに③trading④realestate⑤manufacturing⑥minpakuのフィードを追加 — 各フィードをfeedparserで動作確認した上で採用
- [x] generate_report.py（write_reportがカテゴリ別ファイル名・タイトルで出力）を変更
- [x] run_daily.py（カテゴリごとにレポート生成するループ）を変更
- [x] 動作確認 — run_daily.py実行で新規Source 187件・Knowledge 187件、5カテゴリ分のレポート（news/trading/realestate/manufacturing/minpaku）が`reports/`に出力されることを確認

### 4-5. 民泊: 個人Obsidianボルト（oarai-obsidian）の取り込み

- [x] db.py に extract_keywords()（janomeで質問文から名詞抽出）を追加し、search_knowledge() をキーワード抽出ベース・全履歴対象に修正
  - 発見: 質問文全体をそのままLIKE検索していたため、自然文の質問がほぼ一致しない不具合があった（「OpenAI」等の単語がたまたま含まれる場合しか機能せず）。キーワード抽出方式に修正して解消
- [x] ingest_obsidian.py（GitHub APIでMarkdownノートを取得しSource/Knowledgeを作成）を実装
  - 対象リポジトリがprivateだったため `gh auth token` で認証、Contents APIで全文取得する方式に変更
- [x] 動作確認 — 31件のノートを取り込み（テンプレート等2件は除外）、再実行で重複0件、ask.pyで大洗の物件所在地・価格を出典URL付きで正しく回答できることを確認。既存のニュース質問にも回帰なし

### 4-6. 民泊: Obsidianボルトへの書き込み

- [x] db.py に update_node_content() / find_knowledge_by_url() を実装
- [x] ingest_obsidian.py を拡張し、既存ノートの内容変更時にSource/Knowledgeを更新（新規作成しない）
- [x] update_obsidian.py（create_note() / append_note()、sha突き合わせで競合検出）を実装。gh認証取得は`github_auth.py`に共通化（ingest_obsidian.pyからも利用）
- [x] 動作確認 — サンドボックス用テストノート（90_資料/JARVISテスト.md）で新規作成・既存パスへの再作成エラー・追記・sha不一致時の409競合エラー・再取り込みでの更新反映（1件目は新規、2回目の追記は更新として検知）を確認。テスト後にGitHub側・ローカルDB側とも削除済み
- [x] 実運用で「大洗_地盤と傾斜」ノートに床水平偏差率の実測データを追記 → 発見: search_knowledge()がAND一致0件時にOR一致を「結果」「判断」等の一般語込みで単純に新しい順で返していたため、本命ノートが無関係な記事群に埋もれて見つからないケースがあった → 一致キーワード数で並べ替える方式に修正し解消（db._search_by_keywords()）。ask.pyで実測データと判断が出典URL付きで正しく回答されることを確認、既存のニュース質問にも回帰なし

### 4-1. エージェント・APIルーティング

- [x] agent_log.py（エスカレーション・委譲履歴を jarvis_agent.log に記録）を実装
- [x] llm.py に ask_claude_tiered()（軽量既定、検証失敗/force_escalate時のみ高精度へ）を実装
- [x] 動作確認 — 通常呼び出しではログが記録されず、force_escalate=True時のみ高精度レイヤー（Sonnet 5.5）が使われログに記録されることを確認

### 4-2. コード実行サブエージェント

- [x] code_agent.py（OpenAI APIでコード生成・修正・デバッグ）を実装
- [x] 実際のコードタスク（GCD関数の実装）で動作確認、agent_log.pyへの記録も確認

### 4-3. 自律タスク実行・スキル蓄積の仕組み化

- [x] db.py に get_or_create_agent() / find_skill_by_title() を実装
- [x] run_task.py（タスク分類→code_agent/ask.answerへ振り分け→Task/Skill/Agentグラフ記録）を実装
- [x] 動作確認 — コードタスク2件・ナレッジタスク2件を実行し、正しく振り分けられグラフが記録されることを確認
- [x] 同種タスクの再実行でSkillノードが再利用され、重複作成されないことを確認（4タスク実行してもSkill/Agentは各2件のみ）

## ①ピアノ教室運営補助: アクセス分析

対象: https://ono-piano.com/（教室ページ: https://ono-piano.com/piano.class/）。Search Console・GA4は設定済み、サービスアカウントへの権限付与はユーザー側で対応可能。

- [x] Google Cloudでサービスアカウントを作成しJSONキーを発行、Search Console・GA4プロパティに閲覧権限で追加（ユーザー側作業）— jarvis-piano-reader@onopianocom-chatbot-log.iam.gserviceaccount.com
- [x] config.py/.env/.env.exampleにGOOGLE_APPLICATION_CREDENTIALS/SEARCH_CONSOLE_SITE_URL/GA4_PROPERTY_IDを追加
- [x] fetch_search_console.py（get_search_console_summary()関数、検索パフォーマンス取得）を実装
- [x] fetch_ga4.py（get_ga4_summary()関数、アクセスデータ取得）を実装
- [x] scripts/test_google_integrations.py（接続確認・エラー分類）を実装し、両API（Search Console・GA4）の接続成功を実データで確認
  - 発見: 対象プロパティは`https://ono-piano.com/`ではなく`https://ono-piano.com/piano.class/`だった（.envのSEARCH_CONSOLE_SITE_URL修正で解消）
- [x] fetch_search_console.py拡張（get_query_performance/get_page_performance/get_device_performance/get_search_console_report、期間比較付き）— 実データで動作確認（28日間clicks 132件、前期間比+18%等）
- [x] fetch_ga4.py拡張（get_ga4_report、get_ga4_top_pages、get_ga4_traffic_sources、discover_ga4_events）
  - 発見: GA4 RunReportRequestで複数date_rangesを使う場合、`dimensions`に`dateRange`を明示すると400エラーになる（APIが自動付与するため不要）。dimensionsから除去して解消
- [x] config.pyにPIANO_LOCAL_KEYWORDS/PIANO_BRAND_KEYWORDS/PIANO_INQUIRY_EVENT_KEYWORDS/PIANO_SOCIAL_EVENT_KEYWORDSを追加
- [x] piano_analytics.py実装: クエリ分類（表示回数多×CTR低/掲載順位4〜15位/順位・クリック数増減/地域・ブランドクエリ）
- [x] piano_analytics.py実装: 問い合わせ系イベント調査（discover_ga4_eventsの結果からキーワード一致で抽出）
  - 実データでは計測整備済みと判明: contact_form_click(35件)、cta_click(50件)、phone_click/line_click/email_click等が既に実装されていた
- [x] piano_analytics.py実装: レポート文生成（前期間比、ゼロ除算・データ不足を安全に処理）— 0件データ・両API失敗のエッジケースをクラッシュなく確認
- [x] piano_analytics.py実装: generate_recommendations()（Claudeで優先度順Top3提案）
  - 発見: max_tokens=1500だと提案が途中で切れる不具合 → 2500に増やして解消。GSC・GA4を横断した具体的な提案（CTR改善、フォーム離脱率改善等）が生成されることを確認
- [x] piano_analytics.py実装: run_piano_analysis()（Search Console/GA4どちらかの失敗時も継続、Source/Knowledge保存）— unittest.mockでSearch Console障害をシミュレートし、GA4データのみで継続することを確認
- [x] run_daily.pyにpiano_analytics.run_piano_analysis()呼び出しとpianoカテゴリのレポート対象追加
- [x] テスト: Search Console単体/GA4単体/両API統合/前期間比較/0件データ/APIエラー/問い合わせイベントなし/レポート生成の各パターンを確認
- [x] 実データで1回実行し、実測値と改善提案Top3を確認 — run_daily.py実行で6カテゴリ（piano含む）のレポートが生成され、ask.pyでも実データに基づく回答（検索キーワード・改善施策）を出典付きで確認

## 外部プロジェクト連携（1004_Insight_Reporter）

- [x] config.pyにINSIGHT_REPORTER_DIR/REPORT_CATEGORY_MAP/MAX_REPORT_AGE_DAYSを追加
- [x] ingest_insight_reporter.py実装: load_latest_report() / is_sufficient() / ingest_report()
- [x] piano_analytics.pyにrun_piano_update()（Insight Reporter優先、不十分ならrun_piano_analysis()にフォールバック）を実装
- [x] run_daily.pyをrun_piano_update()呼び出しに変更
- [x] 動作確認:
  - latest.json不在時（現状の実態）: フォールバックしてJARVIS独自のSearch Console/GA4取得が実行されることを実データで確認
  - 鮮度切れ（20日前）・必須フィールド欠落のレポート: is_sufficient()がFalseを返しフォールバックすることを一時ディレクトリでのモックで確認（1004_Insight_Reporter配下には書き込まず検証）
  - 十分なレポートが存在する場合: Source/Knowledgeとして取り込まれ、出典がInsight Reporterであることがtitle/urlに明記されることをモックで確認
  - 1004_Insight_Reporter側はPiano Growthレポートが未実行（reports/配下が空）のため、実レポートでの取り込みは今後の検証課題として残る

## 民泊: oarai-project-os連携

- [x] config.pyにOARAI_PROJECT_OS_REPO/OARAI_PROJECT_OS_API_BASEを追加
- [x] db.pyにupdate_node_title() / find_nodes_by_title_prefix()を追加（保留提案の管理用）
- [x] ingest_oarai_project_os.py実装: data/*.json・docs/**/*.mdの取得、Source/Knowledge化、更新検知
  - 実データで動作確認: 25件取り込み（data/*.json 11件、docs配下14件）、再実行で重複0件
  - ask.pyで「大洗Project OSの現在のフェーズと未解決のブロッカー」を質問し、tasks.json/risks.json/project_status.mdを横断した正確な回答（出典付き）を確認
- [x] oarai_project_os.py実装: build_ingestion_envelope() / propose_change() / check_api_health() / submit_envelope() / submit_or_hold() / resubmit_pending_proposals()
- [x] 動作確認:
  - check_api_health(): 本番API（現状500エラーで停止中）に対し実際にFalseを返すことを確認
  - submit_or_hold(): API未応答時、提案がJARVISのDBにTaskノード（category=minpaku、「未送信」）として保留されることを実際に確認
  - resubmit_pending_proposals(): requests.get/postをモックしてAPI復旧を再現し、保留中の提案が送信され、タイトルが「送信済み」に更新されること、再走査で二重送信されないことを確認（本番サーバーには一切アクセスしていない）
  - テスト後、モックで生成した「送信済み」の虚偽状態を含むテストノードは削除済み

## 対話型CLI（chat.py）

- [x] chat.py実装: run_task.py（ナレッジ/コード分類）をバックエンドにした対話ループ（exit/quit/Ctrl+Cで終了）
  - 発見: Windowsで標準入出力がリダイレクトされるとコンソールのコードページ（cp932等）とUTF-8が不一致になりUnicodeEncodeErrorになる不具合 → `sys.stdin/stdout.reconfigure(encoding="utf-8")`で解消
  - 動作確認: 「大洗の物件の所在地は？」（Obsidian・oarai-project-os両方のSourceを横断した正確な回答）、「Pythonでリストの中央値を求める関数を実装して」（code_agent経由でコード生成）をUTF-8ファイル経由の入力で確認

## 保守

- [x] リファクタリング（機能変更なし）: db.pyのコネクション処理を`_fetchall`/`_fetchone`/`_execute`に共通化、Claude API呼び出しを`llm.py`の`ask_claude()`に統合（summarize.py/ask.pyの重複排除）、live_talk.pyがtalk.pyの`talk()`を再利用するよう変更。既存の動作（search_knowledge/get_recent_knowledge/ask.answer()）に回帰がないことを確認済み

## 全体計画（JARVIS-MAP-002）との整合

全体計画に合わせるための作業。着手前に spec.md / architecture.md を更新する。

- [x] ドキュメントを第0層＋5層構成・Mac mini中心の構成に更新（AGENTS.md / architecture.md / spec.md）。CLAUDE.md を AGENTS.md に移し、CLAUDE.md は `@AGENTS.md` のみにした
- [x] Mac mini 上で依存をインストールし、`run_daily.py` / `ask.py` の動作を確認 — 2026-10-11。`.venv`（Python 3.13.9、uv）に requirements.txt を導入。ask.py で出典付き回答、run_daily.py で収集〜レポート出力、scripts/test_google_integrations.py で Search Console・GA4 接続を確認
  - 発見: python.org 版 Python では HTTPS の証明書検証が失敗し、feedparser が例外を出さずに全フィード0件を返していた → 付属の「Install Certificates.command」を実行して Mac 全体で解消（一時的に `.env` の `SSL_CERT_FILE` で回避していたが削除済み）
- [ ] Mac mini 上で `chat.py` と Phase 3（音声）・Phase 4 の動作を確認
- [x] 定期実行を launchd に登録 — 2026-10-11。`~/Library/LaunchAgents/com.ohide.jarvis.daily-news.plist`（毎日6:00）。Windowsのタスクスケジューラは停止済み
- [x] `config.py` の `INSIGHT_REPORTER_DIR`（`1004_Insight_Reporter`）を実ディレクトリ名 `1004_insight_reporter` に合わせる — 2026-10-12。実ディレクトリ名と完全一致すること、`ingest_insight_reporter.load_latest_report("piano_growth")` が読めることを確認（macOS 既定の大文字小文字を区別しないファイルシステムでは動くが、Docker 等の Linux 上では見つからない）
- [ ] Docker 化（MAP Phase 4）: `Dockerfile` / `compose.yaml`、DB・レポート・ログの永続 Volume、Healthcheck
- [ ] HTTP API 化（MAP Phase 5）: `/health`、`/api/v1/`、OpenAPI 仕様
- [ ] Architecture Registry の参照（MAP Phase 2 で Registry 作成後）

## CKC Phase 1（Conversation Knowledge Collector、MAP 外の追加）

タスク・受け入れ基準の正本は `../docs/tasks.md`（T-101〜T-117）と `../docs/testing.md`。ここには状態だけを書く。開発は worktree（`../.worktrees/1003-ckc`、ブランチ `feat/ckc-phase1`）で行う。

- [x] T-101 docs（spec 4-10、architecture 3.9、本節）
- [x] T-102 テストの基盤（開発用 `.venv`、pytest、本番パスの検出、パスの環境変数）
- [x] T-103 データ型と契約テスト
- [x] T-104 秘密情報の検出（Codex）
- [x] T-105 ノートの生成と読み取り（Codex）
- [x] T-106 抽出結果の検証（Codex）
- [x] T-107 LLM の共通関数
- [x] T-108 ローカル Vault への書き込みと `update_obsidian.py` の改修
- [x] T-109 記録（CKC 用テーブル）
- [x] T-110 取り込み
- [x] T-111 抽出
- [x] T-112 振り分けと照合
- [x] T-113 保存
- [x] T-114 CLI
- [x] T-115 `chat.py` との連携
- [x] T-116 受け入れテスト（2026-10-12。本物の claude -p・jarvis-knowledge・Mac/スマートフォン間の同期で手順 1〜9・11 を確認。手順 10 の日次処理は main マージ後の 6:00 の実行で確認）
- [ ] T-117 ChatGPT・Claude のエクスポートの取り込み（Codex、任意）

## Architecture Registry との接続（T-310、MAP Phase 2）

- [x] `registry.py`: Registry の `generated/registry.json` から Vault のパス（この端末の locator）とプロジェクトを読む。読めないときは `config.FALLBACK_*` で動き、警告を1回出す。`JARVIS_VAULT_ROOT` がある開発・テストでは VAULT_ROOT の下を使う
- [x] `vault.resolve_vault()`・CKC（route・extract・review・publish）を `registry.py` に切り替え
- [ ] main へのマージ（Registry の T-308 で registry.json が生成されてから。生成前にマージすると、本番が毎回「予備の設定値で動く」警告を出すため）

## Obsidian の取り込みをローカルの Vault に切り替え（T-202、ADR-0016）

- [x] `ingest_obsidian.py`: GitHub API ではなくローカルの Vault（Registry から場所を取得）を読む。oarai-obsidian は `民泊@大洗/`、jarvis-knowledge は `_jarvis/` を除く全体。URL は従来の GitHub の blob URL と同じ形（既存ノードと重複しない、ファイル名は NFC）。Git は操作しない

## Discovery / Guardian / Mermaid（MAP Phase 3、T-401〜T-405）

設計は `../docs/architecture.md` §11。

- [x] T-401 `discovery/collect.py`（読み取りだけ、fetch なし、1件の失敗で全体を止めない）
- [x] T-402 `discovery/guardian.py`（Verified / Failed / Unknown、未登録・存在しない・停止・障害・同期・バックアップ・依存の不整合）
- [x] T-403 `discovery/output.py`・`discover.py`（latest.json・guardian-report.md・architecture.mmd・STATUS.md、`--dry-run`）
- [x] T-404 `scripts/commit_registry_outputs.sh`（対象の4ファイルだけ、main のみ、強制 push なし）、`scripts/launchd/com.ohide.jarvis.discovery.plist`（毎日 7:00。**登録は Owner の承認後**）
- [ ] T-405 受け入れ（2026-10-13 未明に実データで実行し Registry に記録 f37ab70。Owner の確認待ち）

## Architecture Control Center（ACC、MAP Phase 6、静的版）

- [x] `acc/build.py`: Registry と Discovery の結果から、問題・Project View・Infrastructure View・Layer View・全体構成図を1つの HTML に（`python -m acc.build`、既定の出力は `reports/acc/index.html`）
- [ ] Tailscale 経由での配信（ネットワークへの公開のため Owner の承認待ち）
- [ ] React / React Flow の対話型画面（npm の依存の追加のため Owner の承認待ち）

## API 連携（MAP Phase 5）

- [x] `insight_reporter_api.py`: Insight Reporter の `/api/v1` のクライアント（接続先は Registry、`ApiUnavailable`・`ApiError` の区別、任意のトークン）
- [x] `ingest_insight_reporter.load_latest_report()`: API → ファイルの順に取得（API が止まっていても従来どおり動く）
- [ ] Jarvis 自身の API（`/health`・`/api/v1/`）: Web フレームワーク（FastAPI）の追加が必要なため、Owner の承認待ち

## Knowledge Engine（MAP Phase 7）

- [x] `knowledge_engine.py`・`knowledge_cli.py`: プロジェクト横断の検索（BM25、プロジェクトで絞り込み）、出典の追跡（Vault・Registry・会話まで）、重複と鮮度の監査、検索結果だけを根拠に答える（RAG、llm.complete）
- [ ] 意味検索（埋め込み）: ローカル LLM（Ollama）の埋め込みモデルの導入が必要なため Owner の承認待ち

## Knowledge Architect（MAP Phase 8）

- [x] `architect.py`: 既存資産（知識・Registry の Project と Resource）の再利用の提案、必要なら新しい Project・保存先・ノートの構成の提案（LLM の出力は検証）。適用は確認の後だけで、ノートの雛形は Vault の `_jarvis/themes/<project_id>/`、Registry への Project の追加は一時 worktree から PR（マージは Owner）
- [x] `chat.py`: 「テーマ登録: <テーマ>」で提案 → 確認 → 適用

## Multi-Agent Governance（MAP Phase 9）

- [x] `governance.py`: board（tasks.md と Registry に登録されたリポジトリの PR から、着手できるタスク・承認待ち・PR の注意点（積み重ね・同じファイル・古い PR））、prompt（Codex 向けの指示の下書き）、review（PR の差分の LLM レビュー案。投稿しない）

## 自律改善・継続運用（MAP Phase 10）

- [x] `improve.py`: 構成（Guardian）・知識（重複・鮮度）・開発（ボード・PR）・技術的負債（TODO・長いファイル・マージ済みのブランチ）・バックアップを監査し、改善候補を根拠付きで優先度順に出す。開発の再開の要約（progress.md）も付ける
- [x] ACC に「改善候補と開発の再開」を追加（一画面で開発を管理）
- [x] `scripts/launchd/com.ohide.jarvis.improve.plist`（毎週月曜 7:30。**登録は Owner の承認後**）

## 定期処理の失敗の通知（T-203）

- [x] `discovery/notify.py`・`discover.py --notify`: Guardian がエラーを見つけたら Mac の通知センターに出す（同じ内容は1日1回。外部のサービスには送らない）。Discovery の plist に `--notify` を追加


## Phase 承認レビューの指摘の解消（2026-10-13、`../docs/phase-approval-review.md`）

- [x] T-1004 `governance.py`: 依存の欄の 4 桁のタスク ID（T-1002 など）と範囲（T-401〜T-403）を読む。未完了の 4 桁 ID に依存するタスクを着手可能にしない（回帰テスト）
- [x] T-1104 `improve.py`: 開発の再開の要約が、日付の注記付きの見出し（`## 現在の状態（…）`）と最後の節でも読めるようにする（回帰テスト）
- [x] T-902 `architect.py`: LLM の提案の検証を強化（提案全体・配列の各要素・必須項目・真偽値・型を検査し、不正な構造を日本語のエラーで拒否。`reuse: [null]`・`notes: [{}]`・id が `[]`・`{}` でも例外にならない）
- [x] T-903 新しいテーマの適用の受け入れ（本番とは別の Vault・Registry・DB のコピー）: 本物の claude -p で新しい Project `home-solar` の提案 → 適用（ノートの雛形 5 件、一時 worktree でブランチ・コミット・ローカルの bare リポジトリへ push）→ Owner のマージ（模擬）→ validate・render → `registry.projects()` で参照、を確認。GitHub への PR 作成（gh）だけは外部への書き込みのため実行せず、呼び出し内容を記録
- [x] T-312（1003 分）: 本番で動いている `com.ohide.jarvis.daily-news.plist` を `scripts/launchd/` に保存（再構築の手順書の入手先。秘密情報なし。登録の手順は README）

## スキルと会話からの実行（FR-014 Phase 1、`../docs/tasks.md` §12）

- [x] T-1205 設計の更新（spec.md 4-11、architecture.md 3.10、本節）
- [x] T-1206 `skills.py`（索引の読み込みと予備の索引）
- [x] T-1207 `orchestrator.py`（ルール → LLM の振り分け、入力の検証、権限の確認、実行）
- [x] T-1208 `chat.py` の切り替え（振り分け、普通の質問は Knowledge Engine、会話の文脈）
- [x] T-1209 実行の記録（Task → uses → Skill、成否・所要時間・振り分けの方法）
- [x] T-1211 開発レビューの反映（確認の必須化、最低限の権限、時間の上限、記録の伏せ字）
- [x] T-1210 受け入れ（`../docs/testing.md` §9）: 本番とは別の DB・Vault で本物の claude -p を使い、12 の発言を確認。見つかった 2 点（Owner の発言が失敗時に材料から抜ける、決定の発言を LLM がナレッジ化に振り分ける）を修正し再確認

## 定期実行の初回で見つかった不具合（2026-10-14）

- [x] `discovery/collect.run`: コマンドがない（launchd の PATH）・時間切れでも例外にせず、失敗の終了コードを返す（T-401 の「収集の例外で全体を止めない」）
- [x] 定期ジョブそのものの失敗の通知: `discover.py --notify` は例外・0 以外の終了コードも通知、`improve.weekly()`（改善の監査 → ACC）も失敗を通知。launchd の plist の PATH に `/usr/local/bin`（docker）を追加

## 応答の速さ（FR-014、`../docs/tasks.md` T-1212、2026-10-14 Owner 承認）

- [x] LLM での振り分けを既定で無効（`JARVIS_ROUTE_WITH_LLM=1` で有効）。ルールに当たらない発言は普通の質問として答える
- [x] 回答の逐次表示: `llm.complete_stream`（claude -p の stream-json）、`knowledge_engine.answer(on_text=)`、`orchestrator.Context.stream`、`chat.py`
- [x] システムの状態は前回の照合結果（Registry の guardian-report.md）の要約。「最新」「今の」などのときだけその場で照合
- [x] テストで本物の claude -p を逐次出力で呼ばない安全策（conftest）
