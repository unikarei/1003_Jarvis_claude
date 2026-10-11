# JARVIS

個人用AIアシスタント。Phase 1 MVPは「AIニュース日次収集・要約」（RSS取得 → Claude APIで要約 → Markdownレポート生成）。

仕様・設計・タスクの詳細は `docs/spec.md` / `docs/architecture.md` / `docs/task.md`、プロジェクト全体の方針は `AGENTS.md` を参照。

## JARVISと対話する

```powershell
python chat.py
```

継続的な対話型CLI。入力内容に応じてナレッジ検索（`ask.py`と同じ検索対象: ニュース・民泊ノート・大洗Project OS等すべてのcategory）またはコード実行サブエージェントに自動で振り分けられる（`run_task.py`と同じ分類ロジック）。終了は `exit` / `quit` / Ctrl+C。

## セットアップ（Windows）

```powershell
pip install -r requirements.txt
```

`.env.example` を `.env` にコピーし、`ANTHROPIC_API_KEY` を設定する（`OPENAI_API_KEY` はPhase 4以降のコード実行サブエージェント導入時まで未設定でよい）。

```powershell
copy .env.example .env
```

## 実行

```powershell
python run_daily.py
```

`fetch_news.py`（RSS取得） → `summarize.py`（Claude APIで要約） → `generate_report.py`（レポート生成）を順に実行する。初回実行時に `jarvis.db`（SQLite）が `schema.sql` から自動初期化される。

対象カテゴリ: news（AIニュース）/ trading（仮想取引）/ realestate（不動産）/ manufacturing（製造業）/ minpaku（民泊）/ piano（ピアノ教室アクセス分析）。レポートはカテゴリごとに別ファイルとして `reports/{category}_{日時}.md` に出力される。

各ステップは個別にも実行できる（`python fetch_news.py` など）。`python generate_report.py <category>` でカテゴリを指定したレポート単体生成も可能（省略時は全カテゴリ）。

## ①ピアノ教室アクセス分析（Search Console / GA4）

Google Search Console・GA4のサービスアカウント連携が必要（`.env`に`GOOGLE_APPLICATION_CREDENTIALS`/`SEARCH_CONSOLE_SITE_URL`/`GA4_PROPERTY_ID`を設定）。接続確認:

```powershell
python scripts/test_google_integrations.py
```

分析の単体実行:

```powershell
python piano_analytics.py
```

直近28日（Search Console）・直近7日（GA4）のデータを前期間と比較し、検索クエリの分類（表示回数多×CTR低、順位4〜15位、順位/クリック数の増減、地域・ブランドクエリ）、問い合わせ系イベント件数、Claudeによる優先度順Top3の改善提案を含むレポートをKnowledgeとして保存する。Search Console・GA4のどちらかが取得失敗しても、取得できた方のデータのみで処理を継続する。`run_daily.py`からも自動実行される（失敗しても全体は止めない）。

定期情報収集は、対応するレポート種別については隣接プロジェクト`../1004_Insight_Reporter`が上位ソースとなる（`run_daily.py`は`piano_analytics.run_piano_update()`を呼ぶ）。Insight Reporterの`reports/piano_growth/latest.json`が存在し十分（生成から10日以内・必須フィールドあり）なら、それをSource/Knowledge化して使う。無ければJARVIS独自のSearch Console/GA4取得にフォールバックする。JARVISはInsight Reporter配下のファイルを変更・importしない（読み取り専用）。

## 質問応答（Phase 2）

```powershell
python ask.py "先週のAIニュースで重要だったのは？"
```

蓄積したKnowledgeをSQLiteで検索し、出典URL付きで回答する。引数を省略すると対話入力になる。検索はjanomeで質問文から名詞を抽出してLIKE検索するため、全履歴（ニュースだけでなく後述の民泊ノート等）が対象になる。

## 民泊ノートの取り込み（Obsidianボルト連携）

```powershell
python ingest_obsidian.py
```

GitHub上の個人Obsidianボルト（`unikarei/oarai-obsidian`、private）から「民泊@大洗」ノートを取得し、category=minpakuのSource/Knowledgeとして保存する（要約せず全文保存）。privateリポジトリのため`gh auth login`でのGitHub CLI認証が必要（`gh auth token`で取得したトークンを使う）。再実行時は、新規ノートは追加、既存ノートは内容が変わっていればcontentを更新する（重複作成はしない）。取り込み後は `python ask.py "大洗の物件の所在地は？"` のように質問できる。

ボルトへの書き込み（手動実行のみ、自動では書き込まない）:

```powershell
python update_obsidian.py create "民泊@大洗/02_購入調査/大洗_新規メモ.md" "本文"
python update_obsidian.py append "民泊@大洗/01_物件/大洗_物件概要.md" "追記内容"
```

`create`は既存ファイルと衝突するとエラーになる。`append`は末尾に日付とJARVIS追記である旨を付けて追記する。取得後に他端末等で編集されていた場合はGitHubのsha不一致により409エラーで中断する（上書き事故防止）。書き込み後は`ingest_obsidian.py`を再実行してローカルのKnowledgeに反映する。

## 民泊: 大洗Project OS連携（oarai-project-os）

大洗物件の購入判断〜開業準備を管理する別リポジトリ（`unikarei/oarai-project-os-since2026`、private、独自のSDD統治`AGENTS.md`を持つ）との連携。

```powershell
python ingest_oarai_project_os.py
```

`data/*.json`（タスク・決定事項・リスク等の構造化データ）・`docs/**/*.md`を取得し、category=minpakuのSource/Knowledgeとして保存する（要約せず保存）。`ask.py`で大洗Project OSのタスク・リスク・決定事項について質問できる。

対話からの改造提案（Python関数として利用。CLIは未提供）:

