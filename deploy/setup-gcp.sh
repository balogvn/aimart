#!/usr/bin/env bash
# One-time Google Cloud setup so GitHub Actions can deploy AImart Parquet to Cloud Run with no
# long-lived credential anywhere. Idempotent: every step checks before it creates, so re-running
# converges on the same state and never rotates a secret (rotation is deliberate; see README).
#
#   PROJECT_ID=my-project deploy/setup-gcp.sh
#   PROJECT_ID=my-project BUCKET=my-project-aimart-artifacts deploy/setup-gcp.sh   # GCS artifacts
#
# Needs: gcloud (logged in as a project Owner, or someone who can create service accounts, WIF
# pools, secrets and IAM bindings), python3 (only to generate random secrets), and billing
# enabled on the project. It changes nothing on GitHub; it prints the variables to set there.
#
# What it creates:
#   * APIs: Cloud Run, IAM, IAM Credentials, STS, Resource Manager, Secret Manager, Storage
#   * aimart-deployer SA: what GitHub Actions becomes (deploys revisions, nothing else)
#   * aimart-runtime SA: what the service runs as (reads its two secrets; optional bucket)
#   * Workload Identity pool + GitHub OIDC provider, restricted to $GITHUB_REPO owned by the
#     GitHub account with id $GITHUB_OWNER_ID (a name alone can be re-registered by someone else
#     after a rename or deletion; the numeric id cannot)
#   * Secret Manager secrets AIMART_SIGNING_KEY_MLDSA and AIMART_SIGNING_SECRET, generated here
#     and piped straight into gcloud on stdin: never echoed, never in argv, never on disk
#   * optionally a bucket for artifacts with a 1-day delete rule
set -euo pipefail

# ------------------------------------------------------------------------------ settings
PROJECT_ID="${PROJECT_ID:-}"
REGION="${REGION:-us-central1}"
SERVICE="${SERVICE:-aimart-parquet}"
GITHUB_REPO="${GITHUB_REPO:-balogvn/aimart}"
# `gh api users/balogvn --jq .id`; for an organization use its id (`gh api orgs/<org> --jq .id`).
GITHUB_OWNER_ID="${GITHUB_OWNER_ID:-20478656}"
POOL_ID="${POOL_ID:-github}"
PROVIDER_ID="${PROVIDER_ID:-aimart}"
DEPLOYER_SA_NAME="${DEPLOYER_SA_NAME:-aimart-deployer}"
RUNTIME_SA_NAME="${RUNTIME_SA_NAME:-aimart-runtime}"
SECRET_SIGNING_KEY="${SECRET_SIGNING_KEY:-aimart-signing-key-mldsa}"
SECRET_SIGNING_SECRET="${SECRET_SIGNING_SECRET:-aimart-signing-secret}"
BUCKET="${BUCKET:-}" # empty: artifacts stay in the instance's RAM-backed /tmp
# generate (default): random values made here. prompt: paste existing values (input hidden),
# e.g. to keep the ML-DSA key you already publish so old signatures keep verifying.
SECRET_SOURCE="${SECRET_SOURCE:-generate}"

# ------------------------------------------------------------------------------ helpers
log() { printf '\n==> %s\n' "$*"; }
die() { printf 'error: %s\n' "$*" >&2; exit 1; }

[ -n "$PROJECT_ID" ] || die "set PROJECT_ID, e.g. PROJECT_ID=my-project $0"
command -v gcloud > /dev/null || die "gcloud is not installed (https://cloud.google.com/sdk/docs/install)"
command -v python3 > /dev/null || die "python3 is needed to generate secrets"
case "$SECRET_SOURCE" in generate | prompt) ;; *) die "SECRET_SOURCE must be generate or prompt" ;; esac
[[ "$GITHUB_REPO" =~ ^[A-Za-z0-9-]+/[A-Za-z0-9._-]+$ ]] || die "GITHUB_REPO must be owner/name"
[[ "$GITHUB_OWNER_ID" =~ ^[0-9]+$ ]] || die "GITHUB_OWNER_ID must be the numeric GitHub account id"

