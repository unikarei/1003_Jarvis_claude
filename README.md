# JARVIS

個人用AIアシスタント。Phase 1 MVPは「AIニュース日次収集・要約」（RSS取得 → Claude APIで要約 → Markdownレポート生成）。

仕様・設計・タスクの詳細は `docs/spec.md` / `docs/architecture.md` / `docs/task.md`、プロジェクト全体の方針は `CLAUDE.md` を参照。

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

`fetch_news.py`（RSS取得） → `summarize.py`（Claude APIで要約） → `generate_report.py`（レポート生成）を順に実行する。初回実行時に `jarvis.db`（SQLite）が `schema.sql` から自動初期化される。レポートは `reports/` に日時付きファイル名で出力される。

各ステップは個別にも実行できる（`python fetch_news.py` など）。

## 定期実行（タスクスケジューラ）

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

## RSSフィードの追加

`config.py` の `RSS_FEEDS` にエントリを追記する。
