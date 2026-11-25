#!/usr/bin/env bash
# Change the canary on Cloud Run: the v2 share, the rollback and the demo's fault injection.
#   deployment/canary.sh status                 print the current settings
#   deployment/canary.sh set <0-100>            send that share of requests to v2
#   deployment/canary.sh rollback               all traffic to v1 (V2_TRAFFIC_PERCENT=0)
#   deployment/canary.sh fault error            v2 fails, the v1 fallback answers (docs/canary.md)
#   deployment/canary.sh fault slow [seconds]   v2 waits first (default 35 s, at most 60)
#   deployment/canary.sh fault off              stop injecting faults
# Same settings as deploy.sh (PROJECT_ID, ENV, REGION, SERVICE, GCLOUD). Every change except
# status creates a new revision (about a minute) and changes only the variables named here.
# It prints the values before and after, read back from the service, not assumed.
set -euo pipefail

PROJECT_ID="${PROJECT_ID:-project-69a1b17d-f37d-485c-912}"
ENV="${ENV:-dev}"
REGION="${REGION:-northamerica-northeast1}"
SERVICE="${SERVICE:-navcare-api-$ENV}"
GCLOUD="${GCLOUD:-gcloud}"
PYTHON="${PYTHON:-$(command -v python3 || command -v python || echo python3)}"
MAX_SLOW_SECONDS=60  # the agent's Gemini budget (app/agent_v2/fault_injection.py)
SETTINGS="V2_TRAFFIC_PERCENT V2_FAULT_ENABLED V2_FAULT_MODE V2_FAULT_SLOW_SECONDS ENGINE_HEADER_ENABLED"

gc() { "$GCLOUD" --project="$PROJECT_ID" --quiet "$@"; }

usage() {
  sed -n '2,8p' "$0" | sed 's/^# //' >&2
  exit 2
}

# One "NAME=value" line per setting, "(unset)" when the service does not have it.
settings() {
  gc run services describe "$SERVICE" --region="$REGION" --format=json |
    "$PYTHON" -c '
import json, sys
service = json.load(sys.stdin)
env = {item["name"]: item.get("value", "(from a secret)")
       for item in service["spec"]["template"]["spec"]["containers"][0].get("env", [])}
for name in sys.argv[1:]:
    print(name + "=" + env.get(name, "(unset)"))
' $SETTINGS
}

show() {
  echo "$1 ($SERVICE, $REGION):"
  settings | sed 's/^/  /'
}

warn_if_fault_on() {
  if settings | grep -qx "V2_FAULT_ENABLED=true"; then
    echo "Note: fault injection is ON, so v2 requests fail or slow down on purpose." \
         "Turn it off with: deployment/canary.sh fault off"
  fi
}

update() {
  show "Before"
  gc run services update "$SERVICE" --region="$REGION" --update-env-vars="$1"
  show "After"
  warn_if_fault_on
}

[ $# -ge 1 ] || usage
case "$1" in
  status)
    [ $# -eq 1 ] || usage
    show "Current"
    warn_if_fault_on
    ;;
  set)
    [ $# -eq 2 ] || usage
    # Whole numbers only: the steps in docs/canary.md are 0, 10, 25, 50 and 100.
    if ! [[ "$2" =~ ^[0-9]+$ ]] || [ "$((10#$2))" -gt 100 ]; then
      echo "The v2 share must be a whole number from 0 to 100, not '$2'." >&2
      exit 2
    fi
    update "V2_TRAFFIC_PERCENT=$((10#$2))"
    ;;
  rollback)
    [ $# -eq 1 ] || usage
    update "V2_TRAFFIC_PERCENT=0"
    echo "Rolled back: every new request goes to v1."
    ;;
  fault)
    [ $# -ge 2 ] || usage
    case "$2" in
      error)
        [ $# -eq 2 ] || usage
        update "V2_FAULT_ENABLED=true,V2_FAULT_MODE=error"
        ;;
      slow)
        [ $# -le 3 ] || usage
        seconds="${3:-35}"
        if ! [[ "$seconds" =~ ^[0-9]+$ ]] || [ "$((10#$seconds))" -lt 1 ] \
            || [ "$((10#$seconds))" -gt "$MAX_SLOW_SECONDS" ]; then
          echo "The wait must be a whole number of seconds from 1 to $MAX_SLOW_SECONDS," \
               "not '$seconds'." >&2
          exit 2
        fi
        update "V2_FAULT_ENABLED=true,V2_FAULT_MODE=slow,V2_FAULT_SLOW_SECONDS=$((10#$seconds))"
        ;;
      off)
        [ $# -eq 2 ] || usage
        update "V2_FAULT_ENABLED=false,V2_FAULT_MODE=off"
        ;;
      *) usage ;;
    esac
    ;;
  *) usage ;;
esac
