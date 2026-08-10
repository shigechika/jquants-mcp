#!/bin/bash
# assert-jquants-billing-settings.sh -- fail loudly if the stdio Cloud Run
# service's billing-relevant settings are not exactly what they should be.
#
# Checked against fixed expected values, not a before/after diff of the same
# deploy: a diff alone passes silently when the service was *already*
# misconfigured before the run started -- which is exactly the incident this
# guard exists for (an out-of-band `gcloud` change persists across every
# later deploy that only confirms "nothing changed this run"). Called both
# before and after the deploy step in cd.yml, so pre-existing drift fails
# fast instead of letting a deploy land on top of it.
#
# Usage: assert-jquants-billing-settings.sh <pre-deploy|post-deploy>
# Requires GCP_PROJECT, GCP_REGION, CLOUDRUN_SERVICE in the environment.

set -euo pipefail

STAGE="${1:?usage: $0 <pre-deploy|post-deploy>}"

# minScale: Cloud Run omits this annotation entirely when scale-to-zero is
# in effect -- it never writes an explicit "0", so the expected value here
# is the empty string, not "0".
EXPECTED_MINSCALE=""
EXPECTED_MAXSCALE="1"
EXPECTED_THROTTLE="false"

# --format=json + jq, not `--format=value(...)` + `read`: bash `read` strips
# leading/trailing runs of IFS whitespace before splitting -- including a
# leading empty field from a tab-separated line -- regardless of what IFS is
# set to, as long as IFS consists solely of whitespace characters. minScale
# being absent (the expected, correct state) produces exactly that leading
# empty field, which silently shifted every value by one and made this
# script reject a correctly configured service (ai-review R2F1, reproduced
# and confirmed before this fix).
DESCRIBE_JSON=$(gcloud run services describe "$CLOUDRUN_SERVICE" \
  --project "$GCP_PROJECT" \
  --region "$GCP_REGION" \
  --format=json)
MINSCALE=$(echo "$DESCRIBE_JSON" | jq -r '.spec.template.metadata.annotations["autoscaling.knative.dev/minScale"] // ""')
MAXSCALE=$(echo "$DESCRIBE_JSON" | jq -r '.spec.template.metadata.annotations["autoscaling.knative.dev/maxScale"] // ""')
THROTTLE=$(echo "$DESCRIBE_JSON" | jq -r '.spec.template.metadata.annotations["run.googleapis.com/cpu-throttling"] // ""')

echo "[$STAGE] minScale='$MINSCALE' maxScale='$MAXSCALE' cpu-throttling='$THROTTLE'"

if [[ "$MINSCALE" != "$EXPECTED_MINSCALE" ]] || \
   [[ "$MAXSCALE" != "$EXPECTED_MAXSCALE" ]] || \
   [[ "$THROTTLE" != "$EXPECTED_THROTTLE" ]]; then
  echo "::error::[$STAGE] Billing-relevant settings do not match the expected values \
(minScale expected '$EXPECTED_MINSCALE' got '$MINSCALE', \
maxScale expected '$EXPECTED_MAXSCALE' got '$MAXSCALE', \
cpu-throttling expected '$EXPECTED_THROTTLE' got '$THROTTLE'). \
This deploy step never passes scaling/CPU flags, so this is either a \
pre-existing drift from an out-of-band change or a bug in this script's \
expected values -- investigate before deploying again."
  exit 1
fi

echo "[$STAGE] Billing settings OK."
