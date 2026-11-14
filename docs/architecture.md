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
| L5 Presentation | 入出力 | `chat.py` / `ask.py`（CLI）、`talk.py` / `live_talk.py` / `speech_to_text.py` / `text_to_speech.py`（音声）、`generate_report.py`（Markdownレポート） | Web Summary / ACC（本リポジトリの内部モジュール、ADR-0010。`acc/build.py` の静的なページと、開発オーケストレーターの操作画面 `acc/dev_console.py`（127.0.0.1 だけ、起動ごとのトークン、`../docs/dev-orchestrator-design.md` §16））、HTTP API（MAP Phase 5〜6） |

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
- `config.py`: `JARVIS_DB_PATH`・`JARVIS_VAULT_ROOT`・`JARVIS_CKC_DATA_DIR`・`JARVIS_LLM_PROVIDER`・`JARVIS_REGISTRY_JSON`・`JARVIS_HOST_ID` の環境変数での差し替え、Registry を読めないときの予備（`FALLBACK_VAULTS`・`FALLBACK_PROJECTS`）
- `registry.py`（新規、T-310）: Architecture Registry の `generated/registry.json` から Vault のパスとプロジェクトを読む（`../docs/architecture.md` §6.9）
- `schema.sql`: CKC 用のテーブル（`ckc_conversations`・`ckc_candidates`）を `CREATE TABLE IF NOT EXISTS` で追加。既存の `nodes`・`edges` は変えない
- テスト: `tests/`（pytest、開発用依存 `requirements-dev.txt`）

## 3.10 モジュール構成（スキルと会話からの実行、FR-014 Phase 1）

設計の詳細は `../docs/architecture.md` §19（流れ、jarvis-skills の形式、Phase 1 のスキル 11 件、振り分け、権限、文脈）。

- `skills.py`（新規）: jarvis-skills の `generated/skills.json` を読む（Registry の `repo-jarvis-skills` の locator、または環境変数 `JARVIS_SKILLS_JSON`）。読めなければ予備の索引（FR-014 より前から chat.py にあった `knowledge-answer`・`knowledge-capture`・`theme-register`・`code-task`）で動き、警告を出す（後方互換）
- `orchestrator.py`（新規）: 振り分け（ルール → LLM）、入力の検証、権限の確認、実行、結果の整形、対話の文脈。スキル名 → 本リポジトリの関数の対応表を持ち、索引にあって `active` のものだけを呼ぶ
  - 2026-11-11（T-2002、ADR-0035）: 会話の LLM（ハーネス）の順番 `harness_order`・`run_with_harnesses`（休止・切り替えの知らせ・全部だめのときの `AllHarnessesFailed`）、切り替えの発言 `harness_command`。`Context.harness`・`resting`・`llm_info`。`llm.py` に `codex` のプロバイダー・`LLMUnavailable`・`failure_reason`・`_read_json_lines`（claude と codex の JSON の行の読み取りの共通化）
