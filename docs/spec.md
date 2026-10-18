# spec.md — JARVIS 仕様

何を作るか。機能追加・変更は、このファイルを先に更新してから実装する。

## 1. 目的

個人用AIアシスタント。情報を定期収集・要約し、ナレッジを蓄積して質問に答え、依頼タスクを自律実行し、スキルを蓄積する。

## 2. 事業方向性（6つ）

1. ピアノ教室運営補助（集客・SNS運用・アクセス分析）
2. 調査系（世界のニュース・AIニュース等の定期リサーチ・報告）
3. 仮想取引（株・FX）
4. 不動産・株情報の収集＆報告
5. 製造業の設計・開発支援調査とビジネス化の模索支援
6. 民泊立ち上げプロジェクト

## 3. 開発フェーズ（確定）

ここでの Phase 1〜4 は JARVIS 内部の開発段階。全体計画 JARVIS-MAP-002 の Phase 0〜10（「MAP Phase」）とは別物。

インターフェースは段階的に拡張する。下位層（取得・要約・メモリ）は変えず、入出力のみ差し替える。

### Phase 1: CLI／定期バッチ（現在のMVP）

- 対象: ②AIニュース日次収集・要約
- 入力: なし（定期実行）。出力: Markdownレポート
- 実行: `python run_daily.py`、定期実行はタスクスケジューラ（Windows）／launchd（Mac）
- 受け入れ条件:
  - RSSから新着記事を取得し、URL重複なくSourceノードとして保存できる
  - 各記事を日本語2〜3文で要約し、Knowledgeノードとして保存できる
  - Task → produced → Knowledge → derived_from → Source のグラフが作られる
  - 日次のMarkdownレポートが `reports/` に出力される
- 音声対応は含まない

### Phase 2: テキスト質問応答

- 蓄積したKnowledgeを検索して回答するCLI（`ask.py`）を追加
- 入力: 質問文（コマンドライン引数、省略時は対話入力）。出力: 標準出力への回答（出典URL付き）
- 例: `python ask.py "先週のAIニュースで重要だったのは？"`
- 検索仕様（SQLite、ベクトルDBは未導入）:
  - 質問文全体をそのままLIKE検索すると自然文はほぼ一致しないため、janome（形態素解析）で質問文から名詞を抽出し、それをキーワードとしてKnowledgeを検索する（タイトル・本文、いずれかのキーワードを含むものを取得し、一致キーワード数が多い順に並べる。「結果」「判断」等の一般的な単語1つだけの一致が、複数語が一致する本命より上位に来ないようにするため）
  - キーワード一致はKnowledgeの新旧を問わず全履歴から検索する（ニュースだけでなく民泊ノート等の長期参照用ナレッジも対象になるため）
  - キーワード一致が0件の場合のみ、直近N日（既定14日）の最新Knowledgeにフォールバックする（「最近の動向」のような曖昧な質問向け）
  - 件数は既定20件を上限とする
  - 検索結果（タイトル・要約・出典URL・作成日時）をコンテキストとしてClaude APIに渡し、日本語で回答を生成する。コンテキストに情報がない場合は「わからない」旨を明示する（ハルシネーション防止）
- モデル: コスト最適化方針（CLAUDE.md参照）に従い、既定は軽量レイヤー（Haiku）
- 受け入れ条件:
  - 質問に対し、該当Knowledgeの内容を踏まえた回答と出典URLが得られる
  - 該当するKnowledgeが見つからない場合、その旨を回答する（存在しない情報を創作しない）

### Phase 3: 音声対話