```python
from oarai_project_os import propose_change, submit_or_hold

envelope = propose_change(
    summary="南側庭にサウナ設置候補地あり、分電盤の容量確認が必要",
    entity_type="task",  # activity/evidence/task/decision_candidate/risk/topic_update/note
    payload={"priority": "B"},
)
submit_or_hold(envelope)
```

本番API（`https://oarai.rebirth-lab.site/health`、2026-10-09時点で停止中）が疎通可能なら`POST /api/ingest`でステージングのみ行う（正本の`data/*.json`は直接書き換えない）。疎通不可なら提案をJARVISのDBにTaskノード（「未送信」）として保持し、`resubmit_pending_proposals()`で後から送信し直せる。`decision_candidate`は常にユーザー承認必須として送信される。

## 音声対話（Phase 3）

音声合成には[VOICEVOX ENGINE](https://voicevox.hiroshiba.jp/)が必要（本リポジトリには含まない）。Windows CPU版のセットアップ:

```powershell
# https://github.com/VOICEVOX/voicevox_engine/releases から
# voicevox_engine-windows-cpu-<version>.vvpp をダウンロード（.vvppはzip形式）
Expand-Archive voicevox_engine-windows-cpu-<version>.vvpp -DestinationPath voicevox_engine
voicevox_engine\run.exe
```

ENGINEが `http://127.0.0.1:50021` で起動していることを確認してから、以下を実行する。

```powershell
# ファイルベース（自動テスト・デバッグ用）
python talk.py <入力音声ファイル.wav> <出力音声ファイル.wav>

# 実機マイク/スピーカー（Windows開発機での検証用）
python live_talk.py 8  # 8秒間録音し、回答をスピーカーで再生
```

いずれも 音声/テキスト → `speech_to_text.py`（faster-whisper） → `ask.answer()` → `text_to_speech.py`（VOICEVOX） → 音声、を接続する。ENGINE未起動の場合は明確なエラーで停止する。`speech_to_text.py` / `text_to_speech.py` は単体でも実行できる。

現在はWindows開発機で実機マイク/スピーカーの動作検証まで完了している。Mac mini での音声対話の確認は未実施。

## セットアップ（Mac mini）

2026-10-11 から Mac mini を正本の実行環境としている。

```sh
cd 1003_Jarvis_claude
uv venv .venv --python 3.13
uv pip install --python .venv/bin/python -r requirements.txt
cp .env.example .env   # 値を設定する。Google の鍵ファイルは jarvis-piano-reader-key.json として配置
```

python.org 版の Python は HTTPS の証明書検証に使う CA ファイルを持たないため、`.env` に venv 内の certifi を指定する（指定しないと、feedparser が証明書エラーを例外にせず、RSS 取得がエラーなしで 0 件になる）。

```sh
echo "SSL_CERT_FILE=$(.venv/bin/python -c 'import certifi;print(certifi.where())')" >> .env
```

`.venv/` は `.gitignore` に含まれていないため、`.git/info/exclude` に追加してローカルで除外している。

## 定期実行（launchd、Mac mini）

`~/Library/LaunchAgents/com.ohide.jarvis.daily-news.plist` で毎日 6:00 に `.venv/bin/python run_daily.py` を実行する。ログは `~/Library/Logs/jarvis/daily-news.log`（標準エラーは `daily-news.err.log`）。

```sh
launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/com.ohide.jarvis.daily-news.plist   # 登録
launchctl kickstart gui/$(id -u)/com.ohide.jarvis.daily-news                                # 今すぐ1回実行
launchctl print gui/$(id -u)/com.ohide.jarvis.daily-news | grep -E "state|last exit"        # 状態確認
launchctl bootout gui/$(id -u)/com.ohide.jarvis.daily-news                                  # 登録解除
```

## 定期実行（タスクスケジューラ、Windows。2026-10-11 に停止済み）

`config.py` はすべてのパスを `__file__` 基準で解決するため、作業ディレクトリに依存しない。PowerShellで以下を実行してタスクを登録する（`python.exe`のパスは環境に合わせて変更）。

```powershell
$pythonExe = "C:\Users\<user>\AppData\Local\Programs\Python\Python312\python.exe"
$script = "D:\usr8_work\work_23_chatgpt\16_PoCs\1003_Jarvis_claude\run_daily.py"
$tr = "`"$pythonExe`" `"$script`""
schtasks /Create /TN "JARVIS_DailyNews" /TR $tr /SC DAILY /ST 06:00 /F
```

確認・削除:

```powershell
schtasks /Query /TN "JARVIS_DailyNews" /V /FO LIST
schtasks /Delete /TN "JARVIS_DailyNews" /F
```

## タスク実行・スキル蓄積（Phase 4）

```powershell
python run_task.py "Pythonで文字列を逆順にする関数を実装して"   # コード関連 → code_agent.py（OpenAI）に委譲
python run_task.py "先週のAIニュースで重要だったのは？"          # ナレッジ関連 → ask.answer()
```

タスクの説明文に`コード`/`実装`/`関数`/`バグ`/`デバッグ`/`スクリプト`/`プログラム`/`リファクタ`のいずれかが含まれるとコードタスクと判定し、`code_agent.py`（`OPENAI_API_KEY`が必要）に委譲する。それ以外はナレッジ質問応答として`ask.answer()`を使う。実行結果はTaskノードとして保存され、同種のタスクで使った方法はSkillノードとして再利用される（Task→uses→Skill、Task→executed_by→Agent）。

エージェントの判断履歴（階層型エスカレーション・コード委譲）は `jarvis_agent.log` に記録される（`.gitignore`で除外）。

## RSSフィードの追加

`config.py` の `RSS_FEEDS` にエントリを追記する。