- `chat_actions.py`（新規、T-1207）: `chat.py` から「ナレッジ化」「テーマ登録」の処理を分けたもの（`chat.py` は読み込み時に標準入出力の設定を変えるため）。`chat.py` と `orchestrator.py` が使う
- `chat.py`（改修）: 発言を `orchestrator` に渡す。「ナレッジ化して」「テーマ登録:」の動作は変えない
- `run_task.py`（改修）: 実行の記録（Task → uses → Skill、成否・所要時間・振り分けの方法）。既存の `run_task()` は残す
- `config.py`: `JARVIS_SKILLS_JSON` の差し替え
- 声での会話（`../docs/architecture.md` §19.6）: `voice_chat.py`、`voice/record.py`、`voice/speak.py`、`speech_to_text.transcribe_array`。読み上げのエンジンは `voice/tts.py`（say・AivisSpeech・VOICEVOX、ADR-0029。設定と試聴は `acc/voice_settings.py`）。既定は macOS の `say`（声 `JARVIS_VOICE`、既定 Kyoko。出力先 `JARVIS_VOICE_OUTPUT`、既定は入力と同じ機器＝ヘッドセット）。認識のヒントの語は `JARVIS_VOICE_VOCAB`
- T-1506（Owner 事前承認）: `voice/web/server.py` は `make_server(allowed_hosts=())` と `JARVIS_VOICE_ALLOWED_HOSTS` の DNS 名を起動時に検証し、Host と POST Origin（追加名は HTTPS のみ）を照合する。`__main__.py` は `--allow-host`・`--tailnet-port` を受け、tailnet 用 URL を表示する。待ち受けは 127.0.0.1 のまま。`python -m voice.web --port 8772 --allow-host ohidemac-mini.taild8cb51.ts.net` と `tailscale serve --bg --https=8446 http://127.0.0.1:8772` で tailnet 内から開く。ACL の tcp:8446 許可が必要で、インターネットには公開しない。
- ブラウザでの声の会話（ADR-0031、T-1501〜T-1504）: `voice/web/`。`__main__.py`（起動。Whisper の読み込みと `orchestrator.start_warm_up()`、sounddevice を import しない）、`server.py`（標準ライブラリの `ThreadingHTTPServer`、127.0.0.1 だけ。`make_server(port, session_factory, token)`。ブラウザ → サーバーは POST、サーバー → ブラウザは SSE の 1 本。`GET /api/config`・`POST /api/session`・`GET /api/events`・`POST /api/utterance`（PCM 16 bit・モノラルの WAV、4 MB・20.5 秒まで）・`POST /api/end`・`GET /api/audio/<id>`（保持している文の WAV、今の会話の ID だけ）・`POST /api/played`（`{"turn", "id", "event": start|end|error}`、別の番号は 409、保持していない ID は 404）・`POST /api/calibrate`（回り込みの測定のあいさつ、`{"audio": bool}`、2 回目は 409）・`POST /api/interrupt`（`{"turn"}`、今の番号でなければ 409）・`POST /api/approve`（`{"turn", "id", "decision": run|decline}`、T-1504。一致して期限内なら 200、違う・期限切れ・使用済み・会話が違えば 409、形が違えば 400）。`/api/config` に `barge_margin`（`JARVIS_VOICE_BARGE_MARGIN`、既定 5.0）。イベントは `session`・`seq`・`type`（ready・recognized・ignored・answer_chunk・answer_done・audio（あいさつは `calibration: true`）・notice・error（kind に stt・answer・playback・tts・bad_audio・internal）・turn_done（割り込まれた発言は `interrupted: true`）・approval・approval_done（T-1504）・ended）と `turn`。SSE は 5 秒ごとに `event: ping`（名前付き。`seq` に入らない）、切れたら会話を終える（再接続しない））、`session.py`（`WebSession(transcribe, handle, emit, record, synth, launcher, clock)`。WAV → float32 → `voice.tts.resample` で 16 kHz → 認識 → 別のスレッドで `orchestrator.handle(text, ctx, ask=画面での承認)`（`ctx` は `Context(voice=True, terminal=False)`）、`ctx.stream` を `answer_chunk` と `DuplexSpeaker.feed` に、終わりに `voice_chat.speak_rest`（`voice_chat.py` と共通の読み方）。ブラウザが読み終える（`speaker.speaking()` が False）まで発言の処理を続け、履歴は `speaker.heard(turn)` の文だけ、その後 `turn_done`。割り込み（T-1503）: 発言ごとの状態 `_TurnState`（番号・遅延の記録・認識した文・結果）を持ち、`interrupt(turn)` と、処理中の `submit` が `_interrupt_locked` を呼ぶ（会話のロックの中で、番号を割り込まれた集合 `_cut` に入れ → `speaker.new_turn()` → 履歴に Owner の発言＋聞いた文＋`voice_chat.INTERRUPTED_MARK` → 答えを作っている途中なら `orchestrator.Context(voice=True, history=...)` で文脈を分ける → `turn_done` interrupted）。`_cut` の番号のイベントは `_send_locked` が送らない。`ctx.cancel` は `voice_chat.run_barge_in` と同じ「番号が違えば True」、`ctx.stream` は番号が違えば `voice_chat.Cancelled`。発言のスレッドは終わりに同じロックの中で `_cut` を確かめ、割り込まれていれば何もしない（履歴を二重に入れない）。`calibrate()` はあいさつを番号 0 で `speaker.say` し、`BrowserPlayer` の番号も 0 にして再生の知らせを受ける。`DuplexSpeaker.synthesizing()`（合成の待ち・合成中）で合成の終わりを待つ。1 文も合成できなければ `_FirstSynth` が `error` tts を発言ごとに 1 回送る。合成は `_FirstSynth` が最初の 1 文の前にだけ `voice_chat.wait_engine` で待ってから `tts.make_synth(notify=notice, recover=voice_chat._recover_for(launcher))` を作る。エンジンは `voice_chat.start_engine` で会話の開始に起動し始め、`end()` で `launcher.stop()`（本番の launcher は `server.default_session_factory` が `engine_process.for_settings` で作る）。遅延は `latency.start("web")` を回答のスレッドで、audio_ready は再生の部品のイベント、first_audio は最初の `start` の知らせで記録。雑音の判定と終わりの言葉は `voice_chat` のものを使う。画面での承認（T-1504）: `ask` は `_ask(turn, prompt)` で、会話のロックの中で承認の要求 `_Approval`（`secrets.token_urlsafe(16)` の ID・番号・期限＝`clock()`＋60 秒、同時に 1 つ）を作って `approval` を送り、`speaker.say("書き込みがあります。画面で確認してください。", turn=turn)`。答え（`approve(turn, id, decision)` が `hmac.compare_digest` で ID・番号・期限・未回答を確かめて `answer` を入れる）・期限・取り消しのどれかまで短い間隔で待ち、ロックの中で `result` を 1 回だけ確定して `approval_done` を送る。`"y"` は result が run のときだけ。run のときは要求を残したまま `"y"` を返し、`orchestrator.execute` がスキルを起動する直前に `Context.before_run`（`_before_run(turn)`）を呼ぶ。呼ぶのはスキルを動かすスレッドの中で、handler を呼ぶ直前（`orchestrator._gated`。確認と起動の間に別スレッドへの投入を挟まない）。会話のロックの中で、要求が取り消されておらず会話が続いていて割り込まれていなければ要求を使い切って `approval_done` run を送り True、そうでなければ False（`execute` は `error="cancelled"` で実行しない。`chat.py`・`voice_chat.py` は `before_run` を設定しない）。`_cancel_approval_locked` は run で確定して実行を始める前の要求も取り消す。割り込み（`_interrupt_locked`）と `end()` は `_cancel_approval_locked` で待っている要求を cancelled にし、`approval_done` を `turn_done`・`ended` より先に送る（声の「はい」は新しい発言として割り込みになり、要求を取り消す）。端末の入力を待つスキルは `orchestrator.execute` が `Context.terminal=False` を見て確認を求めずに `needs_terminal` を返す）、`player.py`（`BrowserPlayer`: `voice.duplex.Player` と同じ形の `enqueue`・`set_turn`・`stop`・`speaking`・`heard`・`close` と、ブラウザからの `audio(id)`・`played(turn, id, event)`。sounddevice を使わない。今の番号の音だけを WAV にして `secrets.token_urlsafe` の ID で保持（64 文・20 MB まで）、end で聞いた文にして消す。期限は先頭の文から順に「始められる時刻（前の文の終わり、start の知らせ）＋長さ＋5 秒」）、`static/`（`index.html`・`app.js`（`audio` を受けたら取り（`AbortController` で止められる）、`decodeAudioData` して index の順に Web Audio で再生、`/api/played` を送る。会話の開始であいさつを再生して回り込みを測り、検出は止めずに再生中だけしきい値を上げる。考えている間・話している間の話し始めで再生を止めて `/api/interrupt`。`/api/utterance` の応答の前に割り込んだら、番号が分かるまで新しい番号のイベントを保持し（`createCutHold`）、分かった時点で割り込みを送って保持したイベントを渡し直す。異常の表示・切断（15 秒の無通信・SSE の `onerror`・想定外の POST の失敗。応答は `postOutcome` で想定内（古い番号の 409 など）と分ける）で会話を終えマイクを解放）・`vad.js`（発話の検出の純粋な関数。`percentile`・`playThresholdFrom`・`createDetector` の `setPlaying`・`setPlayThreshold`・start の `onsetFrames`）・`page.js`（ページの判定の純粋な関数: 異常の種類ごとの文言 `problem`・`micErrorKind`・`seq` の確認 `createSeqFilter`・番号の判定 `turnDecision`・再生の失敗の数え方・割り込みの時間 `bargeTiming`・承認の要求を出すか `approvalShown`・残りの秒数 `secondsLeft`・結果の文言 `approvalResultText`）。`app.js` は `approval` で確認の文・「実行する」「実行しない」・残りの秒数を出し、押したら `/api/approve` を 1 回だけ送ってボタンを無効にし、`approval_done`・割り込み・終了・切断・次の番号でボタンを消す（T-1504）。割り込み・次の番号で消した要求は `withdrawn` に覚え、その `approval_done`（cancelled など）は番号の判定の前に受けて結果を出す（`page.js` の `approvalMatches`）。`vad.js`・`page.js` は `tests/js/` で Node のテスト・`vad-worklet.js`（10 ms ごとの RMS とフレームの時刻）。外部の CDN を読まない）
- 受け入れ（T-1210）で見つかった点の修正: LLM で選ぶのは確認の要らない読み取りのスキルだけ（書き込み・課金のあるスキルは決まった言い回しのときだけ）。Owner の発言は、応答が失敗・時間切れでもナレッジ化の材料に残す
- 開発レビュー（`../docs/review-2026-10-14.md` §3.2）の反映（T-1211）: `confirm: true` のスキルと `code-task` は毎回確認、`orchestrator.FLOOR` に関数ごとの最低限の権限（索引と比べて厳しい方を使う）、振り分けの LLM は 60 秒・確認を待たないスキルは 120 秒で打ち切り（`llm.complete` に任意の `timeout`、`code_agent` の OpenAI に 120 秒）、実行の記録は `ckc/secrets.mask_secrets` で伏せ字