- 入力: 音声認識（faster-whisperでローカル推論）
- 出力: 音声合成（VOICEVOX ENGINE のローカルHTTP API、既定 `http://127.0.0.1:50021`）
  - 2026-10-18 から（`../docs/decisions.md` ADR-0029）: 声の会話（`voice_chat.py`）の読み上げは、音声エンジン（macOS の say〔既定〕・AivisSpeech・VOICEVOX。有料の ElevenLabs・OpenAI TTS は Owner の承認まで未接続）・話者・話速・抑揚を選べる。設定と試聴の画面は `python -m acc.voice_settings`（127.0.0.1 だけ）で、設定は `data/voice/settings.json`。HTTP エンジンの接続先は Mac の中だけ（転送は追わない）。選んだエンジンが使えないとき・途中で止まったときは say（Kyoko）で読む。長い文は読点の切れ目で先に読む。試聴の画面の「候補の比較」は、決めた候補（AivisSpeech・VOICEVOX の話者とスタイル、macOS の Kyoko・Otoya の拡張の声、Kyoko）を同じ文で試聴でき、「選ぶ」で設定の欄に入れる（保存は「この音声を使う」）。入っていない・起動していない候補は理由を示して使えないとする。有料の API は候補に含めない
- Phase 2の質問応答窓口（`ask.answer()`）の前後に入出力を付けるだけの構成にする（`talk.py` / `live_talk.py`）
- 動作検証はWindows開発機の実機マイク/スピーカーで行う（Mac miniへの移行はMVPがWindowsで十分に検証できた後）
- 2種類の入出力モードを用意する:
  - ファイルベース（`talk.py`）: 音声ファイル（wav）のパスを受け取り文字起こし、回答テキストをwavファイルとして保存。自動テスト・デバッグ用
  - 実機マイク/スピーカー（`live_talk.py`）: 固定時間（既定5秒）マイクから録音 → 文字起こし → 回答生成 → VOICEVOX ENGINEで音声合成 → 既定スピーカーで再生
- VOICEVOX ENGINEは別途ダウンロード・起動する前提（pipには含まれない）。未起動時はエラーメッセージで明示する
- 受け入れ条件:
  - 音声ファイルを渡すと文字起こしされ、Phase 2の検索・回答ロジックに渡せる
  - 回答テキストがVOICEVOX ENGINE経由で音声ファイルに合成される（ENGINE起動時）
  - Windows実機で、マイクに話しかけた質問に対しスピーカーから音声で回答が返る

### Phase 4以降

他カテゴリ（①③④⑤⑥）への横展開は後回しにする。Phase 4では以下3点を実装する。

#### 4-1. エージェント・APIルーティング（階層型エスカレーション）

- `llm.ask_claude()` を拡張し、軽量レイヤー（既定）→ 必要時のみ高精度レイヤーへ自動エスカレーションする関数 `ask_claude_tiered()` を追加する
- エスカレーション条件（いずれか）:
  - 軽量レイヤーの応答が空、または極端に短い（検証失敗）
  - 呼び出し側が `force_escalate=True` を指定（ユーザーの明示要求、または多段推論が必要と判断した場合）
- エスカレーションの履歴（時刻・from/toモデル・理由・プロンプト概要）を `jarvis_agent.log` に記録する（コスト追跡用、`.gitignore`で除外）
- 既存の `summarize.py` / `ask.py` は変更しない（定型タスクのため軽量レイヤー固定で十分。切替ロジックの対象は今後の複雑なタスクから）

#### 4-2. コード実行サブエージェント

- `code_agent.py`: コードタスク（生成・リファクタリング・デバッグ）の説明文を受け取り、OpenAI API（`gpt-4o-mini`等の低単価モデル）で結果を生成して返すCLI/関数
- `OPENAI_API_KEY` が未設定の場合は明確なエラーメッセージで停止する
- 呼び出し履歴も `jarvis_agent.log` に記録する

#### 4-3. 自律タスク実行・スキル蓄積の仕組み化

> 2026-10-14: スキル蓄積は 4-11（FR-014）で置き換える。以下は Phase 4 時点の記録。

