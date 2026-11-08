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

- 入力: 音声認識（faster-whisperでローカル推論。モデルは `JARVIS_WHISPER_MODEL`（既定 large-v3-turbo、2026-11-04 Owner 指示「高精度に変更」。以前は small）、Apple Silicon の Mac では mlx-whisper で GPU の上で動かし（2026-11-05 Owner 承認、ADR-0032。1 回 約 0.5 秒）、声の区間だけを Silero VAD で切り出して渡す。mlx-whisper が使えない環境と `JARVIS_STT_BACKEND=faster` では CPU の int8・`JARVIS_WHISPER_THREADS`（既定 8）スレッド。初回は Hugging Face からダウンロード）
- 出力: 音声合成（VOICEVOX ENGINE のローカルHTTP API、既定 `http://127.0.0.1:50021`）
  - 2026-10-18 から（`../docs/decisions.md` ADR-0029）: 声の会話（`voice_chat.py`）の読み上げは、音声エンジン（macOS の say〔既定〕・AivisSpeech・VOICEVOX。有料の ElevenLabs・OpenAI TTS は Owner の承認まで未接続）・話者・話速・抑揚を選べる。設定と試聴の画面は `python -m acc.voice_settings`（127.0.0.1 だけ）で、設定は `data/voice/settings.json`。HTTP エンジンの接続先は Mac の中だけ（転送は追わない）。選んだエンジンが使えないとき・途中で止まったときは say（Kyoko）で読む。長い文は読点の切れ目で先に読む。試聴の画面の「候補の比較」は、決めた候補（AivisSpeech・VOICEVOX の話者とスタイル、macOS の Kyoko・Otoya の拡張の声、Kyoko）を同じ文で試聴でき、「選ぶ」で設定の欄に入れる（保存は「この音声を使う」）。入っていない・起動していない候補は理由を示して使えないとする。有料の API は候補に含めない。選んだ音声のエンジン（VOICEVOX・AivisSpeech）が止まっていれば、声の会話（`voice_chat.py`）と評価の画面（`acc/voice_eval.py`）の間だけ起動し（127.0.0.1 だけ、使えるまで待つ）、終わるときにその会話が起動したものだけを止める（`voice/engine_process.py`。すでに動いているものは使うだけで止めない。Jarvis の会話が起動したエンジンは使っている会話の一覧で管理し、最後の会話が終わったときだけ止める（同時に 2 つの会話でも 1 つだけ起動する）。前の会話が異常終了して残したものは引き継いで止める。止める相手は pid・開始時刻・実行ファイル・ポート・プロセスのグループで確かめる。記録を読めない・壊れているときは止めない（記録が消えたときは、その会話が起動したものだけを止める）。エンジン本体が終わって子だけが残ったときは、その実行ファイルのプロセスであることを確かめてグループごと片付ける。会話の途中でエンジンが止まったら一度起動し直して選んだ音声に戻し（30 秒に 1 回まで）、戻せなければ say（Kyoko）で読む。常駐・自動起動の設定はしない。2026-10-18 Owner 指示）
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

JARVIS側からもボルトを更新できるようにする。自動実行はせず、手動の CLI と、会話のスキル（毎回の確認つき）でのみ書き込む。

- `update_obsidian.py`:
  - `create_note(path, content)`: 新規ノートを作成する（同名ファイルが既に存在する場合はエラー）
  - `append_note(path, addition)`: 既存ノートの末尾に追記する。追記部分には日付と「JARVIS追記」である旨を明記する
  - 書き込み前に対象ノートの現在のsha（GitHub Contents API）を取得し、それを使って更新する。取得後に他端末等で編集され競合した場合はAPIが409を返すため、分かりやすいエラーメッセージで中断する（ボルトのREADMEにある「編集前に取得、編集後に保存」の運用方針に準拠し、上書き事故を防ぐ）
  - 認証は`ingest_obsidian.py`と同様に`gh auth token`を使う。トークンや認証情報は本リポジトリに保存しない
