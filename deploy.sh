#!/usr/bin/env bash
# ─────────────────────────────────────────────────────────────────────────────
# deploy.sh — Build and deploy Team USA Hometown Signals API to Cloud Run
#
# Prerequisites:
#   - gcloud CLI authenticated: gcloud auth login
#   - Project configured:       gcloud config set project YOUR_PROJECT
#   - Artifact Registry repo:   gcloud artifacts repositories create ...
#   - Cloud Run API enabled:    gcloud services enable run.googleapis.com
#   - (Optional) Vertex AI:     gcloud services enable aiplatform.googleapis.com
#
# Usage:
#   chmod +x deploy.sh
#   ./deploy.sh                          # uses defaults
#   PROJECT_ID=my-project ./deploy.sh   # override project
#   DRY_RUN=true ./deploy.sh            # print commands without executing
# ─────────────────────────────────────────────────────────────────────────────

set -euo pipefail

# ─── Configurable defaults (override via env vars) ───────────────────────────
PROJECT_ID="${PROJECT_ID:-$(gcloud config get-value project 2>/dev/null || echo "")}"
REGION="${REGION:-us-central1}"
VERTEX_LOCATION="${VERTEX_LOCATION:-global}"
GEMINI_MODEL="${GEMINI_MODEL:-gemini-3-flash-preview}"
SERVICE_NAME="${SERVICE_NAME:-hometown-signals-api}"
IMAGE_REPO="${IMAGE_REPO:-${REGION}-docker.pkg.dev/${PROJECT_ID}/team-usa/${SERVICE_NAME}}"
IMAGE_TAG="${IMAGE_TAG:-latest}"
MIN_INSTANCES="${MIN_INSTANCES:-0}"
MAX_INSTANCES="${MAX_INSTANCES:-10}"
MEMORY="${MEMORY:-512Mi}"
CPU="${CPU:-1}"
CONCURRENCY="${CONCURRENCY:-80}"
TIMEOUT="${TIMEOUT:-30s}"
DRY_RUN="${DRY_RUN:-false}"

# ─── Optional: Gemini API key (set as Cloud Run secret) ──────────────────────
# If set, this will be passed as an environment variable to the Cloud Run service.
# Prefer using Vertex AI with ADC for production (GEMINI_API_KEY not needed).
GEMINI_API_KEY=""

# ─────────────────────────────────────────────────────────────────────────────
IMAGE="${IMAGE_REPO}:${IMAGE_TAG}"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

echo ""
echo "══════════════════════════════════════════════════════════"
echo " Team USA Hometown Signals API — Cloud Run Deploy"
echo "══════════════════════════════════════════════════════════"
echo " Project:  ${PROJECT_ID}"
echo " Region:   ${REGION}"
echo " Service:  ${SERVICE_NAME}"
echo " Image:    ${IMAGE}"
echo " Dry run:  ${DRY_RUN}"
echo "══════════════════════════════════════════════════════════"
echo ""

if [[ -z "${PROJECT_ID}" ]]; then
  echo "ERROR: PROJECT_ID is not set."
  echo "  Set it via: export PROJECT_ID=your-gcp-project-id"
  echo "  Or: gcloud config set project your-gcp-project-id"
  exit 1
fi

run() {
  if [[ "${DRY_RUN}" == "true" ]]; then
    echo "[DRY RUN] $*"
  else
    "$@"
  fi
}

# ─── Step 1: Configure Docker for Artifact Registry ──────────────────────────
echo "▸ Configuring Docker authentication..."
run gcloud auth configure-docker "${REGION}-docker.pkg.dev" --quiet

# ─── Step 2: Build Docker image ───────────────────────────────────────────────
echo "▸ Building Docker image..."
run docker build \
  --platform linux/amd64 \
  -t "${IMAGE}" \
  "${SCRIPT_DIR}"

# ─── Step 3: Push to Artifact Registry ────────────────────────────────────────
echo "▸ Pushing image to Artifact Registry..."
run docker push "${IMAGE}"

# ─── Step 4: Deploy to Cloud Run ─────────────────────────────────────────────
echo "▸ Deploying to Cloud Run..."

DEPLOY_ARGS=(
  gcloud run deploy "${SERVICE_NAME}"
  --image "${IMAGE}"
  --region "${REGION}"
  --platform managed
  --allow-unauthenticated
  --memory "${MEMORY}"
  --cpu "${CPU}"
  --min-instances "${MIN_INSTANCES}"
  --max-instances "${MAX_INSTANCES}"
  --concurrency "${CONCURRENCY}"
  --timeout "${TIMEOUT}"
  --set-env-vars "CORS_ALLOW_ALL_ORIGINS=false"
  --set-env-vars "LOG_LEVEL=INFO"
  --set-env-vars "GCP_PROJECT=${PROJECT_ID}"
  --set-env-vars "VERTEX_LOCATION=${VERTEX_LOCATION}"
  --set-env-vars "GEMINI_MODEL=${GEMINI_MODEL}"
  --set-env-vars "ENABLE_GEMINI_AI=true"
)

# Optional GEMINI_API_KEY
if [[ -n "${GEMINI_API_KEY}" ]]; then
  DEPLOY_ARGS+=(--set-env-vars "GEMINI_API_KEY=${GEMINI_API_KEY}")
  echo "  (GEMINI_API_KEY will be set as env var)"
fi

run "${DEPLOY_ARGS[@]}"

# ─── Step 5: Print service URL ───────────────────────────────────────────────
if [[ "${DRY_RUN}" != "true" ]]; then
  SERVICE_URL=$(gcloud run services describe "${SERVICE_NAME}" \
    --region "${REGION}" \
    --format "value(status.url)" 2>/dev/null || echo "(unavailable)")
  echo ""
  echo "══════════════════════════════════════════════════════════"
  echo " Deploy complete!"
  echo " Service URL: ${SERVICE_URL}"
  echo " Health:      ${SERVICE_URL}/health"
  echo " API docs:    ${SERVICE_URL}/docs"
  echo " Hubs list:   ${SERVICE_URL}/api/hometown/hubs"
  echo "══════════════════════════════════════════════════════════"
  echo ""
  echo "Frontend environment variable:"
  echo "  VITE_API_BASE_URL=${SERVICE_URL}"
  echo ""
fi