- `run_task.py`: タスクの説明文を受け取り、簡易分類（コード関連キーワードを含むか）で `code_agent.py` またはKnowledge検索・回答（`ask.answer()`）に振り分けて実行するCLI
- 実行したタスクは Task ノードとして保存し、使用したエージェントを `executed_by` エッジ、実行方法を `Skill` ノード（`uses` エッジ）として記録する。同種のタスク（タイトルの部分一致）が既にSkillとして存在する場合は新規作成せず再利用する
- 受け入れ条件:
  - タスクの説明文を渡すと、コード関連かナレッジ関連かに応じて適切な処理が実行される
  - 実行後、Task → uses → Skill、Task → executed_by → Agent のグラフが作られる
  - 2回目以降、同種のタスクでは既存のSkillノードが再利用される（新規Skillノードが重複作成されない）

#### 4-4. 他カテゴリへの横展開（③④⑤⑥、①ピアノ教室は対象外）

①ピアノ教室運営補助はSNS運用・アクセス分析が中心で「RSS収集→要約→レポート」のパイプラインに合わないため対象外とする。③④⑤⑥は②AIニュースと同じ業界ニュース収集・要約のため、同一パイプラインをカテゴリ違いで横展開する。

- `config.py` の `RSS_FEEDS` に③仮想取引（trading）④不動産（realestate）⑤製造業（manufacturing）⑥民泊（minpaku）向けのフィードを追加する
- `fetch_news.py` / `summarize.py` はカテゴリに依存しない実装のため変更不要（Source/Knowledgeは既にcategory列を持つ）
- `generate_report.py` / `run_daily.py` を変更し、レポートはカテゴリごとに別ファイル（`reports/{category}_{日時}.md`）として出力する
- 受け入れ条件:
  - `run_daily.py` 実行後、news/trading/realestate/manufacturing/minpaku の5カテゴリそれぞれで新規Source・Knowledgeが作られる
  - `reports/` に5カテゴリ分の別ファイルが出力される

#### 4-5. 民泊: 個人Obsidianボルトの取り込み

民泊カテゴリは業界ニュースに加えて、ユーザー個人の「大洗民泊」計画ノート（GitHub上のObsidianボルト、`unikarei/oarai-obsidian`）を参照できるようにする。

- `ingest_obsidian.py`: GitHub API（`git/trees` + `raw.githubusercontent.com`）でボルト内の `民泊@大洗/` 配下のMarkdownノートを取得する。ローカルへのgit clone・ネストしたgitリポジトリは作らない
  - テンプレート（`99_テンプレート/大洗_調査ノート雛形.md`）と資料追加案内（`90_資料/資料の追加方法.md`）は実質的な内容がないため取り込み対象外とする
  - 各ノートをSourceノード（category=minpaku、url=GitHubのblob URL）として保存し、URL重複は排除する
  - ノートは既に人間が整理した参照用ナレッジであり、ニュースのような要約は行わない。全文をそのままKnowledgeノードのcontentとして保存する（derived_fromでSourceに接続）
  - 既存ノートの内容が変わっていた場合（ユーザーの編集・後述のJARVISからの書き込み）は、新規ノード追加ではなく既存Source/Knowledgeのcontentを更新する
- 検索仕様の変更: 上記の理由から、`search_knowledge()` のキーワード一致検索は直近N日に絞らず全履歴を対象にする（Phase 2の検索仕様を修正、詳細は該当セクション参照）
- 受け入れ条件:
  - `ingest_obsidian.py` 実行で、対象ノート分のSource・Knowledgeが作られる
  - 再実行してもURL重複により新規ノードが増えない
  - `ask.py` で大洗の物件・設備等について質問すると、該当ノートの内容と出典URL（GitHub上のノートへのリンク）付きで回答される

- 2026-10（T-202）: 取り込みは GitHub API ではなく Mac mini のローカルの Vault を読む（`../docs/decisions.md` ADR-0016）。対象に汎用 Vault `jarvis-knowledge`（`_jarvis/` を除く）を加えた。ノードの URL は従来の GitHub の blob URL と同じ形を保つ

#### 4-6. 民泊: Obsidianボルトへの書き込み（新規ノート作成・既存ノートへの追記）

