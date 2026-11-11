# Jarvis 2 統合メモリ基盤 開発仕様書

**採用案B：CKC内部へのMem0 OSS組込み**

文書番号：J2-MEM-ARCH-001　版：1.0（設計ベースライン案）　作成日：2026年11月12日

ステータス：設計方針は合意済み。既存GitHubソース未監査のため、実装箇所・依存関係・所要工数は要確認。

対象：Jarvis 2 / CKC / Obsidian / Codex・Claude・Ollama連携。本文は開発者へそのまま渡せる要求仕様であり、付録は本会話の重要論点を優先順位順に記録する。

# 0. 要旨・設計の絶対条件

- Jarvis本体から見える入口はCKCの統合Memory API一つとし、Mem0 OSSは内部で交換可能な検索・記憶コンポーネントとする。
- Obsidian（Markdown Vault）を「承認済み長期ナレッジの正本」とする。Mem0を第二の正本にしない。
- 会話原文と全変更イベントは独立したJSONLに保存する。Mem0とObsidianの両方を復元できるようにする。
- CKCの抽出・照合・承認・公開フローを維持する。未承認知識が確定知識に混入することを防ぐ。
- 既存CKCとObsidianノートは削除・全面再構築しない。移行は冪等・可逆・段階的に実施する。
- Codex、Claude、Ollamaのいずれを呼び出しても、同じユーザー／案件の許可された記憶を検索できる。
# 1. 背景と目的

Jarvis 2は複数LLMを呼び分けられる一方、会話を跨いだ自動想起と長期記憶の統合管理が課題。既存CKCは会話から知識を抽出しObsidianへ保存するため、Mem0を独立した第二の知識管理システムとして並設すると重複・矛盾が生まれる。CKCを中核に据え、Mem0 OSSの検索・記憶機能を内包して保守負担を抑える。

# 2. スコープと非スコープ

## 2.1 スコープ

- CKC統合Memory APIおよびMem0アダプタ
- 会話・セッション履歴、個人設定、決定事項、研究ナレッジの検索と保存
- CKC承認・Obsidian公開・Mem0索引同期
- JSON/JSONLのエクスポート、Markdown生成、バックアップ・復元
- 検索品質評価、監査ログ、障害時のフォールバックと段階的移行
## 2.2 非スコープ

- LLMルーターの全面置換、Letta Codeへの全面移行
- Mem0クラウドサービスへのデータ送信（別途承認時のみ）
- CKCに存在しない既存機能が実装済みであるとの断定
- ObsidianノートをLLM判断だけで無条件に上書きする動作
# 3. 論理アーキテクチャ

```
Jarvis UI / Voice / API
         |
  Existing LLM Router ----> Codex / Claude / Ollama
         |
     CKC Memory API  <---- single entry point
         |
  +------+------------------+
  |  Memory Orchestrator     |
  | - recall / remember     |
  | - policy / scope / ACL  |
  | - reconciler / approval |
  +---+---------+-----------+
      |         |
  Mem0 Adapter  CKC Knowledge Workflow
  (retrieval)   (extract/reconcile/approve/publish)
      |         |
  local index   Obsidian Vault (authoritative)
      +----+----+
           |
 Conversation archive / events.jsonl / snapshots.json
```

読み取り：会話要求→CKCが権限と案件を判定→Mem0検索＋必要ならObsidian全文検索→最新性・根拠・承認状態で統合→LLMへ注入。書き込み：原文保存→CKC候補生成→照合→必要な承認→Obsidian確定→Mem0を同期。日常的な会話文脈は別の暫定メモリ領域に登録できる。

# 4. データ管理方針

| データ種別 | 正本／保存先 | 更新条件 |
|---|---|---|
| 会話原文 | JSONL／SQLite（既存採用を確認） | 受信直後、追記専用 |
| 承認済み知識 | Obsidian Markdown＋CKC台帳 | CKC承認後 |
| 未承認候補 | CKCレビューキュー | 抽出後。確定検索から除外 |
| 会話上の暫定記憶 | Mem0 transient scope | 短期保存、有効期限・昇格あり |
| 検索インデックス | Mem0／ローカルベクトルDB | 承認ノートと暫定メモリから再構築可能 |
| 変更イベント | events.jsonl | 全操作で追記、削除はtombstone |
| スナップショット | JSON＋任意MD | 定期・要求時に出力 |

## 4.1 記憶オブジェクト（共通スキーマ）