## 4. 実行基盤

- 開発・実行の中心: Mac mini M4 Pro（48GB）。Windows PC は Tailscale / SSH 経由の操作端末（JARVIS-MAP-002 §3）
- これまでの実装・動作確認は Windows 上で実施。Phase 1・2 は Mac mini で動作確認済み（2026-10-11、`run_daily.py`・`ask.py`・Google API 接続）。2026-10-11 から Mac mini の `jarvis.db` を正本とし、定期実行も Mac mini（launchd）に移した。Windows の定期実行は停止済み
- macOS（python.org 版 Python）では「Install Certificates.command」で CA ファイルを用意しておく（Mac mini は実行済み）。feedparser は証明書エラーを例外にせず空の結果を返すため、未実行だと RSS 取得が 0 件になる
- 今後: Docker 化（独立した `Dockerfile` / `compose.yaml`、永続データは Volume）と HTTP API 化（`/health`、`/api/v1/`）を全体計画に沿って行う（MAP Phase 4〜5）
- LLM: 機密性の高い処理はローカルLLM、高精度が必要な処理はクラウドAPI（Claude）のハイブリッド
- 音声（Phase 3）: 認識はfaster-whisper等、合成はVOICEVOX等をローカル実行。Apple Silicon の Mac では認識を mlx-whisper（GPU）で動かす（ADR-0032、使えなければ faster-whisper）

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