JARVIS側からもボルトを更新できるようにする。自動実行はせず、手動トリガー（CLI実行）でのみ書き込む。

- `update_obsidian.py`:
  - `create_note(path, content)`: 新規ノートを作成する（同名ファイルが既に存在する場合はエラー）
  - `append_note(path, addition)`: 既存ノートの末尾に追記する。追記部分には日付と「JARVIS追記」である旨を明記する
  - 書き込み前に対象ノートの現在のsha（GitHub Contents API）を取得し、それを使って更新する。取得後に他端末等で編集され競合した場合はAPIが409を返すため、分かりやすいエラーメッセージで中断する（ボルトのREADMEにある「編集前に取得、編集後に保存」の運用方針に準拠し、上書き事故を防ぐ）
  - 認証は`ingest_obsidian.py`と同様に`gh auth token`を使う。トークンや認証情報は本リポジトリに保存しない
- `ingest_obsidian.py` を拡張し、既存ノートの内容が変わっていたらSource/Knowledgeのcontentを更新する（上記4-5に追記済み）。これにより、JARVISでの書き込み後に再取り込みすれば最新内容がask.pyの検索対象になる
- **ローカル Vault への改修（4-10 の前提、`../docs/decisions.md` ADR-0016）**: 書き込み先を GitHub Contents API から Mac mini のローカル Vault（`~/work/obsidian/<Vault>`）に変える。`create_note` / `append_note` の関数名・引数・CLI・追記の書式は変えない。sha 照合の代わりに、読み込み時と書き込み直前の内容のハッシュ、対象ファイルの未コミット変更、Git の操作中（merge・rebase・ロック）を確認し、一時ファイルから原子的に置き換える。Git の commit・push はしない（Obsidian Git に任せる）。書き込みの共通部分は `vault.py` に置き、CKC と共用する
- 受け入れ条件:
  - 新規ノートを作成できる（既存パスと衝突する場合はエラーになる）
  - 既存ノートに追記できる
  - 競合時（取得後に他で更新された場合）はエラーで中断し、上書きしない
  - 書き込み後に`ingest_obsidian.py`を再実行すると、ローカルのKnowledgeが最新内容に更新される

#### 4-7. ①ピアノ教室運営補助: アクセス分析〜改善提案の定期レポート化

対象サイト: https://ono-piano.com/（Search Consoleプロパティ: https://ono-piano.com/piano.class/、`SEARCH_CONSOLE_SITE_URL`で指定）。目的は単なるアクセス集計ではなく「問い合わせを増やすために次に何をすべきか」をJARVISが提案すること。他カテゴリと同じ「取得→Knowledge化→レポート」のパイプラインを使うが、データ源はRSSではなくGoogle Search Console API / GA4 Data APIにし、最後にClaudeで改善提案を生成する一段を追加する。

接続基盤（`fetch_search_console.py`の`get_search_console_summary()`、`fetch_ga4.py`の`get_ga4_summary()`、サービスアカウント認証）は実装・実データ確認済み。以下を追加する。

- **Search Console取得の拡張**（`fetch_search_console.py`）:
  - `get_query_performance(start, end, row_limit=50)`: dimension=queryでクエリ別clicks/impressions/ctr/position
  - `get_page_performance(start, end, row_limit=50)` / `get_device_performance(start, end)`: dimension=page/device
  - `get_search_console_report(days=28)`: 直近days日と直前days日を取得し、クエリ単位でclicks/impressions/ctr/positionの差分を計算して返す
- **GA4取得の拡張**（`fetch_ga4.py`）:
  - `get_ga4_report(days=7)`: 直近days日と直前days日のactiveUsers/sessions/screenPageViews/newUsers（可能ならengagementRate/averageSessionDuration）を比較付きで取得
  - `get_ga4_top_pages(days)` / `get_ga4_traffic_sources(days)`: dimension=pagePath / sessionSource,sessionMedium
  - `discover_ga4_events(days=28)`: dimension=eventName, metric=eventCountで実在するイベント一覧を取得する（問い合わせ系イベント名を推測で決め打ちしない。実装済みイベントが無ければ「計測未整備」として扱う）
