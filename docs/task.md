# task.md — JARVIS タスク

状態: [ ] 未着手 / [~] 進行中 / [x] 完了

## Phase 1: CLI／定期バッチ（②AIニュース）

- [x] ノード/エッジのスキーマ設計（schema.sql）
- [x] config.py（RSSフィード一覧・設定）
- [x] db.py（初期化・ノード/エッジ操作・ID採番）
- [x] fetch_news.py（RSS取得→Sourceノード、URL重複排除）— 実フィードで動作確認済み（50件取得、再実行で重複0件）
  - [x] 題名の重複の排除（`../docs/tasks.md` T-803、PR #17）: 直近30日の同じカテゴリ・同じ題名をスキップ。2026-10-16 修正: 保存済みの題名も NFKC・空白をそろえて比べる（全角スペースの残る古い題名で再配信がすり抜けた）
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
  - [x] T-117 変換モジュール（`ckc/parsers/`。架空の fixture でテスト。capture・CLI への接続と実際のエクスポートでの確認は未実施）

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
- [x] 開発オーケストレーターの操作画面（`../docs/tasks.md` T-1305、`../docs/dev-orchestrator-design.md` §16）: `python -m acc.dev_console` を VS Code のターミナルで起動（常駐しない）。指示書を選んで開始・進捗・ログとテスト結果・指摘と修正の回数・停止・状態・PR のリンク。オーケストレーターは決まった引数の CLI で呼ぶだけ。127.0.0.1 に固定、起動ごとのトークン（Cookie）、Host・CSRF の確認、指示書は `docs/codex/` のコミット済みで未変更のものだけ、ログは秘密情報を伏せる。静的なページにも最近の実行（読み取りだけ）を出す。マージ後の「作業フォルダを削除」（判定と削除はオーケストレーターの `worktrees`・`cleanup`、§16.5）と使い方のヘルプ（?）。後片付けしたタスクの run は一覧から外し「履歴（後片付け済み）」に出す（記録は消さない）。一度 PASS した指示書（完了済み）は開始のリストから外し、開始も断る
- [ ] Tailscale 経由での配信（ネットワークへの公開のため Owner の承認待ち）
- [ ] React / React Flow の対話型画面（npm の依存の追加のため Owner の承認待ち）
- [x] ACC の自動更新（`../docs/tasks.md` T-704、2026-10-15 Owner 事前承認（AGENTS.md §2.1 適用））: `discover.py --acc` で Discovery の結果を書いた後・コミットの前に `acc.build` を実行。ACC の失敗ではコミットを止めず、終了コードを 0 以外にする（`--notify` で通知）。登録済みの plist に `--acc` を追加（本番への反映は main へのマージ後）

## API 連携（MAP Phase 5）

- [x] `insight_reporter_api.py`: Insight Reporter の `/api/v1` のクライアント（接続先は Registry、`ApiUnavailable`・`ApiError` の区別、任意のトークン）
- [x] `ingest_insight_reporter.load_latest_report()`: API → ファイルの順に取得（API が止まっていても従来どおり動く）
- [ ] Jarvis 自身の API（`/health`・`/api/v1/`）: Web フレームワーク（FastAPI）の追加が必要なため、Owner の承認待ち

## Knowledge Engine（MAP Phase 7）

- [x] `knowledge_engine.py`・`knowledge_cli.py`: プロジェクト横断の検索（BM25、プロジェクトで絞り込み）、出典の追跡（Vault・Registry・会話まで）、重複と鮮度の監査、検索結果だけを根拠に答える（RAG、llm.complete）
- [x] T-1304 `knowledge_cli.py audit --json`（監査結果を JSON で出す。DB を読めないときは標準エラーに 1 行で終了コード 1。`../docs/testing.md` §10）
- [x] T-1401 集計のスキル `knowledge-stats`（会話の経路 C、ADR-0027）: `knowledge_engine.stats()`（jarvis.db の Knowledge をデータソース別・分野別に集計、Registry の Vault で取り込まれていないものは「未取得」）、`knowledge_cli.py stats [--json]`、`orchestrator.py` のハンドラー（件数・分野を発言から決める、LLM を呼ばない）、声では上位 3 つの名前と件数だけを読む。スキルの索引への登録は jarvis-skills の別の PR（`../docs/testing.md` §11）
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