### 会話の記録・要約・想起（T-2101・T-2102、2026-11-12）

全体 `docs/architecture.md` §10.9 に従う。`memory/store.py` が JSONL 正本と SQLite 索引を管理し、要約失敗回数と除外状態も `status` レコードへ追記して復元する。JSONL 追記後に索引の更新が失敗した場合、次の発言の記録前にその会話の索引を原文から復元する。`memory/summarize.py` は Ollama だけを使い、起動時のバックグラウンド処理で要約する。T-2102 では `memory/recall.py` が SQLite 索引と承認済みの Preference を読み、BM25 で関係する要約を選ぶ。原文 JSONL・外部通信・LLM は想起に使わない。経路 A・B の回答直前にだけ記憶パックを注入し、例外時は空として続行する。`knowledge_engine.answer(memory=None)` は既存の評価呼び出しとの互換性を保つ。

### 記憶評価（T-2103、2026-11-12）

`evaluation/memory/` に固定 JSON・CLI・runner を置く。シナリオとモードごとの TemporaryDirectory 内で DB・JSONL・Vault・CKC・遅延ログを隔離し、設定差し替えは終了時に復元する。既存の Ollama 限定ガードを再利用し、承認と外部操作は拒否する。採点に LLM は使わず、発言単位の期待をすべて満たしたシナリオを合格として集計する。想起関数を計時して回答生成時間と分ける。実環境の測定・改善は本実装の対象外（Owner 依頼）。


### 会話のハーネスのセッションと道具（T-2401〜T-2409、2026-11-14）

全体 `docs/architecture.md` §19.9 に従う。`harness_session.py` が会話 ID ごとのセッション（ハーネス・送った発言の数・送った記憶の項目・最後に使った時刻）を持ち、`orchestrator` の経路 A・B の `run(provider, stream)` から呼ばれる。決まりの文は `prompts.py`（claude は `--system-prompt`、codex は `developerInstructions`、1 回限りはプロンプトの頭）。`llm.ClaudeSession` は標準入力を閉じずに 1 行ずつ送り、`_read_json_lines(until=...)` で result まで読む。標準エラーはファイルに出す（会話の間に管が詰まらないように）。`llm.CodexThread` は常駐の app-server の中のスレッドで、`_app_turn` を重ねる。`jarvis_mcp.py` は標準ライブラリだけの標準入出力の JSON-RPC（initialize・tools/list・tools/call・ping）。起動の設定は `harness_session._mcp_config` が作り、DB・記憶フォルダなどの場所を環境変数で明示する。記憶のメモは `memory/store.py`（`add_note` など）と `schema.sql` の `memory_notes`。新しい依存はない。

## ナレッジ保存と接続状態（2026-11-13）

要件・設計の正本は統合ワークスペース docs/spec.md §3.4.13 と docs/architecture.md §22。既存 orchestrator のルール、knowledge_connections の読み取り診断、CKC schema の検証を使う。依存・DB スキーマは追加しない。

## ツールの初回承認（T-2501）

共通の窓口が構造化要求の検証、永続した道具ごとの許可、実行前の確認を担う。harness_session は Claude・Codex ともネイティブツールを渡さず窓口を経由する。Web のみ承認後の Claude 専用プロセス。正本: 統合ワークスペース docs/architecture.md §19.10、spec §3.4.12 J。