- **`piano_analytics.py`（新規）**: 分析ロジックを集約する
  - クエリ分類: 表示回数多×CTR低 / 掲載順位4〜15位（改善余地） / 順位上昇・下降 / クリック数増減 / 地域クエリ（`config.PIANO_LOCAL_KEYWORDS`: 水戸・茨城等との部分一致） / ブランドクエリ（`config.PIANO_BRAND_KEYWORDS`: 小野ピアノ等との部分一致）を判定する
  - Search ConsoleとGA4はユーザー単位で結合できないため、同一ユーザーとして関連付けない。期間単位・ページ単位の並記にとどめ、推測（LLMによる提案）と実測値（API取得値）を明確に区別して表示する
  - `generate_recommendations(report_text)`: 上記の構造化レポートをもとに、`llm.ask_claude_tiered()`で優先度順Top3の改善提案を生成する。各提案は根拠データ・問題・推奨施策・期待効果・優先度を含む形式を必須とし、「SEOを改善しましょう」のような抽象的な提案のみを許さないようプロンプトで明示する
  - `run_piano_analysis()`: 取得→分類→レポート文生成→改善提案生成→Source/Knowledge保存までを行う。Search Console・GA4いずれかの取得が失敗しても処理を止めず、取得できた方のデータのみで続行し、`[WARN]`としてログ・レポートに明記する（認証キー等の秘密情報はログに出さない）
- **データ保存**: 新しいDB機構は追加しない。1回の実行で日次スナップショット（生データのJSON的テキスト）をSourceノード1件、レポート本文と改善提案をKnowledgeノード1件として保存する（category=piano）。既存のnodes/edgesの仕組みをそのまま使うことで、`created_at`の蓄積が日次トレンドの基礎データになる
- **run_daily.py**: 既存のRSSパイプラインに加えて`piano_analytics.run_piano_analysis()`を呼び出す（失敗してもrun_daily.py全体は止めない）。カテゴリ別レポートのループに`piano`を追加する。`generate_report.py`自体の変更は不要（Knowledgeをそのまま表示する既存の仕組みで足りる）
- **対象外（今回やらないこと）**: Webサイト本文・title/description・Google Business Profile・GA4/Search Consoleの設定変更、広告出稿は一切行わない。データ取得→分析→改善提案の提示までに限定し、実際の施策実行は人間の承認後に別タスクで行う
- 受け入れ条件:
  - Search Console・GA4のどちらか一方が失敗しても、残りのデータで処理が継続し、最終的にレポートが生成される
  - レポートに「前期間比」の数値（増減・ポイント差）が、ゼロ除算やデータ不足時も安全に表示される
  - 問い合わせ系イベントが実装されていない場合、イベント名を創作せず「計測未整備」と明記される
  - 改善提案が優先度順に最大3件、根拠データ・問題・推奨施策・期待効果付きで生成される
  - 実データで1回実行し、Search Console/GA4の実測値と改善提案Top3が得られる

#### 4-8. 外部プロジェクト（1004_Insight_Reporter）との連携: 上位ソース化

定期情報収集は、対応するレポート種別については隣接プロジェクト `1004_Insight_Reporter` が担う。JARVISはそれを**上位ソース**として優先的に参照し、情報が不足する場合のみJARVIS独自に調べる（フォールバック）。

