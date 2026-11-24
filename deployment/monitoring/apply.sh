#!/usr/bin/env bash
# Create or update the canary log-based metrics and dashboard for one Cloud Run service.
#   deployment/monitoring/apply.sh                            navcare-api-dev (default)
#   SERVICE=navcare-api-prod deployment/monitoring/apply.sh   its own metrics and dashboard
# Needs gcloud signed in with access to the project (CLOUDSDK_CORE_ACCOUNT picks the account).
# Metrics only count log lines written after they exist.
set -euo pipefail

PROJECT_ID="${PROJECT_ID:-project-69a1b17d-f37d-485c-912}"
SERVICE="${SERVICE:-navcare-api-dev}"
GCLOUD="${GCLOUD:-gcloud}"
DIR="$(cd "$(dirname "$0")" && pwd)"
if ! [[ "$SERVICE" =~ ^[a-z]([-a-z0-9]*[a-z0-9])?$ ]]; then
  echo "SERVICE must be a Cloud Run service name, not '$SERVICE'." >&2
  exit 1
fi
# Names are per service, so applying one service never repoints another's metrics or dashboard.
PREFIX="${SERVICE//-/_}"
DASHBOARD_NAME="NavCare canary: $SERVICE"  # the displayName in dashboard.json, filled in
TMP="$(mktemp -d)"
trap 'rm -rf "$TMP"' EXIT

gc() { "$GCLOUD" --project="$PROJECT_ID" --quiet "$@"; }
render() { sed -e "s/__SERVICE__/$SERVICE/g" -e "s/__PREFIX__/$PREFIX/g" "$1" > "$2"; }

for file in "$DIR"/metrics/*.yaml; do
  name="${PREFIX}_$(basename "$file" .yaml)"
  render "$file" "$TMP/$name.yaml"
  if filter="$(gc logging metrics describe "$name" --format="value(filter)" 2>/dev/null)"; then
    if [[ "$filter" != *"service_name=\"$SERVICE\""* ]]; then
      echo "Metric $name exists but does not read $SERVICE; not changing it." >&2
      exit 1
    fi
    gc logging metrics update "$name" --config-from-file="$TMP/$name.yaml"
  else
    gc logging metrics create "$name" --config-from-file="$TMP/$name.yaml"
  fi
done

render "$DIR/dashboard.json" "$TMP/dashboard.json"
ids="$(gc monitoring dashboards list --filter="displayName=\"$DASHBOARD_NAME\"" \
  --format="value(name)")"
count="$(printf '%s' "$ids" | grep -c . || true)"
if [ "$count" -gt 1 ]; then
  echo "$count dashboards are named '$DASHBOARD_NAME'; delete the extra ones first." >&2
  exit 1
elif [ "$count" -eq 1 ]; then
  # The API expects the current etag on update, so an edit made since then fails, not lost.
  etag="$(gc monitoring dashboards describe "$ids" --format="value(etag)")"
  if [ -z "$etag" ]; then echo "No etag for $ids; not updating." >&2; exit 1; fi
  sed "1s/^{/{\"etag\": \"$etag\",/" "$TMP/dashboard.json" > "$TMP/update.json"
  gc monitoring dashboards update "$ids" --config-from-file="$TMP/update.json"
else
  gc monitoring dashboards create --config-from-file="$TMP/dashboard.json"
fi
