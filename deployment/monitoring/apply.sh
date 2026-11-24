#!/usr/bin/env bash
# Create or update the canary log-based metrics and dashboard from deployment/monitoring/.
#   deployment/monitoring/apply.sh
# Needs gcloud signed in with access to the project (CLOUDSDK_CORE_ACCOUNT picks the account).
# Metrics only count log lines written after they exist.
set -euo pipefail

PROJECT_ID="${PROJECT_ID:-project-69a1b17d-f37d-485c-912}"
GCLOUD="${GCLOUD:-gcloud}"
DIR="$(cd "$(dirname "$0")" && pwd)"
DASHBOARD_NAME="NavCare canary (v1 vs v2)"  # must match displayName in dashboard.json

gc() { "$GCLOUD" --project="$PROJECT_ID" --quiet "$@"; }

for file in "$DIR"/metrics/*.yaml; do
  name="$(basename "$file" .yaml)"
  if gc logging metrics describe "$name" >/dev/null 2>&1; then
    gc logging metrics update "$name" --config-from-file="$file"
  else
    gc logging metrics create "$name" --config-from-file="$file"
  fi
done

# Update the dashboard in place (same URL) if it exists; otherwise create it.
id="$(gc monitoring dashboards list --filter="displayName=\"$DASHBOARD_NAME\"" \
  --format="value(name)" | head -n 1)"
if [ -n "$id" ]; then
  gc monitoring dashboards update "$id" --config-from-file="$DIR/dashboard.json"
else
  gc monitoring dashboards create --config-from-file="$DIR/dashboard.json"
fi