- `ingest_insight_reporter.py`: `1004_Insight_Reporter/reports/<report_id>/latest.json` を読み取り、JARVISのSource/Knowledge（category=対応カテゴリ）として取り込む。JARVISはInsight Reporter配下のファイルを変更・importしない（読み取り専用）
- 「上位ソースとして十分」の判定（`is_sufficient()`）: レポートが存在し、必須フィールド（`report_id`/`generated_at`/`metrics`）が揃い、生成から既定10日以内であること
- 不十分な場合（レポートが存在しない、古い、必須フィールド欠落）は、JARVIS独自の取得にフォールバックする。①ピアノ教室の場合は `piano_analytics.run_piano_analysis()`
- `piano_analytics.py` に `run_piano_update()` を追加し、まず `ingest_insight_reporter.ingest_report("piano_growth")` を試み、不十分なら `run_piano_analysis()` にフォールバックする。`run_daily.py` はこの関数を呼ぶ
- 現時点でInsight Reporterが実装しているレポート種別は `piano_growth` のみ。他カテゴリ（news/trading/realestate/manufacturing/minpaku）はInsight Reporter側に型が追加されるまで、引き続きJARVIS独自のRSS収集パイプラインを使う
- 受け入れ条件:
  - `latest.json` が存在しない場合、JARVIS独自のpiano_analyticsにフォールバックし、処理が止まらない
  - `latest.json` が存在し十分な場合、Insight Reporter側のデータがSource/Knowledgeとして取り込まれ、JARVIS独自の重複収集は行われない
  - 取り込んだKnowledgeの出典がInsight Reporterであることが分かる形で記録される

- 2026-10（MAP Phase 5）: レポートの取得は、まず Insight Reporter の API（`GET /api/v1/reports/{id}/latest`、接続先は Architecture Registry）で行い、API が止まっている・失敗したときは従来どおり `latest.json` を読む（`insight_reporter_api.py`）

#### 4-9. 民泊: oarai-project-os（大洗Project OS）との連携

⑥民泊の情報源として、`unikarei/oarai-project-os-since2026`（private、大洗物件の購入判断〜開業準備を
管理する独自の「Project OS」）を追加する。GitHub上の `data/*.json` / `docs/**/*.md` を正本として読み込み、
将来的には本番API（`https://oarai.rebirth-lab.site/`、2026-10-09時点で`/api/*`は500エラーで停止中）が
復旧次第、対話から抽出した構造化変更案を提案できるようにする。

**読み込み（`ingest_oarai_project_os.py`）**:
- GitHub API（`gh auth token`認証）で `data/*.json`（activities/budget/changes/decisions/evidence/
  procurement/risks/schedule/tasks/topics/vendors）と `docs/**/*.md`（`docs/templates/`配下の雛形は除外）、
  `README.md`/`AGENTS.md` を取得し、Source/Knowledge（category=minpaku）として保存する
- JSON（構造化データ）は要約せず、整形したJSON文字列をそのままKnowledgeのcontentとする。Markdownは全文
  保存する（民泊Obsidian取り込みと同じ方針）
- 既存ノートの内容が変わっていれば新規作成せずcontentを更新する（`ingest_obsidian.py`と同じ更新ロジック）

**提案（`oarai_project_os.py`、対話からの「改造」機能）**:
- `docs/09_it_dx/orchestrator-ingestion-contract.md` が定義する ingestion envelope
  （`source`/`source_ref`/`occurred_at`/`raw_summary`/`proposed_changes`/`tags`）と同じスキーマで
  提案を構築する `build_ingestion_envelope()` / `propose_change()` を実装する
- `decision_candidate` は常に `requires_user_approval=True` とする
- 本番API（`GET /health`）が応答する場合のみ `POST /api/ingest` で送信する。応答しない場合は、提案を
  JARVISのDBにTaskノード（category=minpaku、タイトルに「未送信」である旨を明記）として保持し、処理を
  止めない
- `resubmit_pending_proposals()`: 保留中のTaskノードを走査し、API疎通が回復していれば送信し直す
- JARVISはoarai-project-os側の `data/*.json` を直接書き換えない。GitHubへの直接コミットによる
  ステージングも行わない（`runtime/`は同リポジトリの`.gitignore`対象であり、GitHubコミットでは本番の
  ランタイムに反映されないため）