- `ingest_obsidian.py` を拡張し、既存ノートの内容が変わっていたらSource/Knowledgeのcontentを更新する（上記4-5に追記済み）。これにより、JARVISでの書き込み後に再取り込みすれば最新内容がask.pyの検索対象になる
- **ローカル Vault への改修（4-10 の前提、`../docs/decisions.md` ADR-0016）**: 書き込み先を GitHub Contents API から Mac mini のローカル Vault（`~/work/obsidian/<Vault>`）に変える。`create_note` / `append_note` の関数名・引数・CLI・追記の書式は変えない。sha 照合の代わりに、読み込み時と書き込み直前の内容のハッシュ、対象ファイルの未コミット変更、Git の操作中（merge・rebase・ロック）を確認し、一時ファイルから原子的に置き換える。Git の commit・push はしない（Obsidian Git に任せる）。書き込みの共通部分は `vault.py` に置き、CKC と共用する
- **会話のスキル `obsidian-note`**（統合ワークスペースの spec §3.4.11・ADR-0033、T-1702）: 「Obsidian」と操作の語の両方を含む発言で使い、操作（追加・作成・部分／全体の書き直し・削除）・Vault・ノート・場所・内容のうち足りないものを 1 つずつ聞き返す（`orchestrator.Context.pending`、2 分・同じ項目 3 回で中止）。書く前に内容を示して確認し、削除と全体の書き直しは 2 段目に「削除」「置き換え」の入力を求める（ブラウザでの声の会話では画面の入力欄に入力し、語の照合はサーバー）。取り出しはローカル LLM（Ollama）だけで、このスキルの発言・答えは会話の履歴に入れず、記録には操作・Vault・パスと短い結果だけを残す。
  - 本体は `obsidian_note.py`。書き込みは `vault.py`（どの階層でも `.git`・`.obsidian`・`.trash` を拒否、Markdown だけ、`list_notes`・`delete_file`・`restore_file` を追加）を通す。削除はノートから取り込んだ知識（Knowledge・ほかから参照されない Source・エッジ）も JSON に残してから消し、DB の削除に失敗したらノートを戻す。CKC のノート（`_jarvis/`）は本文だけを変え、その Knowledge の本文を更新する。それ以外は書いた後に `ingest_obsidian` で取り込み直す
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
- **節ごとの取り込み（T-1419、統合ワークスペースの `docs/project-os-split-design.md`、2026-11-03 Owner 承認）**:
  本文が `SECTION_MAX_CHARS`（1,000）字を超えるファイルは `split_sections()` で節に分け、節ごとに Knowledge を作る
  （1,000 字以下のファイルは今のとおり 1 件、`section_key` は `"all"`）。ファイルの Source は全文のまま
  - Markdown: 行頭の `## ` で分ける（フェンス ``` の中では分けない。最初の `## ` より前は「冒頭」）。1,000 字を超える節は
    `### ` で分け（見出し「<##> › <###>」）、まだ超えるときは空行の段落で 1,000 字以内に詰める（2 つ目から「（続き n）」）。
    1 段落が 1,000 字を超えるときだけ文字数で切る
  - JSON: 最上位のオブジェクトの配列の要素ごとに整形し、順に 1,000 字以内に詰める（1 要素が超えるときはその要素だけで
    1 節）。見出しは「<配列の名前> <最初の id>〜<最後の id>」（id がなければ title・name、それもなければ番号）。配列以外の
    項目（空の配列を含む）は最初の節の先頭にまとめる（それが 1,000 字を超えるとき・空でない配列がないときは、それだけを
    行で 1,000 字以内に詰めた「冒頭」の節にする。1 行が超えるときだけ文字数で切る）。読めない JSON・最上位がオブジェクト
    でない JSON は 1 節。節の本文は 1 節のときの本文（`# <path>`・```json・外側の `{ }`・配列の名前の行 `"tasks": [`・
    `]`・区切りの `,` を含む）を切り分けたもので、すべての節をつなぐと空白以外の文字がすべてそろう（配列以外の項目を
    先頭に寄せるため、並びだけが変わることがある）
  - 節の Knowledge: title `<ファイル名> › <見出し>（大洗Project OS）`、content の先頭に `# <ファイル名> › <見出し>`、
    url `…/blob/main/<path>?plain=1#L<開始>-L<終了>`（行がわからない JSON は `#part-<番号>`。長い 1 行を文字数で切って
    行の範囲が重なる 2 つ目からは `?plain=1&part=<番号>#L<開始>-L<終了>` で一意にする）、meta `repo_path`・
    `section_key`（見出しの階層と同じ見出しの中の順番。行番号は使わない）・`section_index`（移行で置き換えたものは
    `replaces`）。各節 → ファイルの Source に `derived_from`。DB のスキーマは変えない
  - 再取り込み: 内容が変わったファイルは節を作り直し、既存の節と `section_key` で対応づけて、変わった節を更新・新しい節を
    追加・**なくなった節は Knowledge とその辺を削除**する（1 ファイル 1 トランザクション。戻り値・表示に `deleted`）。
    `section_key` のない既存の Knowledge（移行の前）は消して節にし、新しい節の `replaces` に元の ID を入れる
  - 移行 `--migrate-sections`: GitHub は呼ばない。DB を `data/backups/jarvis-<日時>-before-sections.db`（DB と同じ
    フォルダの下。日時はマイクロ秒まで、同じ名前があれば番号を付け、既存のバックアップを上書きしない）に sqlite3 の backup API で写してから、Project OS の Source ごとにファイル単位の Knowledge を節の
    Knowledge に置き換える（全体で 1 トランザクション、節になっているファイルは飛ばす）
  - 戻し方 `--rollback-sections <バックアップ>`: Project OS の Knowledge をバックアップの時点の状態にそろえる。今の Project OS の
    Knowledge（節もファイル単位も）と辺を消し、バックアップにある Project OS の Knowledge（移行の前から節だったものを含む）と
    `derived_from` の辺、ほかのノードとの辺（相手が今もあるもの）を同じ ID で戻す（ほかの知識には触れない。1 トランザクション）。
    戻すファイル単位の Knowledge が 1 件もないバックアップ（2 回目以降の移行のときのものなど）は、何も変えずに断る。
    移行したファイルが 0 件のときは、そのバックアップは戻し方に使えず、最初の移行のときのバックアップを使うよう表示する
  - 集計（`knowledge_engine.stats`）は節の Knowledge を根拠の Source ごとに 1 件と数える。評価（`evaluation/runner.py`）
    の順位の照合は、知識の ID か `meta.replaces` が問の `knowledge_id` と同じなら当たりとする

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

- ブラウザでの声の会話（T-1506、Owner 事前承認・2026-11-03 指示）: `python -m voice.web --port 8772 --allow-host ohidemac-mini.taild8cb51.ts.net` で許可した名前を、`tailscale serve --bg --https=8446 http://127.0.0.1:8772` により tailnet 内の HTTPS から開ける。ACL に tcp:8446 の許可が必要で、インターネットには公開しない。サーバーは 127.0.0.1 のみで待ち受ける。追加名は `--allow-host` または `JARVIS_VOICE_ALLOWED_HOSTS`（カンマ区切り）で指定し、DNS 名の形だけを採用する。Host は許可名ならポートを問わず、POST の Origin は追加名の HTTPS のみを許す。localhost・127.0.0.1 の HTTP と既存のトークン・Cookie・ヘッダー確認は維持する。

#### 4-11. スキルと会話からの実行（FR-014 Phase 1）