# Every command names the project explicitly; the caller's gcloud config is never changed.
gc() { gcloud --project="$PROJECT_ID" --quiet "$@"; }

DEPLOYER_SA="${DEPLOYER_SA_NAME}@${PROJECT_ID}.iam.gserviceaccount.com"
RUNTIME_SA="${RUNTIME_SA_NAME}@${PROJECT_ID}.iam.gserviceaccount.com"

# Print a fresh random value on stdout, for piping straight into `gcloud ... --data-file=-`.
generate_secret() {
  case "$1" in
    # FIPS 204 ML-DSA-65 key generation from a 32-byte seed; the server takes base64 of it.
    mldsa) python3 -c 'import base64, secrets, sys; sys.stdout.write(base64.b64encode(secrets.token_bytes(32)).decode())' ;;
    # HMAC key for signed download links.
    hmac) python3 -c 'import secrets, sys; sys.stdout.write(secrets.token_urlsafe(48))' ;;
  esac
}

# Create a secret and its first version, unless it already has an enabled version. Re-running
# never adds a version: a new version of the signing key is a key rotation.
ensure_secret() {
  local name="$1" kind="$2" value
  if ! gc secrets describe "$name" > /dev/null 2>&1; then
    gc secrets create "$name" --replication-policy=automatic \
      --labels=app=aimart-parquet > /dev/null
    echo "created secret $name"
  fi
  if [ -n "$(gc secrets versions list "$name" --filter='state=ENABLED' --format='value(name)' --limit=1)" ]; then
    echo "secret $name already has an enabled version: left unchanged"
    return
  fi
  if [ "$SECRET_SOURCE" = "prompt" ]; then
    # -s: no echo. The value goes from this variable to gcloud's stdin, nowhere else.
    read -rsp "Paste the value for $name (input hidden): " value < /dev/tty
    echo
    [ -n "$value" ] || die "empty value for $name"
    printf '%s' "$value" | gc secrets versions add "$name" --data-file=- > /dev/null
    unset value
  else
    generate_secret "$kind" | gc secrets versions add "$name" --data-file=- > /dev/null
  fi
  echo "added the first version of secret $name (value not shown)"
}

ensure_sa() {
  local name="$1" display="$2" email="${1}@${PROJECT_ID}.iam.gserviceaccount.com"
  if gc iam service-accounts describe "$email" > /dev/null 2>&1; then
    echo "service account $email exists"
  else
    gc iam service-accounts create "$name" --display-name="$display" > /dev/null
    echo "created service account $email"
  fi
}

# ------------------------------------------------------------------------------ 1. APIs
log "Enabling APIs (no-op when already enabled)"
gc services enable \
  run.googleapis.com \
  iam.googleapis.com \
  iamcredentials.googleapis.com \
  sts.googleapis.com \
  cloudresourcemanager.googleapis.com \
  secretmanager.googleapis.com \
  storage.googleapis.com

PROJECT_NUMBER="$(gc projects describe "$PROJECT_ID" --format='value(projectNumber)')"
[ -n "$PROJECT_NUMBER" ] || die "could not read the project number of $PROJECT_ID"

# ------------------------------------------------------------------------------ 2. accounts
log "Service accounts"
ensure_sa "$DEPLOYER_SA_NAME" "AImart Parquet deployer (GitHub Actions)"
ensure_sa "$RUNTIME_SA_NAME" "AImart Parquet runtime (Cloud Run service)"

# ------------------------------------------------------------------------------ 3. WIF
log "Workload Identity Federation for GitHub Actions"
if ! gc iam workload-identity-pools describe "$POOL_ID" --location=global > /dev/null 2>&1; then
  gc iam workload-identity-pools create "$POOL_ID" --location=global \
    --display-name="GitHub Actions" > /dev/null
  echo "created pool $POOL_ID"
else
  echo "pool $POOL_ID exists"
fi