```
{
  "knowledge_id": "ckc:project:uuid",
  "mem0_id": "optional",
  "project_id": "oarai",
  "scope": "project",
  "type": "decision|finding|hypothesis|preference|task|question",
  "content": "...",
  "status": "candidate|approved|superseded|rejected|deleted",
  "source_ids": ["conversation:uuid"],
  "source_uri": "vault://... or archive://...",
  "confidence": 0.95,
  "valid_from": "2026-11-12T00:00:00+09:00",
  "valid_to": null,
  "created_at": "...", "updated_at": "...",
  "revision": 3,
  "content_hash": "sha256:...",
  "access_policy": "private"
}
```

上記はCKC独自の正規化スキーマであり、Mem0のネイティブレスポンスそのものではない。独立のknowledge_idを維持し、mem0_idは対応表で管理する。Obsidianフロントマターにknowledge_id・revision・status・source_idsを持たせる。

## 4.2 変更・競合ルール

- 同一knowledge_idの更新はrevisionを単調増加。content_hashで同一内容の再登録を抑止。
- 決定事項の変更は旧版をsupersededとして保持し、新版のvalid_fromを確定する。
- CKCが承認したノートはMem0から直接上書き不可。Obsidian手動変更はCKCの差分検知・再索引を経由。
- Mem0が候補事実を抽出してもstatus=approvedには自動昇格しない。低リスクの個人嗜好のみポリシーで自動承認可能。
- 削除要求はObsidian、索引、スナップショット、派生キャッシュ、バックアップ保持方針を統一して処理する。
# 5. CKC統合API仕様

| API | 入力・出力（概要） | 責務 |
|---|---|---|
| remember(messages, context) | conversation_id, project_id → ingest_id | 原文記録、候補抽出をキュー |
| recall(query, context) | user_id, project_id, scope, top_k → MemoryHit[] | 検索・再順位付け・根拠付与 |
| propose(candidate) | 候補→candidate_id | 候補登録、重複照合 |
| approve(candidate_id) | 承認者・版→knowledge_id | 正本書込と索引同期を要求 |
| update(knowledge_id, patch) | revision条件→revision+1 | 競合検査、監査イベント |
| forget(knowledge_id, reason) | tombstone/消去結果 | 全保存先への削除反映 |
| export(format, filters) | json/jsonl/md → ファイル | 差分・全量出力 |
| reindex(project_id) | 対象・dry_run → 検査レポート | 正本から索引再構築 |

contextにはuser_id、session_id、agent_id、project_id、許可スコープ、アクセス権、利用可能トークン数を含める。全APIに認証・入力検証・監査ID・タイムアウト・明確なエラー型を用意する。

# 6. Mem0 OSSアダプタ設計

- Python OSS SDKのMemory/AsyncMemoryをCKC Adapterから呼ぶ。Mem0 APIをJarvisのUIやLLMルーターへ直接露出しない。
- 検索は search(query, filters={"user_id": ...}, top_k=...) を基準とし、案件のproject_id、visibility、statusなどのフィルタは実バージョンのmetadata演算を検証して適用する。
- 一覧 get_all(filters=...)、単件 get、update、delete、history を使用し、OSSにない一括Export APIはCKCで実装する。
- 確定知識はCKCが抽出済みの内容を infer=False によって登録できるか検証し、二重の要約・再抽出を避ける。
- 現行OSSの抽出はADD-onlyが基本で、訂正・削除はCKCの明示的なupdate/deleteと再索引に委ねる。
- 保存先はローカル前提。使用LLM、Embedding、ベクトルDB、Mem0のバージョンを設定ファイルで固定する。
- バージョンアップでAPIや検索順位が変わるため、Adapter契約テストを通過するまで本番へ反映しない。
# 7. 検索とLLMへの記憶注入

1. クエリからproject_id、本人・担当エージェント、重要固有名詞、時間条件を推定する。推定不能時は保守的なスコープに限定する。
1. Mem0の意味・キーワード・エンティティ検索で候補を取得し、承認済みObsidian知識からも補完する。
1. 承認済み > 未承認、現在有効 > 旧版、出典あり > 出典なしで優先順位を調整する。
1. 結果は根拠（knowledge_id、source_uri、更新日、status）付きの短い記憶パックにまとめ、トークン上限内でLLMへ注入する。
1. 検索できない場合は推測で「覚えている」と答えず、不確実性を明示する。Mem0障害時はObsidian全文検索へフォールバック。
プロンプトインジェクション対策：記憶は参照データとして扱い、会話原文・外部資料中の「システム命令」「秘密情報開示」などを命令として実行しない。

# 8. 同期、障害処理、整合性