全体仕様 `../docs/spec.md` §3.4（FR-014）を正とし、ここには本リポジトリでの実装の要点だけを書く。設計は `../docs/architecture.md` §19、ADR-0021・0022、受け入れ基準は `../docs/testing.md` §9、タスクは `../docs/tasks.md` §12（T-1205〜T-1210）。4-3 の「スキル蓄積」（Skill ノードに名前を記録するだけ）を、この節で置き換える。

- スキルの宣言（何ができ、どう呼び、どの権限か）は別のリポジトリ `jarvis-skills` に置く。本リポジトリはその生成物 `generated/skills.json` を読むだけで、スキルの宣言を書き換えない
- `chat.py` の発言は `orchestrator.py` が振り分ける。①スキルの発言の例・決まった言い回しのルール、②一致しなければ `llm.complete()`（既定は `claude -p`）でスキルの索引から選ぶ。LLM の選択は索引に照らして検証し、索引にない動作をしない
- 普通の質問は `knowledge_engine.answer()` で答える。蓄積した知識に答えがあれば出典の番号を付けて答え、なければ LLM 自身の一般的な知識で答える（出典は使ったものだけ、直近の会話を渡す。2026-10-17 Owner 指示、`../docs/decisions.md` ADR-0026）。`ask.py` は Knowledge Engine が使えないときの予備
- ルール（集計などの経路 C を含む）にも LLM の振り分けにも当たらない発言は 2 つに分ける（`../docs/decisions.md` ADR-0027、T-1403）。A. 通常の会話は `general-chat`（検索せず、ローカル LLM の Ollama。失敗したら待機させた Claude `claude_warm` に切り替え、有料の API には切り替えない。何も操作・実行・保存しないので、操作を引き受けたように答えず「この会話では〜できません」と伝え、管理画面・システムの状態・ナレッジ化のときだけその言葉を案内する（2026-11-05 Owner の実機で「OK」に「管理画面を起動いたします」と答えた）。）。B. ナレッジの質問は `knowledge-answer`（検索と出典。LLM は `claude_warm`、失敗したら `claude_cli` で一度だけ）。どちらも、答えの一部を表示・読み上げた後の失敗では切り替えず、エラーにする（途中の答えと別の LLM の答えを混ぜない）
- 遅延の記録（`latency.py`、ADR-0027、T-1405）: `chat.py`・`voice_chat.py` の発言ごとに、段階（`segment` 話し終えてから録音の区切りまで・`stt`・`route`・`search`・`llm_ready`・`first_text`・`first_audio`・`done`）までの経過ミリ秒と、決まった属性（`mode`・`skill`・`provider`・`warm`・`fallback`・`ok`）だけを `JARVIS_LATENCY_LOG`（既定 `data/latency/latency.jsonl`、5 MB で `.1` に回して 1 世代だけ残す）に 1 行の JSON で追記する。発言・答えの本文・音声は記録しない。書き込みの失敗で会話を止めない。集計は `python -m latency report [--days N] [--mode voice|text|web] [--skill NAME]`（web はブラウザでの声の会話、段階 audio_ready は first_text と first_audio の間、T-1502）（段階ごと、および skill・provider・warm の組ごとの件数・p50・p95。p95 は最近傍順位法）。声の会話の始まりは、区切りが決まった時刻から無音の長さ（`JARVIS_VOICE_SILENCE_SECONDS`）を引いた時刻、`first_audio` は最初の文の音を再生に渡した時点
- 起動時の読み込み（T-1405）: `chat.py`・`voice_chat.py` を起動したら、裏のスレッドで `orchestrator.warm_up()` を呼び、索引に `general-chat` があれば Ollama のモデルを（`keep_alive` 30 分）、`knowledge-answer` があれば待機させる claude を用意する。失敗しても何も出さずに会話を始める。常駐・定期実行は変えない
- 実マイクの評価の画面（`python -m acc.voice_eval`、127.0.0.1 だけ、ADR-0027、T-1406）: 決まった 10 の文（`EVAL_SENTENCES`）を Owner が 1 件ずつ読み上げ、Mac のマイクで録って文字にし、認識の誤り（CER。NFKC で正規化し空白・句読点・記号を除いた編集距離 ÷ 正解の文字数）と、話し終えてからの段階ごとのミリ秒（`segment`・`stt`。`mode="full"` では `route`・`first_text`・`first_audio` も）を測る。同時に 2 件は測らない（409）、マイクがなければ 503
- `mode="full"` は振り分けた後、権限が `read` で確認のいらないスキルだけを実行し、選んだ音声で最初の 1 文を読み上げて最初の声の再生開始で止める。書き込み・確認のいるスキル（例: ナレッジ化）は実行せず、振り分けの結果だけを示す。結果（番号・認識した文・CER・時間・スキル・LLM）は `data/voice/eval/<日時>.json` に保存し、CER の平均と `first_audio` の p50・p95（最近傍順位法。目標 p50 2 秒・p95 5 秒）の達否を画面に示す。音声はメモリの中だけで扱い、保存しない（読み上げも一時ファイルを使わない経路だけ: say は `-o` を使わず直接再生し、起動の時刻ではなく `say -i` が再生に合わせて最初の語を強調した時刻（擬似端末で受ける）を最初の声とする。強調が届かなければ最初の声は記録しない。ローカルの HTTP エンジンはメモリの中で合成する）。評価の ID はマイクロ秒までで一意にし、計測中の「新しい評価」は 409。遅延の記録（`latency.jsonl`）には書かない
- ナレッジの質問の判定（`orchestrator.is_knowledge_question`）: ナレッジの語（「ナレッジ」「メモ」「出典」など）、プロジェクトの語（「大洗」「民泊」「ピアノ」など、Registry の project_id すべて。英数字の語として大文字・小文字を区別せず照合し、呼びかけと区別できない「jarvis」だけは除く）を含めば B。それらの語がなく、一般知識の質問の形（「〜って何」「〜とは（文末か「とは何」の形。「ことは」「あとは」は除く）」「〜の仕組み」「〜の使い方」「〜の違い」など）なら A（検索の点数は一般の語でも高くなるため、2026-10-18 Owner 指示）。質問・依頼の形（「？」「〜か」「かな」「っけ」「何」「どこ」「どう」「いつ」「教えて」「聞きたい」「調べて」「一覧」「について」など。`orchestrator.QUESTION_FORM`）でない文は雑談として A（検索の点数は関係のない記事にも 8〜13 点で当たるため。2026-11-04 Owner 指示「雑談はナレッジ不要」。評価セット 125 問の振り分けは変わらない）。それ以外は、検索の最上位の点数が `JARVIS_KNOWLEDGE_SCORE_THRESHOLD`（既定 8.0）以上なら B。最近の出来事を聞く語（「最近」「最新」「先週」「動向」「発表」など）があれば、その語を除いた話題で検索し、しきい値を 4 分の 1 にする（語だけでは B にしない。「最近」自体が検索で約 7 点になるため）。迷えば A。スキルの索引に `general-chat` がなければ従来どおりすべて B
- ブラウザでの声の会話（`../docs/spec.md` §3.4.9、`../docs/browser-voice-spec.md`、ADR-0031、受け入れ基準 `../docs/testing.md` §13）: 手元の PC（Windows）のブラウザを耳と口にし、認識・回答は Mac mini で行う。`python -m voice.web [--port 8771]` を手で起動し（常駐・自動起動はしない）、表示された `http://localhost:<番号>/?token=<トークン>` を VS Code のポート転送で開く。Mac の音声の機器は調べない・開かない。T-1501（土台）の範囲は、「会話を始める」を 1 回押した後、ブラウザが音量で発話を自動で区切り（無音 `JARVIS_VOICE_SILENCE_SECONDS` 既定 0.8 秒、最長 20 秒で区切って知らせる、前 0.3 秒を含める、0.3 秒未満は送らない）、WAV を POST → 16 kHz にして認識 → `orchestrator.handle` → 認識した文と回答（逐次）を SSE で画面に文字で出すまで。同時に 1 つの会話だけ（2 つ目は 409）。確認の要るスキルは、T-1504 から画面での承認で実行する（下記）。T-1502（読み上げ）で、回答を `voice.duplex.DuplexSpeaker`（文の区切り・長い文の読点での区切り・コードを読まない・発言の番号で古い文を捨てる）で文ごとに合成し、再生の部品 `voice/web/player.py` の `BrowserPlayer` が 1 文ずつ WAV（PCM 16 bit・モノラル・16 kHz、メモリの中）にして推測しにくい ID を付けて保持し、イベント `audio`（turn・id・index・text・seconds）でブラウザに知らせる。ブラウザは `GET /api/audio/<id>` で取って Web Audio で index の順に続けて再生し（全文を待たない）、始め・終わりを `POST /api/played`（`{"turn", "id", "event": start|end|error}`、別の発言の番号は 409）で知らせる。逐次表示した回答は区切りの残りを、逐次表示しなかった回答（一覧など）は `speech_summary` の要約を読む（`voice_chat.py` と同じ規則、`voice_chat.speak_rest`）。発言の処理はブラウザが最後の文を再生し終えるまで続き、履歴には Owner の発言と、ブラウザが再生し終えた文だけを残す（空なら AI の行を入れない。`error` の文は入れない）。その後に `turn_done` を送る。文ごとに「長さ＋5 秒」を過ぎても終わりの知らせがなければ、その文は聞いていないものとして待つのをやめる。保持する音は会話ごとに 64 文・20 MB まで（超えたら古いものから捨てる）、再生を終えた・捨てた音はすぐ消す。音声の設定は `tts.load_settings()`、エンジン（VOICEVOX・AivisSpeech）は会話の開始で起動し始め、最初の合成の前にだけ待ち、会話の終わり（「終了」・`/api/end`・SSE の切断・サーバーの停止）で止める（この会話が起動したものだけ、`EngineProcess` の規則）。エンジンが使えない・say に切り替えた・戻った知らせはイベント `notice` で画面の注記に出す（有料のエンジンには切り替えない）。T-1503（割り込み・再生中のしきい値・異常の表示・切断、BV-06・BV-07・BV-10・BV-11）: 会話の開始時、背景の音を測った後（1 秒）にページが `POST /api/calibrate` を送り、サーバーはあいさつ「ジャービスです。どうぞ。」を 1 回だけ合成して `audio`（`"calibration": true`、番号 0）で送る（履歴・遅延の記録に入れない。2 回目・発言の後は 409、応答は `{"audio": 送れたか}`）。ページはあいさつの再生中（start〜end）のマイクの RMS の p95 を回り込みとし、再生中のしきい値を max(通常のしきい値, 回り込み × `barge_margin`) にする（`/api/config` で渡す。既定 5.0、`JARVIS_VOICE_BARGE_MARGIN`。合成・再生できなければ回り込み 0 で続けて注記に出す）。会話の間は発話の検出を止めず、再生している間（文の start から最後の文の終了の 0.3 秒後まで）は再生中のしきい値、それ以外は通常のしきい値で判定する（直近 0.16 秒のうち 0.12 秒）。考えている間・話している間に話し始めたら、ページはサーバーの応答を待たずに再生を止め（`AudioBufferSourceNode.stop`）、待ちの音と取りに行く途中の要求を捨て、`POST /api/interrupt`（`{"turn"}`、今の番号のときだけ受け付け、違えば 409）を送り、話し始めの前 0.3 秒を含めて録り続ける。画面の注記に「話し始めから停止まで約 N ms（検出 a・停止 b）」を出す（話し始めは判定の窓で最初に超えたフレーム、停止は stop() の時刻＋`outputLatency`＋`baseLatency`。考えている間の割り込みも「（考えている間）」を付けて出し、停止は待ちの音を捨てた時刻とする）。`/api/utterance` の応答（番号）の前に割り込んだときは、番号が分かるまで新しい番号のイベントを捨てずに保持し、分かったら割り込んだ発言のものを捨てて残りを処理する。サーバーは番号を進めて古い合成・保持した音を捨て、生成を取り消し（`ctx.cancel`・`ctx.stream` は番号が違えば何も送らずに `voice_chat.Cancelled`）、取り消せない処理の結果は使わず文脈を分ける（`voice_chat.run_barge_in` と同じ）。割り込まれた発言の履歴は Owner の発言と、ブラウザが再生し終えた文に `voice_chat.INTERRUPTED_MARK` を付けたもの（聞いた文がなければ AI の行なし。認識が終わる前に割り込まれた発言は履歴に入れない）、記録（`run_task.record`）は結果が出ていればする、遅延は ok=False。考えている間・話している間に `POST /api/utterance` が届いたら（`/api/interrupt` が先でなくても）同じ割り込みにしてから新しい発言にする（409 にしない。新しい番号は割り込まれた発言より大きい）。割り込まれた発言のイベントはその後に送らず、`turn_done`（`"interrupted": true`）を 1 回だけ送る。ページは今の番号と違う・割り込んだ発言のイベントと、`seq` が前に受けたもの以下のイベントを捨てる。異常は区別して表示し次の操作を示す: マイクの拒否（`NotAllowedError`）・マイクがない／抜けた（`NotFoundError`・トラックの `ended`、会話を終える）・再生の失敗（その文を飛ばして続け、続けて 3 文で注記）・通信の切断（SSE の `onerror`・想定外の POST の失敗（通信の例外・5xx・会話がない 404 など。古い番号の 409、もう保持していない音の `/api/played` の 404、受け付けない音声の `/api/utterance` の 400・413、測定済みの `/api/calibrate` の 409 は想定内として続ける）・15 秒イベントも ping も来ない。会話を終えマイクを解放し、自動で再開・古い発言の再送はしない。もう一度「会話を始める」で新しい会話）・認識の失敗（もう一度話す）・音声エンジンの失敗（`notice`、1 文も合成できなければ `error` の `tts` を発言ごとに 1 回。文字の回答は続ける）。SSE の ping は 5 秒ごと（`event: ping`）。会話の終わり・切断では、ページは録音・再生・待ちの音・取りに行く途中の要求を止めてマイクを解放し、サーバーは番号を進めて生成を取り消し、保持した音を消し、エンジンを止める。状態の表示は「聞いています」「考え中」「話しています」。遅延は `latency` の mode `web` で、発話の受け付けから stt・first_text・audio_ready（最初の文の音を保持した時刻）・first_audio（ブラウザが最初の文の start を知らせた時刻）・done を記録する（雑音・「終了」は記録しない、失敗・取り消しは ok=False）。SSE の再接続・会話の引き継ぎはしない（切れたら会話を終える）。起動ごとのトークン（HttpOnly の Cookie `jv`）、Host・Origin は localhost・127.0.0.1（ポートは問わない）、POST は `X-Jarvis-Voice: 1` が必須。音声（発話・読み上げの WAV）はメモリの中だけで扱い、ファイル・DB・ログ・ブラウザの保存領域に残さない（取った WAV は再生の後に参照を外す）
- 声の会話の応答の速さ（2026-11-03 Owner 指示「A,B 実装して」。T-1505 の実機の会話で、ナレッジの質問の最初の声まで 7.4〜8.0 秒）: (A) 声の会話のプロセス（`voice_chat.py`・`python -m voice.web`）は、起動時に `llm.use_claude_model(config.VOICE_CLAUDE_MODEL)` で、そのプロセスの `claude -p`（claude_cli・claude_warm）に `--model` を渡す。既定は `claude-sonnet-5-5`（`JARVIS_VOICE_CLAUDE_MODEL`、空なら CLI の既定）。`chat.py`・定期ジョブ・評価は別のプロセスで、CLI の既定のまま。サブスクリプションの範囲で、追加の費用はない。(B) 読み上げで、発言の最初の文だけ `tts.FIRST_PHRASE`（24 字）を超えたら読点で区切って先に合成・再生する（2 つ目からは今までどおり `LONG_SENTENCE` 60 字。`voice.duplex.DuplexSpeaker` の共通の動きで、CLI の声の会話にも効く）
- ブラウザでの声の会話の画面での承認（T-1504、BV-09・AT-06、ADR-0031。Jarvis の実行時の確認（§3.4.4）の変更で、クリティカルな変更として Owner がマージする）: 確認の要るスキル（書き込み・課金のあるもの。どのスキルが確認を要るかの規則・`FLOOR` は変えない）は、キーボードの `y` の代わりに画面のボタンで承認する。`orchestrator.execute` の確認（`ask`）が呼ばれたら、その発言専用の承認の要求（推測しにくい使い捨ての ID `secrets.token_urlsafe(16)`・発言の番号・期限は要求から 60 秒）を 1 つ作り、イベント `approval`（`{"turn", "id", "text": 確認の文（末尾の「[y/N] >」を除く）, "expires_in": 60}`）を送り、「書き込みがあります。画面で確認してください。」を読み上げる。Owner は画面の「実行する」「実行しない」で `POST /api/approve?session=<ID>`（`{"turn", "id", "decision": "run"|"decline"}`。Host・Cookie・Origin・`X-Jarvis-Voice` の確認は他の POST と同じ）を 1 回だけ送る。待っている要求と ID（`hmac.compare_digest`）・発言の番号が一致し、期限内でまだ答えていなければ 200、違う・期限切れ・使用済み・会話が違う（前の会話・終えた会話）なら 409（何も実行しない）、形が違えば 400。**「実行する」が、その ID・その発言の番号・期限内・会話が続いている・割り込まれていない、のすべてを満たしたときだけ実行する。** それ以外（未承認のまま期限切れ・「実行しない」・割り込み・会話の終わり・切断）はすべて実行しない。「実行する」で確定した後も、スキルを起動する直前にもう一度、要求が取り消されておらず会話が続いていて割り込まれていないことを、割り込み・終わりと同じロックの中で確かめる（`orchestrator.Context.before_run`。スキルを動かすスレッドの中で、処理を呼ぶ直前に確かめる）。その間に割り込み・終わり・切断があれば取り消し（`cancelled`）、実行しない（`approval_done` の run は実行を始めるときに送る）。この確認を通った時点が実行の確定で、それより後の割り込み・終わりは、動き始めたスキルへの割り込みと同じく、スキルを止めず結果を使わない（取り消せない処理、BV-06）。ただし、その結果（実行して出た文）は会話の文脈・読み上げには使わず、イベント `run_result`（`{"turn", "text", "skill", "ok"}`）で画面にだけ「JARVIS（実行の結果）」として出す（承認の確認・実行するかの規則は変えない。2026-11-05 Owner の実機で、「実行する」の後に「OK」と話して管理画面のリンクが出なかった）。結果はイベント `approval_done`（`{"turn", "id", "result": "run"|"declined"|"expired"|"cancelled"}`）で知らせる（割り込み・終わりの取り消しは、`turn_done` interrupted・`ended` より先に送る）。1 つの ID は 1 回だけ使え、答えた・期限が切れた・取り消した後の同じ ID の答えはすべて拒否する。**声の発言は承認にならない**: 待っている間に話せば（「はい」でも）、割り込み（`/api/interrupt` か考えている間の新しい発話）として待っている要求を取り消し、新しい発言として扱う。ページは今の発言の番号と違う `approval` を出さず、確認の文・ボタン・残りの秒数を出し、押したらボタンを無効にし、`approval_done` で結果（実行した・実行しなかった・期限切れ・取り消し）を出してボタンを消す。割り込み・終了・切断でもボタンを消す（発話での割り込みでは、割り込んだ発言の他のイベントは捨てるが、消した要求の `approval_done` の結果は出す）。待つ間も発話の検出・割り込みは動く。端末の入力を待つスキル（`FLOOR` の「対話で入力を待つ」`knowledge-capture`・`theme-register`。承認の後もキーボードで候補ごとの承認・修正を求める）は、ブラウザの会話では確認を求めずに実行せず、「このスキルは端末での対話が要るため、ブラウザの会話では実行しません。chat.py か voice_chat.py で実行してください。」と表示・読み上げる（`orchestrator.Context.terminal=False`、`error="needs_terminal"`・`ok=True`。`chat.py`・`voice_chat.py` は既定の `terminal=True` で、確認の動きは変わらない）。管理画面（`control-center`）は、ブラウザの会話では承認の後に Mac mini の画面で `open` せず、`reports/acc/index.html` を作り直して tailnet の URL（`JARVIS_ACC_URL`、既定 `https://ohidemac-mini.taild8cb51.ts.net:8443/acc/`。:8443 はワークスペースの `docs/` の配信で、`docs/acc` は `reports/acc` へのリンク）を返す。画面は回答の https の URL を新しいタブで開くリンクにし、読み上げは URL を読まない（2026-11-05 Owner 指摘「管理画面に切り替わらない」）
- 権限の段階: `read` は確認なしで実行、`local_write` は実行のたびに確認、`external` は Phase 1 では扱わない（索引にあっても実行しない）
- 対話の間だけ直前の結果（知識の ID・出典）を覚え、「その出典は？」「2 番目の出典」に答える
- 既存の「ナレッジ化して」「テーマ登録:」、コードの依頼は、同じ動作のままスキル（`knowledge-capture`・`theme-register`・`code-task`）として扱う。スキルの索引が読めなくても、この 3 つと普通の質問は予備の索引で使える。コードの依頼の判定は、従来の広いキーワード（「実装」「関数」など、普通の質問にも含まれる語）から「〜を書いて」「デバッグして」などの言い回しに絞る。ナレッジ化とテーマ登録は書き込みがあるため、実行の前に確認が1回増える
- 実行のたびに Task ノード、使ったスキルの Skill ノードへの `uses` エッジ、`meta` に成否・所要時間・振り分けの方法（rule / llm）を記録する。Skill ノードはスキルの `name` と対応させ、既存の 2 件（「ナレッジ質問応答」「コードタスク実行」）は `knowledge-answer`・`code-task` に結び付ける
- 受け入れ条件: 会話で「民泊の補助金について知ってることは？」→「その出典は？」→「システムの状態は？」→「次にやることは？」に、それぞれ想定したスキルで答える。`local_write` のスキルは確認してから実行する