- [x] `discovery/collect.collect_http`: `healthcheck.type` が `none` なら base_url も調べず Unknown（通知なし）。未配備の Project OS API の誤通知の解消（2026-10-16）
- [x] `discovery/collect.run`: コマンドがない（launchd の PATH）・時間切れでも例外にせず、失敗の終了コードを返す（T-401 の「収集の例外で全体を止めない」）
- [x] 定期ジョブそのものの失敗の通知: `discover.py --notify` は例外・0 以外の終了コードも通知、`improve.weekly()`（改善の監査 → ACC）も失敗を通知。launchd の plist の PATH に `/usr/local/bin`（docker）を追加

## 応答の速さ（FR-014、`../docs/tasks.md` T-1212、2026-10-14 Owner 承認）

- [x] LLM での振り分けを既定で無効（`JARVIS_ROUTE_WITH_LLM=1` で有効）。ルールに当たらない発言は普通の質問として答える
- [x] 回答の逐次表示: `llm.complete_stream`（claude -p の stream-json）、`knowledge_engine.answer(on_text=)`、`orchestrator.Context.stream`、`chat.py`
- [x] システムの状態は前回の照合結果（Registry の guardian-report.md）の要約。「最新」「今の」などのときだけその場で照合
- [x] テストで本物の claude -p を逐次出力で呼ばない安全策（conftest）

## 声での会話（FR-014、`../docs/tasks.md` T-1213〜T-1215、2026-10-14 Owner 承認）

