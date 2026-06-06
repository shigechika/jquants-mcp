# Cloud Run デプロイガイド（GCP）

jquants-mcp を Google Cloud Run にデプロイし、OAuth 2.1 ログイン・ユーザーごとの暗号化 J-Quants API キー・Claude Desktop / Claude モバイル対応を実現します。

Cloud Run のマルチユーザーデプロイは構成要素が多いため手順が長くなります。DNS / TLS の反映待ちを含め、初回は 2〜4 時間を見込んでください。

## アーキテクチャ

Cloud Run が HTTPS サーバーを担い、ステートは以下のマネージドストアに分散します:

- **`cache.db`**（市場データ）— セルフホスト publisher が GCS バケットに公開し、Cloud Run のコールドスタート時に `/tmp` へダウンロード。Cloud Run は読み取り専用。
- **`users`**（ユーザーごとの暗号化 J-Quants API キー）— Firestore `users` コレクション。
- **`oauth_state`**（OAuth セッション・PKCE・動的クライアント登録）— Firestore `oauth_state` コレクション。
- **Secrets**（OAuth クライアントシークレット・暗号化キー・allowlist）— Google Secret Manager。

## 想定コスト

1 日 1,000 リクエスト未満の場合:

| サービス | コスト |
|---|---|
| Cloud Run | $0（無料枠で個人利用はほぼカバー） |
| Firestore | $0（無料枠: 50k reads + 20k writes/日） |
| GCS | ~$0.07/月（3 GiB、us-west1） |
| Secret Manager | ~$0.30/月（6 secrets × $0.06） |
| Cloud DNS | $0.20/月 per hosted zone（カスタムドメイン使用時） |
| **合計** | **個人・家族利用なら < $1/月** |

## 前提条件

