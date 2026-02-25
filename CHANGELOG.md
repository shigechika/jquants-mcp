# CHANGELOG

運用に影響する変更・外部要因による対応を記録する。
個別のバグ修正やコード整理は含まない。

---

## 2026-02-25
### CSV 差分インポート（`--incremental`）
- `scripts/import_csv_to_cache.py` に `--incremental` フラグを追加
- 通常日: キャッシュ最新日より新しい行だけ INSERT（~4,000行/日、全件530万行に対し大幅高速化）
- 株式分割・併合: 新規行の `AdjFactor != 1.0` を検知し、該当コードの全行を DELETE → CSV から再 INSERT（調整済み値の全期間更新に対応）
- 銘柄マスタ（`--tickers`）は少量（~4,000行）のため常に全件インポート
- テスト10件追加（通常差分・分割検知・併合検知・複数銘柄同日分割・非分割銘柄の影響なし確認）

### 初回リリース v0.1.0
- FastMCP ベースの MCP サーバー（25ツール）
- J-Quants API v2 の全エンドポイントをラップ（株価・決算・指数・デリバティブ・信用取引・空売り比率）
- 2層 SQLite キャッシュ（行レベル + レスポンスレベル、TTL付き）
- プラン別レートリミッタ（Free=5/分〜Premium=500/分）
- 指数バックオフリトライ + 自動ページネーション
- `~/.jquants-api/jquants-api.toml` から API キー自動検出
- `scripts/bulk_fetch_all.py`: Bulk API 一括ダウンロード
- `scripts/import_csv_to_cache.py`: ローカル CSV からのキャッシュインポート
- GitHub Actions CI（ruff lint/format + pytest on Python 3.10〜3.13）
