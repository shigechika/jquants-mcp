# jquants-dat-mcp

[J-Quants API v2](https://jpx-jquants.com/) を使って日本株市場データを取得する [MCP (Model Context Protocol)](https://modelcontextprotocol.io/) サーバーです。

[j-quants-doc-mcp](https://github.com/knishioka/j-quants-doc-mcp)（ドキュメント MCP）の対になるサーバーです。doc が API の仕様を説明するのに対し、dat は実際に **API を呼び出してデータを取得** します。

## 特徴

- **25 の MCP ツール** — J-Quants API v2 の全エンドポイントをカバー
- **2層 SQLite キャッシュ** — 時系列データは行レベル、その他はレスポンスレベル（TTL付き）
- **株式分割検知** — AdjFactor 変化時にキャッシュを自動無効化
- **レート制限** — プラン別スライディングウィンドウ（Free: 5回/分, Light: 60, Standard: 120, Premium: 500）
- **リトライ** — 429/5xx エラーに対する指数バックオフ
- **ページネーション** — 複数ページの透過的取得
- **プラン対応** — 全ツールを登録し、プラン制限時はわかりやすいエラーメッセージを返却

## 必要要件

- Python 3.10+
- [J-Quants API キー](https://jpx-jquants.com/)（Free プラン以上）

## インストール

```bash
# uv（推奨）
uv pip install jquants-dat-mcp

# pip
pip install jquants-dat-mcp
```

### ソースから

```bash
git clone https://github.com/shigechika/jquants-dat-mcp.git
cd jquants-dat-mcp
uv sync --dev
```

## 設定

設定は以下の優先順位で読み込まれます（後勝ち）:

1. `~/.jquants-api/jquants-api.toml` — API キーのみ（J-Quants 公式設定）
2. `~/.config/jquants-dat-mcp/config.ini`（ユーザーグローバル）
3. `./config.ini`（カレントディレクトリ）
4. 環境変数（MCP クライアントや CLI から）

### API キー（ゼロコンフィグ）

[jquants-api-client](https://github.com/J-Quants/jquants-api-client-python) を既に利用している場合、`~/.jquants-api/jquants-api.toml` から API キーが自動読み込みされます。追加の設定は不要です。

### config.ini

MCP 固有の設定（プラン、キャッシュ、クライアント動作）:

```ini
[jquants]
plan = premium
# cache_dir = ~/.cache/jquants-dat-mcp
# base_url = https://api.jquants.com/v2

[client]
# max_retries = 5
# retry_base_delay = 1.0
# max_pages = 10
```

### 環境変数

| 変数名 | 必須 | デフォルト | 説明 |
|---|---|---|---|
| `JQUANTS_API_KEY` | いいえ* | — | J-Quants API キー |
| `JQUANTS_PLAN` | いいえ | `free` | プラン: `free` / `light` / `standard` / `premium` |
| `JQUANTS_CACHE_DIR` | いいえ | `~/.cache/jquants-dat-mcp` | キャッシュディレクトリのパス |
| `JQUANTS_BASE_URL` | いいえ | `https://api.jquants.com/v2` | API ベース URL |
| `MAX_RETRIES` | いいえ | `5` | リクエスト失敗時の最大リトライ回数 |
| `RETRY_BASE_DELAY` | いいえ | `1.0` | 指数バックオフの基本遅延（秒） |
| `MAX_PAGES` | いいえ | `10` | ページネーション時の最大ページ数 |

\* API キーは `~/.jquants-api/jquants-api.toml` から自動検出されます。上書きが必要な場合のみ `JQUANTS_API_KEY` を設定してください。

環境変数は `config.ini` と `jquants-api.toml` の両方を上書きします。普段使いの設定は `config.ini` や `jquants-api.toml` に任せ、MCP クライアント（Claude Desktop, Claude Code）からは `env` ブロックで必要な設定だけ渡すことができます。

## 使い方

### Claude Code

`claude mcp add` で MCP サーバーを登録:

```bash
claude mcp add jquants-dat-mcp \
  -e JQUANTS_PLAN=premium \
  -- jquants-dat-mcp
```

ソースからインストールした場合:

```bash
claude mcp add jquants-dat-mcp \
  -e JQUANTS_PLAN=premium \
  -- /path/to/jquants-dat-mcp/.venv/bin/jquants-dat-mcp
```

`--scope`（`-s`）オプションで設定の保存先を指定できます:

| スコープ | 説明 | 設定ファイル |
|---|---|---|
| `local`（デフォルト） | 現在のプロジェクト、現ユーザーのみ | `.claude.json` |
| `project` | 現在のプロジェクト、チーム共有 | プロジェクトルートの `.mcp.json` |
| `user` | 全プロジェクト、現ユーザーのみ | `~/.claude.json` |

API キーは `~/.jquants-api/jquants-api.toml` から自動検出されます。上書きが必要な場合のみ `-e JQUANTS_API_KEY=...` を指定してください。

### Claude Desktop

Claude Desktop の設定ファイルに追加:

| OS | 設定ファイル |
|---|---|
| macOS | `~/Library/Application Support/Claude/claude_desktop_config.json` |
| Windows | `%APPDATA%\Claude\claude_desktop_config.json` |
| Linux | `~/.config/Claude/claude_desktop_config.json` |

```json
{
  "mcpServers": {
    "jquants-dat-mcp": {
      "command": "/path/to/jquants-dat-mcp/.venv/bin/jquants-dat-mcp",
      "env": {
        "JQUANTS_PLAN": "premium"
      }
    }
  }
}
```

> **注意:** Claude Desktop は限定的な `PATH`（`/usr/local/bin`, `/usr/bin` 等）しか持たないため、実行ファイルはフルパスで指定してください。

設定後、Claude Desktop を再起動してください。

### スタンドアロン

```bash
jquants-dat-mcp
```

## 提供ツール一覧

### 株式 (Equities) — 6ツール

| ツール名 | エンドポイント | 対応プラン | 説明 |
|---|---|---|---|
| `get_equities_master` | `/equities/master` | Free+ | 上場銘柄一覧 |
| `get_equities_bars_daily` | `/equities/bars/daily` | Free+ | 株価四本値（日足） |
| `get_equities_bars_minute` | `/equities/bars/minute` | Light+ | 株価分足 |
| `get_equities_bars_daily_am` | `/equities/bars/daily/am` | Premium | 前場四本値 |
| `get_equities_investor_types` | `/equities/investor-types` | Light+ | 投資部門別売買 |
| `get_equities_earnings_calendar` | `/equities/earnings-calendar` | Free+ | 決算発表予定 |

### 財務 (Financials) — 3ツール

| ツール名 | エンドポイント | 対応プラン | 説明 |
|---|---|---|---|
| `get_fins_summary` | `/fins/summary` | Free+ | 財務情報（四半期決算） |
| `get_fins_details` | `/fins/details` | Premium | 財務諸表詳細（BS/PL/CF） |
| `get_fins_dividend` | `/fins/dividend` | Premium | 配当金データ |

### 指数 (Indices) — 2ツール

| ツール名 | エンドポイント | 対応プラン | 説明 |
|---|---|---|---|
| `get_indices_bars_daily` | `/indices/bars/daily` | Free+ | 指数四本値 |
| `get_indices_bars_daily_topix` | `/indices/bars/daily/topix` | Free+ | TOPIX 四本値 |

### デリバティブ (Derivatives) — 3ツール

| ツール名 | エンドポイント | 対応プラン | 説明 |
|---|---|---|---|
| `get_derivatives_bars_daily_futures` | `/derivatives/bars/daily/futures` | Light+ | 先物四本値 |
| `get_derivatives_bars_daily_options` | `/derivatives/bars/daily/options` | Light+ | オプション四本値 |
| `get_derivatives_bars_daily_options_225` | `/derivatives/bars/daily/options/225` | Light+ | 日経225オプション四本値 |

### マーケット (Markets) — 6ツール

| ツール名 | エンドポイント | 対応プラン | 説明 |
|---|---|---|---|
| `get_markets_margin_interest` | `/markets/margin-interest` | Standard+ | 信用取引残高 |
| `get_markets_margin_alert` | `/markets/margin-alert` | Standard+ | 増担保規制情報 |
| `get_markets_short_ratio` | `/markets/short-ratio` | Standard+ | 業種別空売り比率 |
| `get_markets_short_sale_report` | `/markets/short-sale-report` | Standard+ | 空売り残高報告 |
| `get_markets_breakdown` | `/markets/breakdown` | Premium | 売買内訳データ |
| `get_markets_calendar` | `/markets/calendar` | Free+ | 取引カレンダー |

### バルクダウンロード (Bulk) — 2ツール

| ツール名 | エンドポイント | 対応プラン | 説明 |
|---|---|---|---|
| `get_bulk_list` | `/bulk/list` | Light+ | ダウンロード可能ファイル一覧 |
| `get_bulk_download_url` | `/bulk/get` | Light+ | 署名付きダウンロード URL 取得 |

### ユーティリティ — 3ツール

| ツール名 | 説明 |
|---|---|
| `health_check` | サーバー稼働状態・API キー設定確認 |
| `cache_status` | キャッシュ統計情報 |
| `cache_clear` | キャッシュクリア |

## キャッシュ

2層構造の SQLite キャッシュを使用しています:

- **Tier 1（行レベル）**: 時系列データを日付×コードで管理。増分取得・株式分割検知（AdjFactor 比較）に対応。
  - `equities_bars_daily`, `equities_master`, `fins_summary`, `indices_bars_daily_topix`, `investor_types`
- **Tier 2（レスポンスレベル）**: API レスポンス全体を TTL 付きでキャッシュ（6h / 24h / 7d）。

キャッシュの保存先はデフォルトで `~/.cache/jquants-dat-mcp/cache.db` です。

### バルクデータ一括取得

`scripts/bulk_fetch_all.py` は J-Quants Bulk API から CSV データを一括ダウンロードし、SQLite キャッシュにインポートするスクリプトです。過去データを効率的にローカルキャッシュに蓄積できます。

```bash
# Light プランの全データを取得（fins_summary, investor_types, topix, equities_master）
uv run python scripts/bulk_fetch_all.py

# 特定のエンドポイントのみ取得
uv run python scripts/bulk_fetch_all.py --endpoints fins_summary topix

# ドライラン — ファイル一覧とサイズのみ表示
uv run python scripts/bulk_fetch_all.py --dry-run
```

プラン別のレート制限（例: Light は 60 req/min）を遵守し、429 エラー時は自動リトライします。

### CSV インポート

`scripts/import_csv_to_cache.py` はローカルの CSV ファイルをキャッシュにインポートします。API を叩かずに他のパイプラインからデータをサイドロードできます。

```bash
# 全件インポート（初回セットアップ）
uv run python scripts/import_csv_to_cache.py \
    --market-history /path/to/jpx-market-history.csv \
    --tickers /path/to/jpx-tickers.csv

# 差分インポート（日次運用）
uv run python scripts/import_csv_to_cache.py \
    --market-history /path/to/jpx-market-history.csv \
    --tickers /path/to/jpx-tickers.csv \
    --incremental
```

`--incremental` を指定すると、キャッシュ最新日より新しい行だけをインポートします（530万行超の全件ではなく ~4,000行/日）。株式分割・併合は `AdjFactor != 1.0` で自動検知し、該当銘柄の全期間データを再インポートして調整済み値を更新します。

## 開発

```bash
# 開発用依存パッケージのインストール
uv sync --dev

# テスト実行
uv run pytest -v

# リント
uv run ruff check src/ tests/

# フォーマット
uv run ruff format src/ tests/
```

## ライセンス

[MIT](LICENSE)