#### 4-12. ナレッジ回答の評価部門（Evaluation Unit）

全体仕様 `../docs/spec.md` §3.4.10（ADR-0030）を正とし、受け入れ基準は `../docs/testing.md` §12。ここには本リポジトリでの実装の要点だけを書く。

- `evaluation/`（`python -m evaluation build`）: 本番の DB をバックアップの機能で `data/eval/tmp/` に写し、`JARVIS_DB_PATH` をコピーにした子のプロセス（`JARVIS_EVAL_ISOLATED=1`）で処理する。子は本番の場所を指していたら始めない。終わったらコピーを消す（`--keep-copy` で残す）
- 評価セット（`evaluation/qa.py`、T-1410）: データソース（`knowledge_engine.source_by_knowledge()`、`stats()` と同じ判定）ごとに均等に知識を選び（本文 80 字未満は除く）、ローカル LLM（Ollama）で質問と要点（本文にそのままある語句 1〜3 個）を作る。質問の形は direct・paraphrase・asr・conversational を乱数の種で混ぜる
- 作問の規則の版 2（T-1414、メタ情報の `rules: 2`。`rules` のない前の版もそのまま読み込み・評価できる）: 作問のプロンプトで、何についての質問か（プロジェクト・物件・会社・製品・記事の主題などの名前）を質問に書かせ（「この〜」「その〜」「例の〜」「あの〜」だけで指さない）、要点を 30 字以内の短い語句にさせる。選ぶ段階で、題名を正規化してさらに数字・日時（数字の並びと、T・:・.・+ で区切られた時刻）を除いた文字列が同じ知識が 2 件（`SERIES_MIN`）以上あれば、繰り返し作られる系列（週次のレポートなど。回によって答えが変わる）として全件を選ばない（本文が短い知識も系列の数に入れる）。除いた件数をメタ情報の `excluded_series` に残す
- 作問の検証: JSON でない・質問が空や 200 字超・要点の数が違う・要点が 30 字（`MAX_FACT_CHARS`、正規化の前）超（`fact_too_long`）・要点が本文にない・要点が質問にある・要点がすべてほかの知識 3 件以上にもある・質問だけを渡した Ollama が「何について聞いているか分かる」に「はい」で答えない（`no_subject`）・本文と質問だけで Ollama が要点を答えられない、をこの順に確かめて捨て、理由ごとに数える（比べるときは全角半角・空白・句読点を正規化）。試行は必要数の 3 倍まで
- 点の揺れ（T-1414）: 評価（作問・答え・判定）の間は `llm.ollama_options({"temperature": 0, "seed": 0})`（`evaluation.EVAL_LLM_SEED`）の中で Ollama を呼ぶ。外では Ollama の要求は今までと同じ（本番の会話は変わらない）。Claude のプロバイダーには影響しない
- 同じ事実の知識（題名の正規化が同じ、または要点の半分以上が相手の本文にある）を 1 つの群にし、群ごとにデータソースの割合を保って dev・val・test（既定 30:15:15）に分ける。`data/eval/qa/<版>.jsonl` と `<版>.meta.json` に保存し、同じ版は上書きしない
- 調整できる値（`tuning.py`、T-1411）: `search_limit`（8）・`context_chars`（1500）・`score_threshold`（None＝`config.KNOWLEDGE_SCORE_THRESHOLD`）・`extra_knowledge_words`（なし）・`synonyms`（なし。`{"住所": ["所在地"]}` の形で、質問に左の語があれば右の語を検索の語に加える）・`answer_prompt`（`default`。ほかに資料からの回答を強めた `grounded`）。本番のコード（`knowledge_engine.search`・`answer`、`orchestrator.is_knowledge_question`）は `tuning.current()` から読み、既定の値は今の動きと同じ。評価の間だけ `tuning.use(t)` で差し替える（ファイルに書かない）
- 評価の実行（`evaluation/runner.py`、T-1411）: `python -m evaluation run --qa <版> [--split dev|val|test|all] [--no-answer] [--judge|--no-judge] [--provider ollama|claude_warm|claude_cli] [--max-llm-calls 200] [--db <パス>] [--keep-copy]`。隔離した子のプロセスで、各問を `tuning.use(t)` の中で、振り分け（`orchestrator.route`、LLM を使わない）→ `knowledge-answer` なら順位の測定（`search`、深さ max(8, search_limit)）と答え（`answer`）の順に処理する。ほかのスキルに振り分けられた問は振り分けの失敗とし、ハンドラーを呼ばない。プロバイダーは ollama・claude_warm・claude_cli だけ（有料の API はエラー）。始める前に LLM の呼び出しの見込み（LLM を使わずに同じ設定で先に振り分け、`knowledge-answer` に進む＝答えを作る問の数）を表示し、Claude は `--max-llm-calls` を超える見込みなら始めず、実行中も上限で止める（ollama は上限なし）。`--no-answer` は LLM を呼ばない
- 指標: 問ごとに振り分け（routed）・根拠の順位（rank）・recall@1/3/8（診断用）・recall_eff（根拠が search_limit 位以内）・要点の一致率（facts_hit。T-1414 で 3 段: 要点ごとに (1) 正規化した要点が正規化した答えに含まれる、(2) 正規化した要点の 2 文字の組（1 文字の要点はその 1 文字）の 0.7（`FACT_BIGRAM_RATIO`）以上が答えにあり、かつ要点の数字の並びがすべて答えにそのまま含まれる、(3) (1)(2) で外れた要点があり `--judge`（既定）のとき、外れた要点をまとめて 1 回で Ollama に判定させる（質問・答え・番号付きの要点を渡し、真偽の JSON 配列で返させる。読めない・数の合わない応答は「含まれない」。答えの中の指示には従わせない。T-1416 で判定は厳しめにし、表記・言い回しの違いは同じ意味とする一方、数値・固有名詞・用語が指す内容が違う・答えが要点より一般的やあいまい・別のものを説明している・迷ったときは「含まない」とさせ、含む 2 つ・含まない 1 つの短い例を示す。判定は答えのプロバイダーによらず常に Ollama で、Claude の上限に数えず、見込みの表示に「判定 最大 N 回（ollama）」と別に示し、実行の記録に `judge_calls` を残す。`--no-answer` では判定しない）。問ごとに文字の一致だけの割合 `facts_hit_strict`（T-1414 より前の値）と要点ごとの一致の方法 `fact_match`（exact・bigram・judge・None）も残す）・「ない」と答えたか（said_none）・秒・段階ごとのミリ秒（route・search・llm_ready・first_text・done。`data/eval/latency/<日時>.jsonl` に書き、本番の遅延の記録には書かない）・落ちた段階（route＝振り分けの失敗、search＝根拠が答えの文脈に渡らない、answer＝要点をすべては含まない。例外ならその段階）。振り分けに失敗した問・例外の後の値は 0（`--no-answer` の facts_hit・said_none は None で分母から外す）。MRR は見つからない問を 0 として全問で平均する。全体・データソース別・形別・組別に集計し、総合点 `score` = 0.2×振り分け + 0.3×recall_eff + 0.5×要点の一致率、`--no-answer` では `score_retrieval` = 0.4×振り分け + 0.6×recall_eff
- レポート（`evaluation/report.py`）: `data/eval/reports/<日時>.json`（全部の数値と問ごとの結果。答えは先頭 300 字）と `.md`（集計の表（`facts_hit_strict` の列を含む）・失敗の多いデータソースと形・失敗した問と落ちた段階と要点ごとの一致の方法・設定の値）。日時はマイクロ秒まで、同じ名前があれば `-2`・`-3` … を付けて別名にする（上書きせず、各実行を別に残す）
- 改善のループ（`evaluation/improve.py`、T-1412）: `python -m evaluation improve --qa <版> [--max-iterations 3] [--candidates 4] [--no-answer] [--judge|--no-judge] [--provider ollama|claude_warm|claude_cli] [--max-llm-calls 200] [--db <パス>] [--start <設定の JSON>] [--seed 0] [--keep-copy]`。隔離した子のプロセスで動かす
  - 探す空間 `SPACE`（ここにない値は試さない）: `search_limit` 6・8・12、`context_chars` 1000・1500・2500、`score_threshold` 6.0・8.0・10.0（None は `config.KNOWLEDGE_SCORE_THRESHOLD` とみなす）、`synonyms` 使わない／`evaluation/synonyms.json`（Git の対象。10〜20 組、左右の両向きに展開）、`extra_knowledge_words` 足さない／住所・所在地・物件・図面・Obsidian・HTML、`answer_prompt` default・grounded
  - 1 回の改善（`improve_once`）: dev の落ちた段階の多い順に関係する軸を優先し（振り分け → `extra_knowledge_words`・`score_threshold`、検索 → `synonyms`・`search_limit`、答え → `context_chars`・`answer_prompt`。同数なら この順。`--no-answer` では答えの軸を試さない）、今の設定から 1 つの軸だけを変えた候補を最大 `--candidates`（既定 4、1〜4）個作る（同じ軸の中の値の順は `--seed` の乱数。すでに試した設定は除く）。各候補を dev で評価し、総合点（`score`、`--no-answer` は `score_retrieval`）が今より `MIN_GAIN` 0.03 以上（T-1414 で 0.01 から変更。同じ設定で答えの揺れだけで 0.029 動いたため）高いものの最良を val で 1 度だけ評価し、val が今の val より `VAL_TOLERANCE` 0.01 を超えて下がらなければ採用する（下がるなら採用せず、次点は試さない）
  - ループ: 始めの設定（既定の `Tuning` か `--start`。`tuned` の結果のファイルも可。全軸が `SPACE` の中にない設定はエラーにして始めない）を dev・val で評価し、`--max-iterations`（既定 3、1〜5。6 以上はエラー）回まで繰り返す。採用のない回で止める。ループの後に test を始めと最後の設定で 1 度ずつ測る（同じ設定なら 1 度）。test の結果で設定を選び直さない
  - 過学習の防止: 評価セットは作り直さない（同じ版）。候補の手がかりは dev の失敗の傾向だけ。val は各回の採否に 1 度だけ。test はループの後に 1 度だけ。回数は既定 3 回
  - 呼び出しの見込み: 始める前に最大の数（初めの dev・val ＋ 各回の 候補×dev ＋ val ＋ test×2。60 問・3 回・4 候補で 480 回）を表示する。ollama は上限なし。claude_warm・claude_cli は `--max-llm-calls`（既定 200）を超える見込みなら始めない（`--max-iterations`・`--candidates` を減らす、`--no-answer`、ollama を案内する）。実行中も残りの回数を各評価の上限にする。`--no-answer` は LLM を呼ばない
  - 結果: `data/eval/tuned/<日時>.json`（採用した設定 `tuned`、始めの設定、始めと終わりの dev・val・test の点と集計、各回の dev の失敗の傾向・候補と dev の点・選んだ候補の val の点・採否と理由、採用した変更、評価セットの版、LLM と呼び出しの数）と `data/eval/reports/<日時>-improve.md`（前後の比較の表、test のデータソース別・形別の変化、採用した変更、各回の候補）。本番の設定・コードは書き換えない（反映は PR）