- 課金有効な Google Cloud アカウント
- GCP プロジェクト（以下の手順で新規作成可）
- [gcloud CLI](https://cloud.google.com/sdk/docs/install) のローカルインストール
- GitHub アカウント（リポジトリを fork して CD ワークフローを実行）
- J-Quants API キー（Free プラン以上）
- オプション: カスタムドメイン（例: `jquants-mcp.example.com`）

## 1. Fork とクローン

GitHub で [shigechika/jquants-mcp](https://github.com/shigechika/jquants-mcp) を fork し:

```bash
git clone git@github.com:YOUR_USERNAME/jquants-mcp.git
cd jquants-mcp
```

## 2. 環境変数の設定

以降の手順で使用するシェル変数を設定します。

```bash
export PROJECT_ID="jquants-mcp-$(whoami)"   # 任意のユニークな ID
export REGION="us-west1"                      # Cloud Run のリージョン
export SERVICE="jquants-mcp"
export GCS_BUCKET="${PROJECT_ID}-cache"       # グローバルにユニークである必要あり
export SA_NAME="jquants-mcp"
export SA="${SA_NAME}@${PROJECT_ID}.iam.gserviceaccount.com"
export GITHUB_REPO="YOUR_USERNAME/jquants-mcp"
```

## 3. GCP プロジェクトの作成・設定

```bash
gcloud projects create "${PROJECT_ID}"
gcloud config set project "${PROJECT_ID}"

# 課金アカウントのリンク（課金アカウント ID に置き換え）
gcloud billing accounts list
gcloud billing projects link "${PROJECT_ID}" \
  --billing-account=<BILLING_ACCOUNT_ID>

# 必要な API を有効化
gcloud services enable \
  run.googleapis.com \
  cloudbuild.googleapis.com \
  artifactregistry.googleapis.com \
  secretmanager.googleapis.com \
  firestore.googleapis.com \
  storage.googleapis.com \
  iamcredentials.googleapis.com \
  sts.googleapis.com
```

カスタムドメインを使う場合:

```bash
gcloud services enable dns.googleapis.com
```

## 4. サービスアカウントの作成

```bash
gcloud iam service-accounts create "${SA_NAME}" \
  --display-name "jquants-mcp Cloud Run SA"

# Firestore 読み書き
gcloud projects add-iam-policy-binding "${PROJECT_ID}" \
  --member "serviceAccount:${SA}" \
  --role "roles/datastore.user"

# Secret Manager アクセス
gcloud projects add-iam-policy-binding "${PROJECT_ID}" \
  --member "serviceAccount:${SA}" \
  --role "roles/secretmanager.secretAccessor"
```

## 5. GCS バケットの作成

```bash
gcloud storage buckets create "gs://${GCS_BUCKET}" \
  --location "${REGION}" \
  --uniform-bucket-level-access

gcloud storage buckets add-iam-policy-binding "gs://${GCS_BUCKET}" \
  --member "serviceAccount:${SA}" \
  --role "roles/storage.objectViewer"
```

publisher ホストで並行アップロードを無効化（SQLite ファイルが壊れるため）:

```bash
gcloud config set storage/parallel_composite_upload_enabled False
```

## 6. Firestore の有効化

```bash
gcloud firestore databases create \
  --location="${REGION}" \
  --type=firestore-native
```

スキーマ設定は不要です。サーバーが初回書き込み時に `users` / `oauth_state` コレクションを自動作成します。

## 7. Workload Identity Federation（WIF）の設定

WIF により GitHub Actions が長期的なサービスアカウントキーなしで GCP に認証できます。

```bash
# Workload Identity Pool を作成
gcloud iam workload-identity-pools create github-actions \
  --location=global \
  --display-name="GitHub Actions"

# Provider を作成（fork 先のリポジトリに限定）
gcloud iam workload-identity-pools providers create-oidc github \
  --location=global \
  --workload-identity-pool=github-actions \
  --display-name="GitHub" \
  --attribute-mapping="google.subject=assertion.sub,attribute.actor=assertion.actor,attribute.repository=assertion.repository" \
  --attribute-condition="assertion.repository=='${GITHUB_REPO}'" \
  --issuer-uri="https://token.actions.githubusercontent.com"

# Provider のリソース名を取得（後で GitHub Secret に使用）
PROJECT_NUMBER=$(gcloud projects describe "${PROJECT_ID}" --format="value(projectNumber)")
export WIF_PROVIDER="projects/${PROJECT_NUMBER}/locations/global/workloadIdentityPools/github-actions/providers/github"
echo "WIF_PROVIDER=${WIF_PROVIDER}"

# GitHub Actions がサービスアカウントを借用できるように設定
gcloud iam service-accounts add-iam-policy-binding "${SA}" \
  --role="roles/iam.workloadIdentityUser" \
  --member="principalSet://iam.googleapis.com/projects/${PROJECT_NUMBER}/locations/global/workloadIdentityPools/github-actions/attribute.repository/${GITHUB_REPO}"
```

Provider の `attribute-condition` はセキュリティ境界です。リポジトリを rename / 移管した場合はこの条件を更新してください。

## 8. OAuth クライアントの作成

### Google OAuth（Cloud Run では必須）

1. GCP コンソールの [API とサービス → 認証情報](https://console.cloud.google.com/apis/credentials) を開く
2. OAuth 同意画面を設定（ユーザータイプ: 外部、スコープ: `openid email profile`）
3. OAuth 2.0 クライアント ID を作成 → ウェブアプリケーション
4. 承認済みリダイレクト URI: `https://<Cloud Run URL>/oauth/callback`（初回デプロイ後に URL が決まるので後で設定）
5. クライアント ID とシークレットを控える

### GitHub OAuth（オプション）

1. GitHub → Settings → Developer settings → OAuth Apps → New OAuth App
2. Authorization callback URL: `https://<Cloud Run URL>/oauth/callback`
3. クライアント ID とシークレットを控える

## 9. Secret Manager への登録

```bash
# J-Quants API キー
echo -n "<YOUR_JQUANTS_API_KEY>" | gcloud secrets create jquants-api-key --data-file=-

# Google OAuth クライアントシークレット
echo -n "<GOOGLE_OAUTH_CLIENT_SECRET>" | gcloud secrets create google-oauth-client-secret --data-file=-

# GitHub OAuth クライアントシークレット（オプション）
echo -n "<GITHUB_OAUTH_CLIENT_SECRET>" | gcloud secrets create github-oauth-client-secret --data-file=-

# ユーザー API キー暗号化用ランダムキー（AES-256-GCM）
python3 -c "import secrets; print(secrets.token_hex(32))" | \
  tr -d '\n' | gcloud secrets create mcp-encryption-key --data-file=-

# OAuth セッショントークン署名キー
python3 -c "import secrets; print(secrets.token_urlsafe(48))" | \
  tr -d '\n' | gcloud secrets create OAUTH_JWT_SIGNING_KEY --data-file=-

# allowlist: サインインを許可するメールアドレス（カンマ区切り）
# 空にすると認証済みユーザー全員が使用可能
echo -n "you@example.com,family@example.com" | \
  gcloud secrets create jquants-allowed-emails --data-file=-
```

シークレットの更新:

```bash
echo -n "<NEW_VALUE>" | gcloud secrets versions add <SECRET_NAME> --data-file=-
```

## 10. GitHub Actions シークレットの追加

fork したリポジトリの **Settings → Secrets and variables → Actions** に以下を追加:

| シークレット名 | 値 |
|---|---|
| `WIF_PROVIDER` | ステップ 7 で出力した `${WIF_PROVIDER}` |
| `WIF_SERVICE_ACCOUNT` | `${SA}`（フルメールアドレス） |
| `GOOGLE_CLIENT_ID` | ステップ 8 の Google OAuth クライアント ID |
| `GH_OAUTH_CLIENT_ID` | ステップ 8 の GitHub OAuth クライアント ID |

## 11. CD ワークフローの修正

同梱の [`.github/workflows/cd.yml`](../../.github/workflows/cd.yml) は上流プロジェクトの GCP リソース向けに設定されています。fork 内の `gcloud run deploy` 行を自分の環境に合わせて編集してください:

```yaml
gcloud run deploy ${SERVICE} \
  --project ${PROJECT_ID} \
  --region ${REGION} \
  --source . \
  --execution-environment gen2 \
  --memory 4Gi \
  --cpu 1 \
  --cpu-boost \
  --max-instances 3 \
  --clear-volumes --clear-volume-mounts \
  --set-env-vars "..." \
  --set-secrets "..."
```

`OAUTH_BASE_URL` は初回デプロイ後に決まる Cloud Run URL に変更してください（仮の値でデプロイ → URL 確認 → 更新して再デプロイ）。

変更を fork の `main` にコミットします。

## 12. 初期 `cache.db` のアップロード

Cloud Run は `cache.db` を読み取り専用で使います。まずローカルマシンで作成してアップロードします:

```bash
# ローカルで実行
uv run jquants-mcp            # cache.db を作成
uv run scripts/daily_fetch.py # または bulk_fetch_all.py で過去データを取得

# GCS へアップロード
gcloud storage cp ~/.cache/jquants-mcp/cache.db \
  "gs://${GCS_BUCKET}/jquants-mcp/cache.db" \
  --no-gzip-in-flight
```

Cloud Run が常に新鮮なスナップショットを持てるよう、ローカルマシンで `daily_fetch.py + gcs_export_cache.py` を cron / launchd で毎日実行し続けてください。

## 13. デプロイ

**Actions** タブ → **CD** → **Run workflow** から手動で初回デプロイを実行します。初回ビルドは 5〜10 分かかります。

デプロイ成功後、URL を確認:

```bash
gcloud run services describe "${SERVICE}" --region "${REGION}" \
  --format="value(status.url)"
```

以下の順に更新:
1. `cd.yml` の `OAUTH_BASE_URL` をこの URL に変更
2. Google / GitHub OAuth クライアントのリダイレクト URI を `<URL>/oauth/callback` に更新
3. コミット + push → CD が自動再デプロイ

## 14. 動作確認

```bash
URL=$(gcloud run services describe "${SERVICE}" --region "${REGION}" \
  --format="value(status.url)")

# 1. 401 が返れば OAuth が有効な証拠
curl -i -s -o /dev/null -w "%{http_code}\n" "${URL}/mcp"

# 2. /settings が OAuth にリダイレクト
curl -i -s -o /dev/null -w "%{http_code}\n" "${URL}/settings"

# 3. 起動ログを確認
gcloud run services logs read "${SERVICE}" --region "${REGION}" --limit=50 \
  | grep -E "SIGHUP handler installed|Initializing .* OAuth"
```

Claude クライアントからの完全な検証は [ステップ 16](#16-claude-クライアントから接続) で行います。

## 15. カスタムドメイン（オプション）

### Cloud DNS ゾーン作成

```bash
gcloud dns managed-zones create example-com \
  --description="example.com" \
  --dns-name="example.com." \
  --visibility=public
```

レジストラで NS レコードを以下で出力される 4 つのネームサーバーに更新:

```bash
gcloud dns managed-zones describe example-com --format="value(nameServers)"
```

### ドメインマッピング

```bash
gcloud beta run domain-mappings create \
  --service="${SERVICE}" \
  --domain="jquants-mcp.example.com" \
  --region="${REGION}"

# 必要な DNS レコードを確認
gcloud beta run domain-mappings describe \
  --domain="jquants-mcp.example.com" \
  --region="${REGION}" \
  --format="yaml(status.resourceRecords)"
```

返された CNAME（または A/AAAA）を Cloud DNS に追加:

```bash
gcloud dns record-sets create jquants-mcp.example.com. \
  --zone=example-com \
  --type=CNAME \
  --ttl=300 \
  --rrdatas="ghs.googlehosted.com."
```

TLS 証明書は Cloud Run が自動発行します。DNS + 証明書の反映に 15〜60 分かかります。

ドメインが使えるようになったら `OAUTH_BASE_URL` と OAuth リダイレクト URI をカスタムドメインに更新して再デプロイ。

## 16. Claude クライアントから接続

### Claude Desktop（Connectors UI）

1. Settings → Connectors → カスタムコネクタを追加
2. URL: `https://jquants-mcp.example.com/mcp`
3. Google でサインイン — 初回サインインで Firestore にユーザーレコードが作成される
4. `/settings` ページで J-Quants API キーを登録

### Claude モバイル（iOS / Android）

2026-04-23（Sonnet 4.6）時点で動作確認済みです。

1. アプリの **Settings → Connectors → 追加**
2. Claude Desktop と同じ URL を入力
3. Google でサインイン
4. モバイルブラウザで `/settings` ページを開いて J-Quants API キーを登録

### Claude Code（mcp-stdio 経由）

Claude Code には HTTP トランスポートで Bearer ヘッダーが落ちるバグがあります。[mcp-stdio](https://pypi.org/project/mcp-stdio/) をプロキシとして使用:

```bash
claude mcp add jquants-mcp \
  -- uvx mcp-stdio --oauth https://jquants-mcp.example.com/mcp
```

`mcp-stdio --oauth` がブラウザで OAuth 2.1 フローを実行し、トークンをローカルにキャッシュします。

## 17. allowlist のカスタマイズ

`JQUANTS_ALLOWED_EMAILS` シークレットでサインイン可能なユーザーを制御します。

| 用途 | 値 |
|---|---|
| 自分だけ | `you@example.com` |
| 家族・チーム | `you@example.com,family1@example.com,family2@example.com` |
| 認証済み全員 | （空）— Google OAuth 同意画面のみがゲート |

更新:

```bash
echo -n "you@example.com,family@example.com" | \
  gcloud secrets versions add jquants-allowed-emails --data-file=-
gh workflow run cd.yml  # 新バージョンを反映するため再デプロイ
```

## 18. モニタリングとアラート

[`ops/alerts/`](../../ops/alerts/) に alerting policy が含まれています。`ops-email` という通知チャンネルが前提です:

```bash
gcloud alpha monitoring channels create \
  --display-name="ops-email" \
  --type=email \
  --channel-labels=email_address="you@example.com"

# チャンネル ID を取得
gcloud alpha monitoring channels list --format="value(name)"

# ops/alerts/*.yaml にチャンネル ID を記入してから:
for f in ops/alerts/*.yaml; do
  gcloud alpha monitoring policies create --policy-from-file="$f"
done
```

## 19. アップグレード（fork を最新に保つ）

定期的に上流の変更を取り込みます:

```bash
git remote add upstream https://github.com/shigechika/jquants-mcp.git  # 初回のみ
git fetch upstream
git merge upstream/main
# cd.yml の SERVICE / PROJECT_ID / URL の競合を解消
git push origin main
```

CI が通れば CD が自動デプロイします。ロールバックが必要な場合:

```bash
gcloud run services update-traffic "${SERVICE}" --region "${REGION}" \
  --to-revisions=<previous-revision>=100
```

## トラブルシューティング

### WIF の `PERMISSION_DENIED` でデプロイ失敗

Provider の attribute condition がリポジトリパス（大文字小文字含む）と完全一致しているか確認:

```bash
gcloud iam workload-identity-pools providers describe github \
  --workload-identity-pool=github-actions \
  --location=global
```

リポジトリを rename / 移管した場合は `--attribute-condition` を更新してください。

### Cloud Run 503 / ヘルスチェック失敗

ログを確認:

```bash
gcloud run services logs read "${SERVICE}" --region "${REGION}" --limit=100
```

主な原因:
- `cache.db` が GCS からまだダウンロードされていない → コールドスタート後 1〜2 分待つ、またはバケットにオブジェクトが存在するか確認
- 環境変数 / シークレットの設定ミス → `cd.yml` をチェック
- OAuth の設定ミス → `OAUTH_BASE_URL` が Cloud Run URL と一致しているか、リダイレクト URI が正しいか確認

### `cache_status` の返値が最小限（行数なし）

バックグラウンドの `cache.db` ダウンロードが未完了です。ランブック: [cache-db-missing](../runbooks/cache-db-missing.md) を参照。

### OAuth ループまたはサインイン失敗

[oauth-loop](../runbooks/oauth-loop.md) を参照。

### Firestore 権限エラー

SA に `roles/datastore.user` が付与されているか確認:

```bash
gcloud projects get-iam-policy "${PROJECT_ID}" \
  --flatten="bindings[].members" \
  --filter="bindings.members:serviceAccount:${SA}"
```

### その他

[runbooks/](../runbooks/README.md) を参照。
