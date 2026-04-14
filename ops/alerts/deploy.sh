#!/usr/bin/env bash
# Deploy all alert policies under ops/alerts/*.yaml to Cloud Monitoring.
# Idempotent: updates policies matched by displayName, creates otherwise.
#
# Usage:
#   CHANNEL="projects/aikawa-dx/notificationChannels/<ID>" ./ops/alerts/deploy.sh
#
# Find the channel ID with:
#   gcloud beta monitoring channels list --project aikawa-dx --format='value(name)'

set -euo pipefail

PROJECT="${PROJECT:-aikawa-dx}"
CHANNEL="${CHANNEL:?CHANNEL env var required (projects/<P>/notificationChannels/<ID>)}"
DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

tmpdir="$(mktemp -d)"
trap 'rm -rf "$tmpdir"' EXIT

existing_json="$(
  gcloud alpha monitoring policies list \
    --project="$PROJECT" --format=json
)"

for yaml in "$DIR"/*.yaml; do
  rendered="$tmpdir/$(basename "$yaml")"
  sed "s|__CHANNEL__|$CHANNEL|g" "$yaml" > "$rendered"

  display_name="$(grep -E '^displayName:' "$rendered" | head -n1 | sed -E 's/^displayName:[[:space:]]*"?([^"]+)"?.*/\1/')"

  existing_name="$(
    echo "$existing_json" \
      | jq -r --arg dn "$display_name" '.[] | select(.displayName == $dn) | .name' \
      | head -n1
  )"

  if [[ -n "$existing_name" ]]; then
    echo "updating: $display_name"
    gcloud alpha monitoring policies update "$existing_name" \
      --project="$PROJECT" \
      --policy-from-file="$rendered" >/dev/null
  else
    echo "creating: $display_name"
    gcloud alpha monitoring policies create \
      --project="$PROJECT" \
      --policy-from-file="$rendered" >/dev/null
  fi
done

echo "done."