# Only tokens from this repository, owned by this account id, are accepted at all; the IAM
# binding below then narrows who may act as the deployer to the same repository.
CONDITION="assertion.repository == '${GITHUB_REPO}' && assertion.repository_owner_id == '${GITHUB_OWNER_ID}'"
MAPPING="google.subject=assertion.sub,attribute.repository=assertion.repository,attribute.repository_owner_id=assertion.repository_owner_id,attribute.ref=assertion.ref"
if gc iam workload-identity-pools providers describe "$PROVIDER_ID" \
  --location=global --workload-identity-pool="$POOL_ID" > /dev/null 2>&1; then
  # Converge: re-apply the mapping and condition so an edited script is reflected.
  gc iam workload-identity-pools providers update-oidc "$PROVIDER_ID" \
    --location=global --workload-identity-pool="$POOL_ID" \
    --attribute-mapping="$MAPPING" --attribute-condition="$CONDITION" > /dev/null
  echo "provider $PROVIDER_ID exists (mapping and condition re-applied)"
else
  gc iam workload-identity-pools providers create-oidc "$PROVIDER_ID" \
    --location=global --workload-identity-pool="$POOL_ID" \
    --display-name="GitHub ${GITHUB_REPO}" \
    --issuer-uri="https://token.actions.githubusercontent.com" \
    --attribute-mapping="$MAPPING" --attribute-condition="$CONDITION" > /dev/null
  echo "created provider $PROVIDER_ID"
fi
PROVIDER="projects/${PROJECT_NUMBER}/locations/global/workloadIdentityPools/${POOL_ID}/providers/${PROVIDER_ID}"
PRINCIPAL_SET="principalSet://iam.googleapis.com/projects/${PROJECT_NUMBER}/locations/global/workloadIdentityPools/${POOL_ID}/attribute.repository/${GITHUB_REPO}"

# ------------------------------------------------------------------------------ 4. IAM
log "IAM bindings (add-iam-policy-binding is idempotent)"
# GitHub Actions in this repository may impersonate the deployer, and nothing else may.
gc iam service-accounts add-iam-policy-binding "$DEPLOYER_SA" \
  --role=roles/iam.workloadIdentityUser --member="$PRINCIPAL_SET" > /dev/null
# The deployer manages Cloud Run services (run.admin includes setting the public invoker
# binding that --allow-unauthenticated needs) ...
gc projects add-iam-policy-binding "$PROJECT_ID" \
  --role=roles/run.admin --member="serviceAccount:${DEPLOYER_SA}" --condition=None > /dev/null
# ... and may attach the runtime identity to a revision, but cannot act as anything else.
gc iam service-accounts add-iam-policy-binding "$RUNTIME_SA" \
  --role=roles/iam.serviceAccountUser --member="serviceAccount:${DEPLOYER_SA}" > /dev/null
echo "deployer: workloadIdentityUser (from ${GITHUB_REPO}), run.admin, serviceAccountUser on runtime"

# ------------------------------------------------------------------------------ 5. secrets
log "Secrets (values are generated and piped into Secret Manager; never printed)"
ensure_secret "$SECRET_SIGNING_KEY" mldsa
ensure_secret "$SECRET_SIGNING_SECRET" hmac
for name in "$SECRET_SIGNING_KEY" "$SECRET_SIGNING_SECRET"; do
  gc secrets add-iam-policy-binding "$name" \
    --role=roles/secretmanager.secretAccessor --member="serviceAccount:${RUNTIME_SA}" > /dev/null
done
echo "runtime: secretAccessor on $SECRET_SIGNING_KEY and $SECRET_SIGNING_SECRET only"

