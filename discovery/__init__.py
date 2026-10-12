"""Discovery / Architecture Guardian（MAP Phase 3、../docs/architecture.md §11、ADR-0009）。

Registry（第0層）に書かれた設計上の構成と、実際の状態を比べる。収集は読み取りだけで、何も変更しない。
結果は Registry リポジトリの discovered/・generated/ に書く（ADR-0013 の出力先）。
"""