- [x] T-1213 `voice/record.py`（Enter で開始、周囲の音から決めたしきい値で無音 0.8 秒で区切る、最長 20 秒、保存しない）、`voice/speak.py`（文ごとの読み上げ、URL・出典の番号・コード・表の区切りは読まない、スキルごとの話し言葉の要約）
- [x] 自然な会話（ADR-0026、2026-10-17 Owner 指示）: `knowledge_engine.answer` はナレッジになければ LLM の知識で答える（出典は使ったものだけ、直近 6 発言を渡す、声のときは 2〜3 文）。`voice_chat.py` は既定でハンズフリー（話し終えたら自動で聞く、雑音の決まり文句は無視、`--push-to-talk` で従来の形）
- [x] 読み上げの音声エンジンの選択（`../docs/decisions.md` ADR-0029、T-1408）: `voice/tts.py`（say・AivisSpeech・VOICEVOX、有料は未接続）、設定と試聴の画面 `python -m acc.voice_settings`、割り込みの再生に差し込む、長い文を読点で区切る
- [x] 音声の候補の比較（2026-10-18 Owner 指示）: 試聴の画面に「候補の比較」（AivisSpeech 4・VOICEVOX 3・macOS の拡張の声 2・Kyoko）。2026-10-18 Owner「もっと JARVIS っぽい声」: AivisHub の hinakoyuhara・TANAKA・ろてじん（ACML）を追加し、執事風の調整（話速 0.92・抑揚 0.8・高さ -0.03）の候補を加えた（AivisSpeech 9 件。話速・抑揚の刻みを 0.01 に）。入っていない候補は理由を示す。AivisSpeech Engine 1.2.0・VOICEVOX ENGINE 0.25.2 を `~/Applications` に導入（自動起動なし）
- [x] 音声エンジンを会話の間だけ起動・停止（2026-10-18 Owner 指示）: `voice/engine_process.py`。止まっていれば起動して待ち、終わりにこの会話が起動したものだけを止める（プロセスのグループごと）。起動済みのものは止めない。異常終了した会話が残したものは引き継ぐ。複数の会話は一覧で共有し最後の会話が止める。SIGTERM・SIGHUP でも止める（起動・停止の途中は終わってから）。会話の途中で止まったら起動し直して選んだ音声に戻す。起動後に話者を先に読み込む（最初の文 0.77 → 0.27 秒）。声の会話と評価の画面から使う
- [x] 声の会話の割り込み（Barge-in、`../docs/decisions.md` ADR-0028、T-1407）: `voice/duplex.py`（自前の再生と参照の音量、回り込みの校正と予測、発言の番号、取り消し）。`voice_chat.py` は既定で割り込みあり（`--no-barge-in`・`JARVIS_VOICE_BARGE_IN=0` で無効）。聞いた文だけを履歴に
- [x] 起動用ファイル `run_text_chat.command`（chat.py）・`run_voice_chat.command`（voice_chat.py）。Finder でダブルクリックで起動（2026-10-17 Owner 依頼）
- [x] T-1214 `voice_chat.py`（orchestrator・逐次表示・読み上げ・キーボードでの確認・記録・ナレッジ化の材料）、`speech_to_text.transcribe_array`（認識のヒントの語）、faster-whisper の small モデルのダウンロード
- [ ] T-1215 受け入れ（Owner の声で）
- [x] T-1404 録音の改善（ADR-0027、`../docs/testing.md` §11）: `voice/record.py` に話し始めの直前 0.3 秒のプリロール、しきい値を周囲の音の中央値 × 2.0（`JARVIS_VOICE_THRESHOLD_FACTOR`、下限 300）、2 ブロック続いたら話し始め、途中の間はしきい値の 0.7 倍未満だけを無音と数える、無音の長さ `JARVIS_VOICE_SILENCE_SECONDS`（既定 0.8 秒）、0.3 秒未満の発話は捨てる。`speech_to_text.transcribe_array` はヒントの語を `hotwords` で渡す（語を 5 つ追加）
- [x] T-1402 LLM の使い分けの土台（ADR-0027、`../docs/testing.md` §11）: `llm.py` に `ollama`（`JARVIS_OLLAMA_URL`・`JARVIS_CHAT_MODEL`・`JARVIS_OLLAMA_TIMEOUT`、`/api/chat` を `think: false`・`keep_alive: 30m` で、逐次・取り消し、`warm_up_ollama()`・`ollama_available()`）と `claude_warm`（`--input-format stream-json` の claude -p を 1 つ前もって起動し、1 回の答えに 1 プロセス、`warm_up_claude()`・atexit で停止）。失敗は `LLMError` で別のプロバイダーに切り替えない。既定と呼び出し側は変えない（振り分けは T-1403）。実機での速さの計測は PR の後
- [x] T-1403 振り分け A/B（ADR-0027、`../docs/testing.md` §11）: `orchestrator.py` に `general-chat`（検索せず Ollama、`LLMError` なら `claude_warm`、使ったプロバイダーを `ctx.last_provider`）と `is_knowledge_question`（ナレッジの語・プロジェクトの語・検索の点数 `JARVIS_KNOWLEDGE_SCORE_THRESHOLD` 既定 8.0）。`knowledge-answer` は `claude_warm`（失敗なら `claude_cli` で一度だけ）。答えの一部を送った後の失敗では切り替えない。プロジェクトの語は Registry の全 project_id（「jarvis」を除く）。索引に `general-chat` がなければ従来どおり。jarvis-skills への登録・実機の計測は PR の後
- [x] T-1403 の改善（2026-10-18 Owner 指示）: 一般知識の質問の形（「って何」「とは」「仕組み」「使い方」「違い」など）は検索の点数が高くても `general-chat`。ナレッジ・プロジェクトの語は優先して `knowledge-answer`。最近の出来事を聞く語（`RECENT_WORDS`）は、その語を除いた話題で検索し、しきい値を 4 分の 1 にする。本番 DB の複製の 32 発話（Claude Code が作った例）で誤り 1（「この文章をまとめて」は検索の点数でナレッジに回る、既知の限界）。テストが本物の claude を起動していたため、`tests/conftest.py` で CLAUDE_BIN と Ollama の接続先を無効にした
- [x] T-1405 遅延の記録と起動時の読み込み（ADR-0027、`../docs/testing.md` §11）: `latency.py`（発言ごとに段階 segment・stt・route・search・llm_ready・first_text・first_audio・done（T-1502 で first_text と first_audio の間に audio_ready、mode に web を追加）の経過ミリ秒と、決まった属性 mode・skill・provider・warm・fallback・ok を `JARVIS_LATENCY_LOG`（既定 `data/latency/latency.jsonl`、5 MB で 1 世代に回す）に 1 行の JSON。本文・音声は記録しない。集計 `python -m latency report`、p50・p95 は最近傍順位法）。`chat.py`・`voice_chat.py`（3 つの形、割り込まれた発言は ok=False）・`orchestrator`・`knowledge_engine`・`llm` から記録。`orchestrator.warm_up()` を会話の起動時に裏のスレッドで呼ぶ（Ollama のモデル・待機させる claude）。実機の計測は PR の後
- [x] T-1406 実マイクの評価の画面（ADR-0027、`../docs/testing.md` §11）: `python -m acc.voice_eval [--port 8773]`（`acc/voice_settings.py` と同じ守り）。決まった 10 の文を 1 件ずつ録って文字にし、CER（NFKC・空白と句読点・記号を除く編集距離 ÷ 正解の文字数）と段階 segment・stt を返す。`mode="full"` は振り分け・読み取りのスキルだけ実行・最初の 1 文の読み上げで first_audio まで測る（書き込み・確認のいるスキルは実行しない）。結果を `data/voice/eval/<日時>.json`（音声なし）に保存し、CER の平均と first_audio の p50・p95（目標 2 秒・5 秒）の達否を示す。Owner の実マイクでの計測は PR の後