- 原文→イベント追記を先に確定し、Obsidian書込・Mem0同期は再実行可能なキューで処理する。
- idempotency_key = knowledge_id + revision + operation。重複実行でも結果を一意にする。
- Obsidian更新成功／Mem0失敗の場合、status=sync_pendingとし、バックオフ付き再試行。逆順の更新を避ける。
- 起動時／毎日、Obsidianのknowledge_id・revision・hashとMem0対応表を差分照合し、欠落・余分・旧版を報告。
- Mem0停止時は既存CKC＋Obsidian検索でサービスを継続。書込イベントはキューに残す。
- 全量再索引はdry-run、バックアップ、差分確認後に実行し、破壊的resetを通常運用から隔離する。
# 9. アーカイブ・移行・バックアップ

```
memory/
  archive/conversations/YYYY/MM/*.jsonl
  archive/events/YYYY/MM/*.jsonl
  exports/snapshots/YYYY-MM-DD.json
  exports/markdown/<project>/*.md
  indexes/mem0/                   # disposable / rebuildable
  mappings/knowledge_mem0.jsonl
  config/memory_policy.yaml
Obsidian Vaults (existing): jarvis-knowledge, oarai-obsidian
```

- 既存Obsidian Vaultを読み取り専用で初期スキャンし、frontmatterを検査。IDがないノートには安定IDを付与する移行計画を作成。
- まず承認済みノートだけMem0へ一方向同期し、検索結果を比較。既存ノートの内容は初期移行で書き換えない。
- 会話履歴は原文のままJSONLへ保存。ChatGPTのデータエクスポートを取り込む場合はプライバシー選別と重複排除を行う。
- 日次差分バックアップ、週次全量復元テスト、月次JSON/MDエクスポートを目標とする（頻度は運用負荷に応じ調整）。
- Mem0を停止・削除してもObsidian＋JSONLから再構築できることを移行完了の条件とする。
# 10. セキュリティ・プライバシー

- ローカル優先。APIキー、パスワード、トークン、接続秘密は記憶抽出前にマスキングし、Obsidian・ログに平文保存しない。
- ユーザー／案件／エージェントのアクセス境界をフィルタと認可で二重に適用する。単なるLLM指示に依存しない。
- 承認フローに人間が確認できる差分、出典、削除理由を表示する。
- クラウドLLMへの送信範囲をポリシーで制御し、研究・個人案件を混在させない。
- 機密情報の保存期間、削除の伝播、バックアップからの復旧後再削除を定義する。
# 11. 設定・運用・可観測性

| 項目 | 初期設定（提案） |
|---|---|
| Mem0 | OSS版をバージョン固定。更新はステージングで検証 |
| Embedding | Ollamaローカル候補。品質と速度を比較して決定 |
| Vector DB | Qdrant候補。既存CKC索引がある場合は重複を評価 |
| 初期 top_k | 10（CKC側で設定可能） |
| 検索のタイムアウト | 2秒を目標。超過時フォールバック |
| ログ | latency, recall_hit, sync_lag, extraction_cost, error_rate |
| 費用 | 抽出LLMトークン、検索回数、索引ストレージを計測 |

# 12. 受入基準とテスト

| テスト | 受入条件（暫定） |
|---|---|
| 記憶検索 | 代表100問で重要知識Recall@10 90%以上 |
| 訂正・時系列 | 古い決定と最新版の識別 95%以上 |
| 根拠 | 正式知識の検索結果はsource_uriが100%付与される |
| 混入防止 | 未承認候補が確定知識として提示されない（重大例0） |
| 性能 | ローカル検索追加レイテンシp95 2秒以内を目標 |
| 可逆性 | Vault＋JSONLのみからMem0再構築、件数・hash一致 |
| 耐障害 | Mem0停止時にObsidian検索へ縮退し会話継続 |
| 冪等 | 同じイベント10回再送で重複確定知識0件 |
| 権限 | 別プロジェクト・別ユーザーの非許可記憶漏洩0件 |

数値は開発開始時の暫定合格目標であり、既存CKCのベースライン計測後に調整する。評価セットには大洗、ターボ機械研究、Jarvis開発、ピアノ教室など異なる案件を含むが、機密データは評価環境で適切に匿名化する。

# 13. 実装フェーズと成果物

| Phase | 内容 | 完了条件 |
|---|---|---|
| P0 監査 | GitHub CKC現状・Vault・DB・API棚卸し | 実装済み/未実装の証跡、変更点一覧 |
| P1 接続 | CKC Memory API・Mem0 Adapter・設定 | 単体テスト、既存機能無変更 |
| P2 移行 | Vault読み込み・ID対応・索引生成 | dry-runと全量照合が成功 |
| P3 自動想起 | Recall→LLM注入・障害縮退 | 会話シナリオで再現 |
| P4 品質管理 | 更新・承認・同期・バックアップ | 競合・削除・再構築テスト合格 |
| P5 比較検証 | CKC単独 vs CKC+Mem0 A/B | 評価結果に基づく本番採用可否 |