- 受け入れ条件:
  - `data/*.json`・`docs/**/*.md` がSource/Knowledgeとして取り込まれ、`ask.py`で大洗Project OSの
    タスク・リスク・決定事項等について質問に回答できる
  - 再実行してもURL重複により新規ノードが増えない。内容変更時はcontentが更新される
  - API未稼働時に提案してもエラーで停止せず、未送信としてJARVIS側に保持される
  - API稼働時は実際に`POST /api/ingest`で送信され、`ingestion_id`が得られる

#### 4-10. Conversation Knowledge Collector（CKC）Phase 1: 明示的なナレッジ登録

全体仕様 `../docs/spec.md` §3.3（FR-013）を正とし、ここには本リポジトリでの実装の要点だけを書く。設計は `../docs/architecture.md` §10、受け入れ基準は `../docs/testing.md`、タスクは `../docs/tasks.md`（T-101〜T-117）。

- 会話（貼り付け・テキストファイル・`chat.py` の対話）から、LLM で知識の候補を抽出し、6種類（Decision / Preference / Hypothesis / Finding / Task / Question）に分類する
- 候補を CLI（`ckc_cli.py`）で一覧・修正・承認・却下し、承認したものだけを Vault の `_jarvis/knowledge/<種類>/` に1件1ノートで保存し、ナレッジグラフに Source（会話）と Knowledge（知識）として登録する
- 保存先: 大洗だけに関わる知識は `oarai-obsidian`、それ以外は `jarvis-knowledge`（`../docs/spec.md` §3.3.8）
- 会話の原文と候補は Vault に置かず、`data/ckc/`（Git の外）と `jarvis.db` に置く
- LLM は `llm.complete()` を通して使い、既定は `claude -p`（ツールなし）。既存の `ask_claude` / `ask_claude_tiered` は変えない。失敗時に有料 API へ自動で切り替えない
- 開発・テストは worktree と開発用の `.venv` で行い、本番の `jarvis.db`・Vault・定期処理に影響させない（`../docs/spec.md` §3.3.12）

#### 4-11. スキルと会話からの実行（FR-014 Phase 1）

全体仕様 `../docs/spec.md` §3.4（FR-014）を正とし、ここには本リポジトリでの実装の要点だけを書く。設計は `../docs/architecture.md` §19、ADR-0021・0022、受け入れ基準は `../docs/testing.md` §9、タスクは `../docs/tasks.md` §12（T-1205〜T-1210）。4-3 の「スキル蓄積」（Skill ノードに名前を記録するだけ）を、この節で置き換える。

