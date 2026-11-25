#!/usr/bin/env bash
# Create or update the canary log-based metrics, dashboard and alert policies for one Cloud Run
# service.
#   deployment/monitoring/apply.sh                            navcare-api-dev (default)
#   SERVICE=navcare-api-prod deployment/monitoring/apply.sh   its own metrics, dashboard, alerts
#   ALERT_EMAILS="a@example.com,b@example.com" deployment/monitoring/apply.sh
#     also emails these people when an alert fires (one email channel each, reused if it exists).
#     Pass the same list every time: an update sets each policy's channels to exactly this list.
# Needs gcloud signed in with access to the project (CLOUDSDK_CORE_ACCOUNT picks the account),
# with the beta component (alert policies and channels are `gcloud beta monitoring`).
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
    # Request metrics name the service as the Cloud Run resource; the correctness metrics, read
    # from the step check's entries, as jsonPayload.service.
    if [[ "$filter" != *"service_name=\"$SERVICE\""* && \
          "$filter" != *"jsonPayload.service=\"$SERVICE\""* ]]; then
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

# Email channels for the alert policies, from ALERT_EMAILS (comma-separated).
EMAIL_RE='^[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}$'
channels=""
IFS=',' read -r -a emails <<< "${ALERT_EMAILS:-}"
for email in "${emails[@]+"${emails[@]}"}"; do
  email="${email//[[:space:]]/}"
  [ -n "$email" ] || continue
  if ! [[ "$email" =~ $EMAIL_RE ]]; then
    echo "ALERT_EMAILS has '$email', which is not an email address." >&2
    exit 1
  fi
  channel="$(gc beta monitoring channels list \
    --filter="type=\"email\" AND labels.email_address=\"$email\"" --format="value(name)" \
    | head -n 1)"
  if [ -z "$channel" ]; then
    channel="$(gc beta monitoring channels create --type=email \
      --display-name="NavCare canary: $email" --channel-labels="email_address=$email" \
      --format="value(name)")"
  fi
  if [ -z "$channel" ]; then echo "No channel for $email; not applying alerts." >&2; exit 1; fi
  channels="${channels:+$channels, }\"$channel\""
done
if [ -z "$channels" ]; then
  echo "ALERT_EMAILS is empty: the alert policies will open incidents in the console but email" \
    "no one." >&2
fi

# Alert policies, one per file, found again by their displayName (unique per service).
for file in "$DIR"/alerts/*.json; do
  out="$TMP/$(basename "$file")"
  render "$file" "$out.tmp"
  sed "s|\"notificationChannels\": \[\]|\"notificationChannels\": [$channels]|" "$out.tmp" > "$out"
  name="$(sed -n 's/^  "displayName": "\(.*\)",$/\1/p' "$out")"
  if [ -z "$name" ]; then echo "No displayName in $file." >&2; exit 1; fi
  ids="$(gc beta monitoring policies list --filter="displayName=\"$name\"" --format="value(name)")"
  count="$(printf '%s' "$ids" | grep -c . || true)"
  if [ "$count" -gt 1 ]; then
    echo "$count alert policies are named '$name'; delete the extra ones first." >&2
    exit 1
  elif [ "$count" -eq 1 ]; then
    gc beta monitoring policies update "$ids" --policy-from-file="$out"
  else
    gc beta monitoring policies create --policy-from-file="$out"
  fi
done
