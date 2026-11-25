#!/usr/bin/env bash
# Build the committed code (HEAD) with Cloud Build and deploy it to Cloud Run.
#   deployment/deploy.sh                              dev service
#   ENGINE_HEADER_ENABLED=true deployment/deploy.sh   dev, X-Engine on for a controlled test
#   ENV=prod deployment/deploy.sh                     prod service
# Needs gcloud signed in with access to the project (set CLOUDSDK_CORE_ACCOUNT to pick an
# account). No secrets here: the Maps key comes from Secret Manager.
set -euo pipefail

PROJECT_ID="${PROJECT_ID:-project-69a1b17d-f37d-485c-912}"
ENV="${ENV:-dev}"
REGION="${REGION:-northamerica-northeast1}"
SERVICE="${SERVICE:-navcare-api-$ENV}"
REPO="navcare"
MAX_INSTANCES="${MAX_INSTANCES:-2}"  # limits exposure; not a spending cap
GCLOUD="${GCLOUD:-gcloud}"
IMAGE="$REGION-docker.pkg.dev/$PROJECT_ID/$REPO/api:$(git rev-parse --short HEAD)"
# Off unless asked for: with it on, anyone who can call the service can force the paid v2.
ENGINE_HEADER_ENABLED="${ENGINE_HEADER_ENABLED:-false}"
if [ "$ENV" != dev ]; then ENGINE_HEADER_ENABLED=false; fi

gc() { "$GCLOUD" --project="$PROJECT_ID" --quiet "$@"; }

# 1. Docker repository, created once.
if ! gc artifacts repositories describe "$REPO" --location="$REGION" >/dev/null 2>&1; then
  gc artifacts repositories create "$REPO" --repository-format=docker \
    --location="$REGION" --labels=app=navcare
fi

# 2. Build only committed files, so .env and local edits never leave this machine.
SOURCE="$(mktemp -d)/source.tgz"
git archive --format=tar.gz -o "$SOURCE" HEAD
gc builds submit "$SOURCE" --region="$REGION" --tag="$IMAGE"

# 3. Deploy. The canary share is set only when the service is created, so a redeploy keeps it.
ENV_VARS="GOOGLE_CLOUD_PROJECT=$PROJECT_ID,ENGINE_HEADER_ENABLED=$ENGINE_HEADER_ENABLED"
if ! gc run services describe "$SERVICE" --region="$REGION" >/dev/null 2>&1; then
  ENV_VARS="$ENV_VARS,V2_TRAFFIC_PERCENT=0"
fi
# Public access is not changed here (see the README).
gc run deploy "$SERVICE" --image="$IMAGE" --region="$REGION" \
  --service-account="navcare-run@$PROJECT_ID.iam.gserviceaccount.com" \
  --set-secrets=GOOGLE_MAPS_API_KEY=GOOGLE_MAPS_API_KEY:latest,NAVCARE_API_KEYS=NAVCARE_API_KEYS:latest \
  --update-env-vars="$ENV_VARS" \
  --labels="app=navcare,env=$ENV" \
  --cpu=1 --memory=512Mi --concurrency=40 --timeout=120 \
  --min-instances=0 --max-instances="$MAX_INSTANCES"

gc run services describe "$SERVICE" --region="$REGION" --format="value(status.url)"
