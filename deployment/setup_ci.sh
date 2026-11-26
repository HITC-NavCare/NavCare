#!/usr/bin/env bash
# One-time Google Cloud setup for the deploy workflow (.github/workflows/deploy.yml, #30).
# Needs a project Owner: Editors cannot grant roles or create the GitHub sign-in.
#   deployment/setup_ci.sh --dry-run   prints every change, makes none
#   deployment/setup_ci.sh             makes them; safe to run again
# It creates the github-deployer service account with only what deployment/deploy.sh needs,
# and lets GitHub Actions sign in as it from the main branch of this repository only, with
# Workload Identity Federation: no key file is created.
set -euo pipefail

PROJECT_ID="${PROJECT_ID:-project-69a1b17d-f37d-485c-912}"
REGION="${REGION:-northamerica-northeast1}"
GCLOUD="${GCLOUD:-gcloud}"
GITHUB_REPO="HITC-NavCare/NavCare"
GITHUB_REPO_ID="1426436353"  # stays the same if the repository is renamed; a new one gets a new id
SA_NAME="github-deployer"
POOL="github"
PROVIDER="navcare-main"

case "${1:-}" in
  --dry-run) DRY_RUN=true ;;
  "") DRY_RUN=false ;;
  *) echo "usage: deployment/setup_ci.sh [--dry-run]" >&2; exit 2 ;;
esac

gc() { "$GCLOUD" --project="$PROJECT_ID" --quiet "$@"; }
# Changes go through change(): printed in a dry run, run otherwise. Reads always run.
change() {
  if [ "$DRY_RUN" = true ]; then
    printf '+ gcloud'; printf ' %q' "$@"; printf '\n'
  else
    gc "$@" >/dev/null
  fi
}

PROJECT_NUMBER="$(gc projects describe "$PROJECT_ID" --format='value(projectNumber)')"
DEPLOYER="$SA_NAME@$PROJECT_ID.iam.gserviceaccount.com"
RUNTIME="navcare-run@$PROJECT_ID.iam.gserviceaccount.com"
# deploy.sh's builds run as Cloud Build's default service account, so submitting one acts as it.
BUILDER="$(gc builds get-default-service-account --region="$REGION" \
  --format='value(serviceAccountEmail)')"
BUILDER="${BUILDER##*/}"  # projects/<number>/serviceAccounts/<email> -> <email>
POOL_ID="projects/$PROJECT_NUMBER/locations/global/workloadIdentityPools/$POOL"
MEMBER="serviceAccount:$DEPLOYER"

echo "Project $PROJECT_ID ($PROJECT_NUMBER); deployer $DEPLOYER; builds run as $BUILDER"
[ "$DRY_RUN" = true ] && echo "Dry run: nothing is changed."

# 1. APIs for the GitHub sign-in (token exchange, then a short-lived token for the deployer).
change services enable sts.googleapis.com iamcredentials.googleapis.com

# 2. The deployer service account.
if ! gc iam service-accounts describe "$DEPLOYER" >/dev/null 2>&1; then
  change iam service-accounts create "$SA_NAME" \
    --display-name="GitHub Actions deployer (deploy.yml, #30)"
  # A new account can take a few seconds to show up for the role grants below.
  for _ in 1 2 3 4 5 6 7 8 9 10; do
    [ "$DRY_RUN" = true ] && break
    gc iam service-accounts describe "$DEPLOYER" >/dev/null 2>&1 && break
    sleep 3
  done
fi

# 3. What deploy.sh and the smoke test do as the deployer. run.developer also lets it call the
#    private service for the smoke test.
for role in run.developer cloudbuild.builds.editor artifactregistry.reader \
            serviceusage.serviceUsageConsumer; do
  change projects add-iam-policy-binding "$PROJECT_ID" --member="$MEMBER" \
    --role="roles/$role" --condition=None
done
# Deploy the service as navcare-run, and submit builds that run as Cloud Build's account.
for account in "$RUNTIME" "$BUILDER"; do
  change iam service-accounts add-iam-policy-binding "$account" --member="$MEMBER" \
    --role=roles/iam.serviceAccountUser --condition=None
done
# Upload the source to the Cloud Build bucket (deploy.yml names it, see STAGING_BUCKET).
for role in storage.legacyBucketReader storage.objectCreator; do
  change storage buckets add-iam-policy-binding "gs://${PROJECT_ID}_cloudbuild" \
    --member="$MEMBER" --role="roles/$role"
done

# 4. Workload Identity Federation: GitHub's sign-in is accepted only for pushes or manual runs
#    on main of this repository. Re-running updates the provider to these settings.
if ! gc iam workload-identity-pools describe "$POOL" --location=global >/dev/null 2>&1; then
  change iam workload-identity-pools create "$POOL" --location=global \
    --display-name="GitHub Actions"
fi
CONDITION="assertion.repository_id == '$GITHUB_REPO_ID' && assertion.repository == '$GITHUB_REPO' && assertion.ref == 'refs/heads/main'"
MAPPING="google.subject=assertion.sub,attribute.repository=assertion.repository,attribute.ref=assertion.ref"
if gc iam workload-identity-pools providers describe "$PROVIDER" --location=global \
     --workload-identity-pool="$POOL" >/dev/null 2>&1; then
  ACTION=update-oidc
else
  ACTION=create-oidc
fi
change iam workload-identity-pools providers "$ACTION" "$PROVIDER" --location=global \
  --workload-identity-pool="$POOL" --display-name="NavCare main branch" \
  --issuer-uri=https://token.actions.githubusercontent.com \
  --attribute-mapping="$MAPPING" --attribute-condition="$CONDITION"
change iam service-accounts add-iam-policy-binding "$DEPLOYER" \
  --member="principalSet://iam.googleapis.com/$POOL_ID/attribute.repository/$GITHUB_REPO" \
  --role=roles/iam.workloadIdentityUser --condition=None

cat <<EOF

Add these as GitHub repository variables (Settings > Secrets and variables > Actions >
Variables). They are not secrets.
  GCP_WORKLOAD_IDENTITY_PROVIDER = $POOL_ID/providers/$PROVIDER
  GCP_DEPLOYER_SA                = $DEPLOYER
Or with the GitHub CLI:
  gh variable set GCP_WORKLOAD_IDENTITY_PROVIDER --repo $GITHUB_REPO --body "$POOL_ID/providers/$PROVIDER"
  gh variable set GCP_DEPLOYER_SA --repo $GITHUB_REPO --body "$DEPLOYER"
EOF