成果物：アーキテクチャ図、契約API仕様、データスキーマ、設定例、移行スクリプト、テストスイート、運用手順書、ロールバック手順、性能・精度評価レポート。

# 14. 未決事項・実装前確認

- CKCの実際のモジュール構成、利用DB、検索機能、既存Vault書式（GitHub実コード確認が必要）。
- Jarvis 2のランタイム言語・プロセス境界とPythonアダプタの呼び出し方式（直接／FastAPI等）。
- ローカルEmbeddingモデルとQdrantの採否、既存検索エンジンとの共存。
- 暫定記憶（嗜好・セッション要約）を自動承認する範囲とTTL。
- Obsidianノート手動編集時の競合解決ポリシー。
- データ保存期間、機密性区分、削除依頼の扱い。
# 付録A. 本会話からの重要要点（重要度順・決定／根拠／注意）

## P0-01【決定】採用案

CKC内部にMem0 OSSを組み込み、JarvisからはCKCの統合Memory APIのみを呼ぶ「案B」を採用。Mem0を独立した別システムとして並設しない。

## P0-02【決定】第二の脳

Obsidianを第二の脳かつ承認済み長期知識の正本とし、Mem0は交換可能な検索・記憶エンジン。

## P0-03【決定】CKC継承

CKCの抽出・重複照合・矛盾管理・承認・Obsidian公開を維持。CKCの既存ナレッジを廃棄しない。

## P0-04【決定】独立アーカイブ

会話全文JSONL、変更イベントJSONL、定期JSONスナップショットをMem0と独立に残す。Markdownは正本と人間向けビュー。

## P0-05【決定】可逆性

Mem0を廃止してもObsidianとJSONLから復元できる設計。CKC側でJSON/MDをエクスポートできるようにする。

## P0-06【決定】マルチLLM

Codex／Claude／Ollamaなどを切り替えても共通の記憶を使用する。LLM単独のチャット履歴に依存しない。

## P0-07【必須】承認と正本同期

Mem0が抽出した候補とCKCの承認済み知識を区別し、古い決定や未承認候補が正しい知識として混入しないようにする。

## P0-08【必須】二重抽出回避

同じ会話をCKCとMem0で独立抽出して二つの正式知識集合を作らない。CKCの承認結果をMem0へ索引同期する。

## P0-09【必須】履歴・訂正

決定事項の旧版、更新時刻、出典、変更履歴を保持する。Mem0新版のADD-only特性を踏まえCKCで訂正・削除を管理。

## P0-10【必須】ID連携

knowledge_idとmem0_idを対応付け、重複・孤児索引・古い記憶を検出できるようにする。

## P1-11【比較結果】Mem0優先

Mem0は「既存Jarvisへ後付けする記憶部品」として有利。Letta/Letta Codeはより大きなエージェント実行基盤であり、将来比較対象。

## P1-12【比較結果】普及指標

Mem0のGitHub Starや開発者認知度が相対的に大きいとの調査。Starやダウンロードは実アクティブユーザー数ではなく、推移予測には不確実性がある。

## P1-13【比較結果】Lettaの方向転換

Lettaは旧MemGPTのメモリ基盤からLetta Codeなど記憶内蔵型エージェント方向へ重点を移したとの調査。CKCの代替導入には既存資産の移行コストが大きい。

## P1-14【比較結果】Mem0の進歩

Mem0は新アルゴリズムでADD-only抽出、意味検索・BM25・エンティティ照合によるハイブリッド検索を強化。OSS/クラウド機能差を必ず確認する。

## P1-15【注意】ベンチマーク

Mem0が公表する高い記憶ベンチマーク成績をOSS単独の保証値と誤認しない。Jarvis固有タスクで実測する。

## P1-16【判断】CKC単独との比較

CKCの機能を自作拡張する案Aは可能。案Bが優れるかはCKC単独vs CKC+Mem0の比較テストで確定する。

## P1-17【必要機能】自動想起

過去の会話・決定を関連度で検索し、必要最小限の記憶を次回LLMに渡す。不要な全文読み込みを避ける。

## P1-18【必要機能】短期長期分離

直近会話・セッション要約、長期嗜好、決定・研究ナレッジを区別し、保存条件や検索順位を変える。

