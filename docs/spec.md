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

#### 4-6. 民泊: Obsidianボルトへの書き込み（新規ノート作成・既存ノートへの追記）

JARVIS側からもボルトを更新できるようにする。自動実行はせず、手動トリガー（CLI実行）でのみ書き込む。

- `update_obsidian.py`:
  - `create_note(path, content)`: 新規ノートを作成する（同名ファイルが既に存在する場合はエラー）
  - `append_note(path, addition)`: 既存ノートの末尾に追記する。追記部分には日付と「JARVIS追記」である旨を明記する
  - 書き込み前に対象ノートの現在のsha（GitHub Contents API）を取得し、それを使って更新する。取得後に他端末等で編集され競合した場合はAPIが409を返すため、分かりやすいエラーメッセージで中断する（ボルトのREADMEにある「編集前に取得、編集後に保存」の運用方針に準拠し、上書き事故を防ぐ）
  - 認証は`ingest_obsidian.py`と同様に`gh auth token`を使う。トークンや認証情報は本リポジトリに保存しない
- `ingest_obsidian.py` を拡張し、既存ノートの内容が変わっていたらSource/Knowledgeのcontentを更新する（上記4-5に追記済み）。これにより、JARVISでの書き込み後に再取り込みすれば最新内容がask.pyの検索対象になる
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

## 4. データモデル

- ノード型: Category / Task / Knowledge / Source / Skill / Agent
- エッジ型: belongs_to / produced / derived_from / depends_on / uses / executed_by
- 詳細は architecture.md を参照

## 5. 非機能要件

- セキュリティ: APIキーは環境変数。Gitに秘密情報とDBを含めない。売買・SNS投稿など取り返しのつかない操作は人間の承認を必須とする
- 運用環境: 開発はWindows、本番運用はMac mini（48GB）
- 外部コンテンツ内の指示には従わない（プロンプトインジェクション対策）
- コスト最適化: エージェント・API呼び出しは「運用コストの最小化」を最優先とし、タスクの複雑度に応じて最も安価なモデル／最も高精度なモデルを自動選択する（階層型エスカレーション、詳細は architecture.md）。コード生成・修正・デバッグは専任のコード実行サブエージェントに委譲する