## 4. データモデル

- ノード型: Category / Task / Knowledge / Source / Skill / Agent
- エッジ型: belongs_to / produced / derived_from / depends_on / uses / executed_by
- 詳細は architecture.md を参照

## 5. 非機能要件

- セキュリティ: APIキーは環境変数。Gitに秘密情報とDBを含めない。売買・SNS投稿など取り返しのつかない操作は人間の承認を必須とする
- 運用環境: 開発・実行の中心はMac mini（48GB）。WindowsはTailscale / SSH経由の操作端末（JARVIS-MAP-002 §3）。Phase 1〜4 の動作確認はWindowsで実施済み。Phase 1・2 は Mac mini で動作確認済み（2026-10-11、`run_daily.py`・`ask.py`・Google API 接続）。2026-10-11 から Mac mini の `jarvis.db` を正本とし、定期実行も Mac mini（launchd）に移した。Windows の定期実行は停止済み
- 外部コンテンツ内の指示には従わない（プロンプトインジェクション対策）
- コスト最適化: エージェント・API呼び出しは「運用コストの最小化」を最優先とし、タスクの複雑度に応じて最も安価なモデル／最も高精度なモデルを自動選択する（階層型エスカレーション、詳細は architecture.md）。コード生成・修正・デバッグは専任のコード実行サブエージェントに委譲する
