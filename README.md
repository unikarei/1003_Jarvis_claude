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

## 質問応答（Phase 2）

```powershell
python ask.py "先週のAIニュースで重要だったのは？"
```

蓄積したKnowledgeをSQLiteで検索し、出典URL付きで回答する。引数を省略すると対話入力になる。

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

現在はWindows開発機で実機マイク/スピーカーの動作検証まで完了している。Mac miniへの移行は本番運用フェーズで行う。

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