## P1-19【必要機能】プロジェクト境界

大洗、タービン研究、Jarvis、ピアノ教室など案件別に記憶を整理し混在と誤引用を防ぐ。

## P1-20【必要機能】Obsidian編集

Obsidianで人間が修正したMarkdownも反映。Obsidianを単なるMem0バックアップにしない。

## P1-21【必要機能】差分・整合性

会話→イベント→承認→正本公開→索引同期の段階を監視し、同期失敗は再試行・復旧可能とする。

## P1-22【必要機能】監査・削除

記憶の追加・更新・削除の主体と根拠を記録し、消去が派生索引にも伝播するようにする。

## P2-23【経緯】3ステップ手法

ChatGPTのデータエクスポート、会話結論を5行に要約、Claude CodeのCLAUDE.mdに記憶フォルダ参照ルールを追加する方法が出発点。

## P2-24【注意】CLAUDE.md限界

CLAUDE.mdはClaude Code用であり、独自Jarvis/Ollamaが自動的に読むわけではない。共通Memory APIが必要。

## P2-25【経緯】ChatGPT輸入

ChatGPTエクスポートには過去会話を含むが内部メモリを完全移植できるわけではない。取り込み前に秘密情報と重複を選別する。

## P2-26【設計原則】原文保持

AI要約で落ちた技術的根拠を後から復元できるよう、元の会話を保存する。

## P2-27【設計原則】JSON対MD

JSONは機械可読の正規化情報、Markdownは人間・コードエージェントが読む知識表現。両方の役割を分ける。

## P2-28【設計原則】保存タイミング

後からMem0 get_allで出力可能だが、同期障害や変更履歴喪失を避けるため原文・イベントを同時保存する方針。

## P2-29【運用】ローカル優先

Mac miniのローカルOllamaやEmbeddingを優先し、外部API使用料とデータ持ち出しを抑える。

## P2-30【運用】品質指標

検索再現率、古い決定の区別、根拠付与率、検索時間、重大誤記憶率を継続計測する。

## P2-31【運用】可用性

Mem0不調時はCKC/Obsidian検索に縮退し、ログとキューから回復できるようにする。

## P2-32【運用】更新の安全性

Mem0の破壊的API変更に備え、依存バージョン固定と契約テスト、ロールバックを必須にする。

## P2-33【既存資産】Vault

会話で言及された `jarvis-knowledge` と `oarai-obsidian` を既存Vault候補として引き継ぐ。実際のパス・構成は監査で確認。

## P2-34【既存資産】CKC

会話で言及されたCapture→Extract→Reconcile→Publish、Decision/Preference/Hypothesis/Finding/Task/Question分類を設計継承。実装現況は監査で確認。

## P3-35【未決】実装詳細

GitHub最新版未確認のため、CKCの構成、DB、API、Obsidian索引、Python/JS境界、プロセス配置は実装前調査事項。

## P3-36【未決】ベクトルDB

Qdrantは候補。既存CKCに索引がある場合、二重構築を避けるため比較後に採否決定。

## P3-37【未決】自動承認範囲

嗜好・暫定記憶の自動保存を認める範囲、TTL、研究上の決定を人間承認にする範囲を決める。

## P3-38【将来】技術ウォッチ

Mem0とLetta Codeの開発状況、ユーザー・コミュニティ、API互換、性能を定期的に再評価し、10年単位の記憶資産保全を優先。

# 付録B. 公開仕様の確認先

- Mem0 OSS Python SDK API（Memory、AsyncMemory、search、get_all、history）：https://github.com/mem0ai/mem0/blob/main/skills/mem0/client/python.md
- Mem0 OSS v2→v3移行（ADD-only、hybrid search、breaking changes）：https://github.com/mem0ai/mem0/blob/main/docs/migration/oss-v2-to-v3.mdx
- Mem0 OSS REST API：https://github.com/mem0ai/mem0/blob/main/docs/open-source/features/rest-api.mdx
- Mem0 changelog：https://github.com/mem0ai/mem0/blob/main/docs/changelog/highlights.mdx
- Mem0 GitHub：https://github.com/mem0ai/mem0
- Letta GitHub：https://github.com/letta-ai/letta
参照基準日：2026年11月12日。ライブラリの具体的な引数、挙動、OSS/Platformの差異は採用時点の固定バージョンと実機テストを優先する。

# 付録C. 変更管理

| 版 | 日付 | 変更内容 |
|---|---|---|
| 1.0 | 2026-11-12 | 案Bの合意を開発仕様書に反映。全会話の決定・注意・未決を付録に集約。 |