# ------------------------------------------------------------------------------ 6. bucket
if [ -n "$BUCKET" ]; then
  log "Artifact bucket gs://${BUCKET}"
  if ! gc storage buckets describe "gs://${BUCKET}" > /dev/null 2>&1; then
    # Same region as the service (no egress between them), and inside the GCS free tier's
    # regions (us-central1, us-east1, us-west1).
    gc storage buckets create "gs://${BUCKET}" --location="$REGION" \
      --default-storage-class=STANDARD --uniform-bucket-level-access \
      --public-access-prevention > /dev/null
    echo "created gs://${BUCKET}"
  else
    echo "gs://${BUCKET} exists"
  fi
  lifecycle="$(mktemp)"
  trap 'rm -f "$lifecycle"' EXIT
  # Artifacts live an hour (AIMART_ARTIFACT_TTL_S); the bucket deletes anything a day old as a
  # backstop, and keeps no soft-deleted copies (they would be billed for 7 more days).
  printf '%s\n' '{"rule": [{"action": {"type": "Delete"}, "condition": {"age": 1}}]}' > "$lifecycle"
  gc storage buckets update "gs://${BUCKET}" --lifecycle-file="$lifecycle" --clear-soft-delete > /dev/null
  gc storage buckets add-iam-policy-binding "gs://${BUCKET}" \
    --role=roles/storage.objectAdmin --member="serviceAccount:${RUNTIME_SA}" > /dev/null
  # V4 signed download URLs without a key file: the runtime SA signs blobs as itself through
  # the IAM Credentials API, which needs Token Creator on itself.
  gc iam service-accounts add-iam-policy-binding "$RUNTIME_SA" \
    --role=roles/iam.serviceAccountTokenCreator --member="serviceAccount:${RUNTIME_SA}" > /dev/null
  echo "runtime: objectAdmin on gs://${BUCKET}, tokenCreator on itself; lifecycle: delete at 1 day"
fi

# ------------------------------------------------------------------------------ 7. output
# Cloud Run's deterministic URL: known before the first deploy, so AIMART_PUBLIC_BASE_URL can be
# set now. Replace it with your custom domain if you map one.
URL="https://${SERVICE}-${PROJECT_NUMBER}.${REGION}.run.app"

log "Done. Set these GitHub repository variables (Settings -> Secrets and variables -> Actions -> Variables)"
cat << EOF

  GCP_PROJECT_ID=${PROJECT_ID}
  GCP_REGION=${REGION}
  GCP_SERVICE=${SERVICE}
  GCP_WORKLOAD_IDENTITY_PROVIDER=${PROVIDER}
  GCP_SERVICE_ACCOUNT=${DEPLOYER_SA}
  GCP_RUNTIME_SERVICE_ACCOUNT=${RUNTIME_SA}
  GCP_SECRET_SIGNING_KEY=${SECRET_SIGNING_KEY}
  GCP_SECRET_SIGNING_SECRET=${SECRET_SIGNING_SECRET}
  AIMART_PUBLIC_BASE_URL=${URL}
  PRODUCTION_URL=${URL}
  AIMART_NETWORK_MODE=testnet
  AIMART_PAY_TO_EVM=<your Base address: receive-only, never a key>
EOF
if [ -n "$BUCKET" ]; then
  echo "  AIMART_GCS_BUCKET=${BUCKET}"
else
  echo "  AIMART_ARTIFACT_MAX_TOTAL_BYTES=268435456"
fi

cat << EOF

Or with the GitHub CLI (run them yourself; this script never touches GitHub):

  gh variable set GCP_PROJECT_ID --repo ${GITHUB_REPO} --body '${PROJECT_ID}'
  gh variable set GCP_REGION --repo ${GITHUB_REPO} --body '${REGION}'
  gh variable set GCP_SERVICE --repo ${GITHUB_REPO} --body '${SERVICE}'
  gh variable set GCP_WORKLOAD_IDENTITY_PROVIDER --repo ${GITHUB_REPO} --body '${PROVIDER}'
  gh variable set GCP_SERVICE_ACCOUNT --repo ${GITHUB_REPO} --body '${DEPLOYER_SA}'
  gh variable set GCP_RUNTIME_SERVICE_ACCOUNT --repo ${GITHUB_REPO} --body '${RUNTIME_SA}'
  gh variable set AIMART_PUBLIC_BASE_URL --repo ${GITHUB_REPO} --body '${URL}'
  gh variable set PRODUCTION_URL --repo ${GITHUB_REPO} --body '${URL}'
  gh variable set AIMART_NETWORK_MODE --repo ${GITHUB_REPO} --body 'testnet'
  gh variable set AIMART_PAY_TO_EVM --repo ${GITHUB_REPO} --body '0x...'

Next: create the 'production' environment and the budgets (deploy/README.md), then run the
Deploy workflow or merge a release PR.
EOF