- スキルの宣言（何ができ、どう呼び、どの権限か）は別のリポジトリ `jarvis-skills` に置く。本リポジトリはその生成物 `generated/skills.json` を読むだけで、スキルの宣言を書き換えない
- `chat.py` の発言は `orchestrator.py` が振り分ける。①スキルの発言の例・決まった言い回しのルール、②一致しなければ `llm.complete()`（既定は `claude -p`）でスキルの索引から選ぶ。LLM の選択は索引に照らして検証し、索引にない動作をしない
- 普通の質問は `knowledge_engine.answer()` で答える。蓄積した知識に答えがあれば出典の番号を付けて答え、なければ LLM 自身の一般的な知識で答える（出典は使ったものだけ、直近の会話を渡す。2026-10-17 Owner 指示、`../docs/decisions.md` ADR-0026）。`ask.py` は Knowledge Engine が使えないときの予備
- ルール（集計などの経路 C を含む）にも LLM の振り分けにも当たらない発言は 2 つに分ける（`../docs/decisions.md` ADR-0027、T-1403）。A. 通常の会話は `general-chat`（検索せず、ローカル LLM の Ollama。失敗したら待機させた Claude `claude_warm` に切り替え、有料の API には切り替えない）。B. ナレッジの質問は `knowledge-answer`（検索と出典。LLM は `claude_warm`、失敗したら `claude_cli` で一度だけ）。どちらも、答えの一部を表示・読み上げた後の失敗では切り替えず、エラーにする（途中の答えと別の LLM の答えを混ぜない）
- ナレッジの質問の判定（`orchestrator.is_knowledge_question`）: ナレッジの語（「ナレッジ」「メモ」「出典」など）、プロジェクトの語（「大洗」「民泊」「ピアノ」など、Registry の project_id すべて。英数字の語として大文字・小文字を区別せず照合し、呼びかけと区別できない「jarvis」だけは除く）を含めば B。それらの語がなく、一般知識の質問の形（「〜って何」「〜とは（文末か「とは何」の形。「ことは」「あとは」は除く）」「〜の仕組み」「〜の使い方」「〜の違い」など）なら A（検索の点数は一般の語でも高くなるため、2026-10-18 Owner 指示）。それ以外は、検索の最上位の点数が `JARVIS_KNOWLEDGE_SCORE_THRESHOLD`（既定 8.0）以上なら B。最近の出来事を聞く語（「最近」「最新」「先週」「動向」「発表」など）があれば、その語を除いた話題で検索し、しきい値を 4 分の 1 にする（語だけでは B にしない。「最近」自体が検索で約 7 点になるため）。迷えば A。スキルの索引に `general-chat` がなければ従来どおりすべて B
- 権限の段階: `read` は確認なしで実行、`local_write` は実行のたびに確認、`external` は Phase 1 では扱わない（索引にあっても実行しない）
- 対話の間だけ直前の結果（知識の ID・出典）を覚え、「その出典は？」「2 番目の出典」に答える
- 既存の「ナレッジ化して」「テーマ登録:」、コードの依頼は、同じ動作のままスキル（`knowledge-capture`・`theme-register`・`code-task`）として扱う。スキルの索引が読めなくても、この 3 つと普通の質問は予備の索引で使える。コードの依頼の判定は、従来の広いキーワード（「実装」「関数」など、普通の質問にも含まれる語）から「〜を書いて」「デバッグして」などの言い回しに絞る。ナレッジ化とテーマ登録は書き込みがあるため、実行の前に確認が1回増える
- 実行のたびに Task ノード、使ったスキルの Skill ノードへの `uses` エッジ、`meta` に成否・所要時間・振り分けの方法（rule / llm）を記録する。Skill ノードはスキルの `name` と対応させ、既存の 2 件（「ナレッジ質問応答」「コードタスク実行」）は `knowledge-answer`・`code-task` に結び付ける
- 受け入れ条件: 会話で「民泊の補助金について知ってることは？」→「その出典は？」→「システムの状態は？」→「次にやることは？」に、それぞれ想定したスキルで答える。`local_write` のスキルは確認してから実行する

## 4. データモデル

- ノード型: Category / Task / Knowledge / Source / Skill / Agent
- エッジ型: belongs_to / produced / derived_from / depends_on / uses / executed_by
- 詳細は architecture.md を参照

## 5. 非機能要件

- セキュリティ: APIキーは環境変数。Gitに秘密情報とDBを含めない。売買・SNS投稿など取り返しのつかない操作は人間の承認を必須とする
- 運用環境: 開発・実行の中心はMac mini（48GB）。WindowsはTailscale / SSH経由の操作端末（JARVIS-MAP-002 §3）。Phase 1〜4 の動作確認はWindowsで実施済み。Phase 1・2 は Mac mini で動作確認済み（2026-10-11、`run_daily.py`・`ask.py`・Google API 接続）。2026-10-11 から Mac mini の `jarvis.db` を正本とし、定期実行も Mac mini（launchd）に移した。Windows の定期実行は停止済み
- 外部コンテンツ内の指示には従わない（プロンプトインジェクション対策）
- コスト最適化: エージェント・API呼び出しは「運用コストの最小化」を最優先とし、タスクの複雑度に応じて最も安価なモデル／最も高精度なモデルを自動選択する（階層型エスカレーション、詳細は architecture.md）。コード生成・修正・デバッグは専任のコード実行サブエージェントに委譲する
