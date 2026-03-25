# CHANGELOG

運用に影響する変更・外部要因による対応を記録する。
個別のバグ修正やコード整理は含まない。

---

## 2026-03-26

### `/settings` Web UI 改善
- 「リフレッシュトークン」ラベルを削除し、J-Quants API キーダッシュボードへのリンクを追加
- Google Sign-In（GSI）によるブラウザ認証を追加（`OAUTH_PROVIDER=google` 時に `/settings` でワンクリック認証が可能）

### `cache_status` / `health_check` プラン修正（Issue #17）
- 認証ユーザーの実プランではなく常に `"free"` を返していたバグを修正
- `health_check`・`cache_status` の両ツールで認証ユーザーの実プランを返すように統一

### ツール docstring の英語統一
- 全ツールの description を英語に統一（MCP クライアントとの互換性向上）

### `get_equities_bars_daily` パラメータ検証強化
- `code` または `date` のどちらかが必須になるようバリデーションを追加（両方省略時はエラーを返す）

---

## 2026-03-12
### Markets ツール Tier 1 キャッシュ移行
- Markets 系 5 ツール（`margin_interest`, `margin_alert`, `short_ratio`, `breakdown`, `calendar`）を Tier 2（TTL 付きレスポンスキャッシュ）から Tier 1（行レベル永続キャッシュ）に移行
  - `short_sale_report` は 1 つの code+date に複数レポーターが存在するため Tier 2 のまま
- `store.py`: 5 テーブル（`markets_margin_interest`, `markets_margin_alert`, `markets_short_ratio`, `markets_breakdown`, `markets_calendar`）を Tier 1 テーブル定義に追加
- `markets.py`: 汎用ヘルパー `_get_with_tier1_cache()` を追加（`date_field` パラメータで `PubDate` 等の非標準日付カラムに対応）

### `scripts/bulk_fetch_all.py` Markets 対応
- 6 エンドポイント追加: `margin_interest`, `margin_alert`, `short_ratio`, `short_sale_report`, `breakdown`, `calendar`
  - `calendar` は Bulk API 非対応（400 エラー）のため `daily_fetch.py --calendar` で取得

### `scripts/daily_fetch.py` 拡張
- Markets 系データを Tier 1 テーブルに直接投入するように変更（`_store_tier1()`, `_fetch_markets_tier1()` ヘルパー追加）
- `--calendar` オプション追加（取引カレンダー取得、Free プラン以上）
- `--backfill DAYS` オプション追加（Markets 系 Tier 1 対象の過去データバックフィル）

## 2026-02-25
### 日次データ取得スクリプト（`daily_fetch.py`）
- `scripts/daily_fetch.py` を追加 — jquantsapi.ClientV2 で追加データを取得し SQLite キャッシュに直接投入
- `~/.config/jquants-dat-mcp/config.ini` の `plan` 設定（または `JQUANTS_PLAN` 環境変数）に応じて取得対象を自動決定
  - Free: `fins_summary`（決算サマリー）、`earnings_cal`（決算発表予定）
  - Light: `topix`（TOPIX 日足）、`investor_types`（投資部門別売買動向）
  - Standard: `short_ratio`（業種別空売り比率）、`margin_interest`（信用取引残高）、`margin_alert`（増担保規制情報）、`short_sale_report`（空売り残高報告）
  - Premium: `breakdown`（売買内訳）
- Tier 1（行レベル差分投入）と Tier 2（レスポンスレベル TTL 付き）の両方に対応
- 403 等の権限エラーは graceful にスキップ（パイプラインを止めない）
- `--endpoint名` フラグで個別指定も可能（プラン外指定時は警告してスキップ）

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