## ブラウザでの声の会話（`../docs/spec.md` §3.4.9、`../docs/browser-voice-spec.md`、ADR-0031、`../docs/tasks.md` §16）

- [x] T-1501 土台（`../docs/testing.md` §13）: `python -m voice.web [--port 8771]`（`voice/web/`。Mac の音声の機器を使わない）。トークンと Cookie・Host・Origin・`X-Jarvis-Voice` の確認、同時に 1 つの会話（2 つ目は 409）、ブラウザの音量による発話の自動検出（`vad.js`）、WAV の POST（上限 4 MB・20.5 秒、レートの一致）→ 16 kHz → 認識 → `orchestrator.handle` → 認識した文と回答の断片を SSE で表示。確認の要るスキルは実行しない。テストは偽の認識・偽の handle と Node の `node --test`。Windows 実機での確認は T-1505
- [x] T-1502 文ごとの合成と再生、聞いた文だけを履歴に（`../docs/testing.md` §13）: `voice/web/player.py` の `BrowserPlayer`（`voice.duplex.Player` と同じ形、WAV をメモリの中で ID を付けて保持、64 文・20 MB まで、知らせがなければ「長さ＋5 秒」で待つのをやめる）を `DuplexSpeaker` に渡し、`GET /api/audio/<id>`・`POST /api/played` を追加。ページは index の順に Web Audio で続けて再生し、再生中は発話の検出を止める（割り込みは T-1503）。読み方は `voice_chat.speak_rest`（`voice_chat.py` と共通に整理）、履歴は聞いた文だけ、読み終えたら `turn_done`。エンジンは会話の開始で起動し始め、最初の合成の前にだけ待ち、会話の終わりで止める。`notice` で音声の切り替えを知らせる。`latency` に mode `web` と段階 `audio_ready`。テストは偽の合成・偽のブラウザ・偽の launcher・偽の時計。Windows 実機での確認は T-1505
- [x] T-1503 割り込み・再生中のしきい値・異常の表示・切断（`../docs/testing.md` §13、BV-06・BV-07・BV-10・BV-11）: 会話の開始時に `POST /api/calibrate` のあいさつ（`audio` の `calibration: true`、1 回だけ、履歴に入れない）で回り込みの p95 を測り、再生中のしきい値を max(通常, 回り込み × `barge_margin`（既定 5.0、`JARVIS_VOICE_BARGE_MARGIN`））にする（`vad.js`）。会話の間は検出を止めず、考えている間・話している間の話し始めでページが再生を止めて `POST /api/interrupt`（今の番号だけ、違えば 409）。サーバーは番号を進めて古い合成・保持した音を捨て、生成を取り消し（`ctx.cancel`）、取り消せない処理は文脈を分け、履歴は Owner の発言＋聞いた文＋割り込みの印（`voice_chat.run_barge_in` と同じ）。処理中の `/api/utterance` も割り込み（409 にしない）。割り込まれた発言は `turn_done`（interrupted）だけ。ページは古い番号・`seq` の古いイベントを捨て（番号の分からないまま割り込んだときは、分かるまで新しい番号のイベントを保持して失わない）、割り込みの時間は考えている間も注記に出し、異常を種類ごとに表示（`page.js`）、切断（SSE の切断・想定外の POST の失敗（古い番号の 409 などは想定内で続ける）・15 秒の無通信、ping は 5 秒ごとの `event: ping`）で会話を終えてマイクを解放（自動の再接続・再送はしない）。合成できなければ `error` tts。テストは偽の合成・偽のブラウザ・偽の handle・Node。Windows 実機での確認（AT-03・AT-04・AT-05・AT-07）は T-1505
- [x] T-1504 画面での承認（`../docs/testing.md` §13、BV-09・AT-06。PR は Owner がマージ）: `orchestrator.execute` の確認を、発言ごとの承認の要求（使い捨ての ID `secrets.token_urlsafe(16)`・発言の番号・期限 60 秒）にし、イベント `approval` で画面に出して「画面で確認してください」と読み上げる。`POST /api/approve`（`{"turn", "id", "decision": run|decline}`、一致・期限内なら 200、違う・期限切れ・使用済み・会話が違えば 409、形が違えば 400）の「実行する」が、その ID・番号・期限内・会話が続いている・割り込まれていない、のすべてを満たしたときだけ実行する。結果は `approval_done`（run・declined・expired・cancelled）。「実行する」の後もスキルの起動の直前に会話・発言の有効性を確かめ（`Context.before_run`）、その間の割り込み・終わり・切断では実行しない。割り込み・会話の終わり・切断で取り消し、声の「はい」は新しい発言（割り込み）として扱い承認にしない。端末の入力を待つスキル（`knowledge-capture`・`theme-register`）は、ブラウザでは確認を求めずに実行しない（`orchestrator.Context.terminal=False`、`needs_terminal`。`chat.py`・`voice_chat.py` は変わらない）。ページは今の番号の要求だけを出し、ボタン・残りの秒数・結果を出す。テストは副作用のない偽のスキルで `orchestrator.handle`・`execute` の確認の経路を通し、期限は差し替えた時計（`tests/test_voice_web_approval.py`）。Windows 実機での確認は T-1505
- [x] 声の会話の応答の速さ（2026-11-03 Owner 指示、T-1505 の実測から）: (A) 声の会話のプロセスの claude -p を Sonnet（`JARVIS_VOICE_CLAUDE_MODEL`）、(B) 発言の最初の文を 24 字を超えたら読点で区切って先に読む
- [ ] T-1505 Windows 実機での受け入れ（AT-01〜AT-11）
- [x] T-1506 tailnet 内のブラウザで声の会話を開く（2026-11-03 Owner 指示、Owner 事前承認）: `--allow-host`・`JARVIS_VOICE_ALLOWED_HOSTS` の DNS 名を Host と HTTPS Origin に追加し、`--tailnet-port` の URL を表示する。`python -m voice.web --port 8772 --allow-host ohidemac-mini.taild8cb51.ts.net`、`tailscale serve --bg --https=8446 http://127.0.0.1:8772`。ACL に tcp:8446 の許可が必要。インターネットには公開しない。静的ページの相対 URL を確認し、HTTP・CLI のテストと全体テストを実行する。Tailscale と ACL の設定は別作業。
- [x] 雑談をナレッジの回答に回さない（2026-11-04 Owner 指示「雑談はナレッジ不要」）: ナレッジ・プロジェクトの語がなく検索の点数だけで決める文は、質問・依頼の形（`orchestrator.QUESTION_FORM`）のときだけナレッジの質問にする。本番の索引での試算: 評価セット 125 問の振り分けは変化なし、過去の会話 85 件のうち雑談 18 件が通常の会話に
- [x] 音声認識を高精度に（2026-11-04 Owner 指示）: faster-whisper のモデルを small → large-v3-turbo（`JARVIS_WHISPER_MODEL`、8 スレッド `JARVIS_WHISPER_THREADS`）。say の 8 文で一致 4/8 → 7/8、認識 1.0 → 2.7 秒
- [x] 音声認識を GPU で（2026-11-05 Owner 承認、ADR-0032）: Apple Silicon では mlx-whisper（large-v3-turbo）、声の区間だけを Silero VAD で渡す。認識 2.7 → 0.5 秒、一致 7/8 は同じ。使えなければ faster-whisper
- [x] ブラウザの会話の管理画面（2026-11-05 Owner 指摘「OK を押しても切り替わらない」）: Mac mini で `open` せず、tailnet の URL をリンクで返す
- [x] 承認して実行した後に割り込まれた発言の結果を、画面にだけ出す（`run_result`。2026-11-05 Owner の実機「管理画面のリンクが出ない」）

## ナレッジ回答の評価部門（`../docs/spec.md` §3.4.10、ADR-0030、`../docs/tasks.md` §15）

- [x] T-1410 評価セット（疑似 Q&A）の作成と検証: `evaluation/`（`qa.py`・`isolate.py`・`python -m evaluation build`）、`knowledge_engine.source_by_knowledge()`（`stats()` と共有）。本番 DB のコピーでの作成は T-1413
- [x] T-1411 評価の実行・指標・レポート: `tuning.py`（調整できる値。本番のコードが読み、既定は今の動き）、`evaluation/runner.py`（振り分け・順位・recall・MRR・要点の一致・「ない」・段階ごとの時間・落ちた段階、総合点 score / score_retrieval）、`evaluation/report.py`（JSON・Markdown）、`python -m evaluation run`。本番 DB のコピーでの計測は T-1413
- [x] T-1412 改善のループ: `evaluation/improve.py`（決めた設定の空間 `SPACE`、dev の失敗の傾向から 1 つの軸だけを変えた候補を最大 4 個、dev で `MIN_GAIN` 0.01 以上の最良を val で確かめ `VAL_TOLERANCE` 0.01 を超えて下がれば不採用、既定 3 回・上限 5 回・採用のない回で停止、test はループの後に 1 度だけ）、`evaluation/synonyms.json`（同義語の辞書）、`python -m evaluation improve`。結果は `data/eval/tuned/<日時>.json` と `data/eval/reports/<日時>-improve.md`（本番の設定は変えない）。本番 DB のコピーでの実行と反映の PR は T-1413
- [x] T-1414 評価の精度の修正（ADR-0030 の改訂、2026-11-02 Owner 承認、`../docs/testing.md` §12）: 要点の採点を 文字の一致 → 2 文字の組の重なり（`FACT_BIGRAM_RATIO` 0.7・数字の並びは一致が必要）→ ローカル LLM の判定（`--judge` 既定／`--no-judge`、常に Ollama、Claude の上限に数えない）の 3 段に。問ごとに `facts_hit_strict`・`fact_match`、集計とレポートに `facts_hit_strict`。`llm.ollama_options()`（評価の間だけ temperature 0・seed 0）。`MIN_GAIN` 0.03。作問の規則の版 2（対象の名前を質問に書く・要点 30 字以内、`fact_too_long`・`no_subject` で捨てる、題名から数字・日時を除くと同じ知識が 2 件以上の系列を選ばない、メタ情報に `rules`・`excluded_series`）。本番 DB のコピーでの作り直しと再評価は別のタスク
- [x] T-1416 判定のプロンプトを厳しくする（提案 G、`../docs/spec.md` §3.4.10 の改訂 A の 3、2026-11-03 Owner 承認、`../docs/testing.md` §12）: `evaluation/runner.py` の `JUDGE_PROMPT` に、数値・固有名詞・用語が違う／一般的・あいまい／別のもの／迷ったら「含まない」の規則と、含む 2 つ・含まない 1 つの例を追加（質問・答え・要点の差し込みと出力の形は変えない）
- [x] T-1419 Project OS の長い文書を節ごとに取り込む（提案 F、`../docs/project-os-split-design.md`、2026-11-03 Owner 承認、`../docs/testing.md` §12）: `ingest_oarai_project_os.py` に `split_sections()`（`SECTION_MAX_CHARS` 1,000、Markdown は `##`→`###`→段落→文字数、JSON は配列の要素ごと）、節ごとの Knowledge（title・content・行の URL・meta `section_key`/`section_index`/`replaces`）、再取り込みの更新・追加・削除（`deleted`）、`--migrate-sections`（バックアップ付き）・`--rollback-sections`。`knowledge_engine.stats` は節をファイルごとに 1 件、`evaluation/runner.py` の順位は `meta.replaces` でも照合。本番 DB の移行と評価の前後の報告はマージの後
- [x] 不具合の修正（2026-11-03）: 起動時の warm_up（裏のスレッド）が `warnings.catch_warnings` で索引の警告を抑えていたため、会話の側で同時に出た警告を横取りすることがあった（`tests/test_chat_ckc.py` がまれに DID NOT WARN）。`skills.load(quiet=True)`・`skills.active(quiet=True)`（警告を出さず結果を残さない）を足し、warm_up はそれを使う。回帰テスト 2 件

## Project OS API の再開（`../docs/tasks.md` T-1216・`../docs/project-os-api-restart-plan.md` 案 A、2026-10-16 Owner 承認）

- [x] 送信先を `JARVIS_OARAI_API_BASE`（既定 `http://127.0.0.1:8010`）に。`submit_or_hold()` が API の応答の status で "submitted" を上書きしていた不具合を修正（API の状態は `api_status`）。保留・再送・送信のテスト
- [ ] Mac mini のローカルの API の常駐（Project OS 側の PR と launchd の登録の後に受け入れ）
