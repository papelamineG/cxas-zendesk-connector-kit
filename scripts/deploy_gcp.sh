#!/usr/bin/env bash
# ==============================================================================
# deploy_gcp.sh — Turnkey Cloud Run & Secret Manager Deployment for CXAS Zendesk Connector
# ==============================================================================
set -euo pipefail

# Color formatting
RED='\033[0;31m'
GREEN='\033[0;32m'
BLUE='\033[0;34m'
YELLOW='\033[1;33m'
NC='\033[0m' # No Color

echo -e "${BLUE}=== CXAS Zendesk Connector — GCP Turnkey Deployment ===${NC}"

# 1. Load from .env if present
if [[ -f .env ]]; then
  echo -e "${BLUE}Loading environment from .env file...${NC}"
  # shellcheck disable=SC1091
  set -o allexport
  source .env
  set +o allexport
fi

# 2. Validation
GCP_PROJECT_ID="${GCP_PROJECT_ID:-}"
GCP_REGION="${GCP_REGION:-us-central1}"
CXAS_LOCATION="${CXAS_LOCATION:-us}"
CXAS_APP_ID="${CXAS_APP_ID:-}"
ZENDESK_SUBDOMAIN="${ZENDESK_SUBDOMAIN:-}"
ZENDESK_EMAIL="${ZENDESK_EMAIL:-}"
ZENDESK_API_TOKEN="${ZENDESK_API_TOKEN:-}"
ZENDESK_MOCK_MODE="${ZENDESK_MOCK_MODE:-false}"

if [[ -z "$GCP_PROJECT_ID" ]]; then
  echo -e "${RED}Error: GCP_PROJECT_ID is required.${NC}"
  echo "Set it in your environment or in a .env file:"
  echo "  export GCP_PROJECT_ID=\"your-project-id\""
  exit 1
fi

echo "Deploying to Project: ${GCP_PROJECT_ID}"
echo "Region:              ${GCP_REGION}"

# If Zendesk credentials are not provided, automatically switch to mock mode
if [[ -z "$ZENDESK_API_TOKEN" || -z "$ZENDESK_SUBDOMAIN" || -z "$ZENDESK_EMAIL" ]]; then
  echo -e "${YELLOW}Warning: Incomplete Zendesk credentials detected.${NC}"
  echo -e "${YELLOW}Defaulting ZENDESK_MOCK_MODE=true (Mock Specialist Simulator active).${NC}"
  ZENDESK_MOCK_MODE="true"
fi

# 3. Enable required Google Cloud APIs
echo -e "\n${BLUE}Enabling Google Cloud APIs...${NC}"
gcloud services enable \
  run.googleapis.com \
  secretmanager.googleapis.com \
  cloudbuild.googleapis.com \
  ces.googleapis.com \
  --project="${GCP_PROJECT_ID}"

# 4. Secret Manager for Zendesk API Token
SECRET_NAME="zendesk-api-token"
SECRET_ENV_FLAG=()

if [[ "$ZENDESK_MOCK_MODE" == "false" && -n "$ZENDESK_API_TOKEN" ]]; then
  echo -e "\n${BLUE}Configuring Secret Manager secret: ${SECRET_NAME}...${NC}"
  if ! gcloud secrets describe "${SECRET_NAME}" --project="${GCP_PROJECT_ID}" >/dev/null 2>&1; then
    echo "Creating secret ${SECRET_NAME}..."
    gcloud secrets create "${SECRET_NAME}" \
      --replication-policy="automatic" \
      --project="${GCP_PROJECT_ID}"
  fi

  echo "Adding secret version..."
  echo -n "${ZENDESK_API_TOKEN}" | gcloud secrets versions add "${SECRET_NAME}" \
    --data-file=- \
    --project="${GCP_PROJECT_ID}"

  SECRET_ENV_FLAG=(
    "--set-secrets" "ZENDESK_API_TOKEN=${SECRET_NAME}:latest"
  )
else
  echo -e "\n${YELLOW}Mock mode is active. Skipping Secret Manager secret creation.${NC}"
fi

# 5. Build environment variables list for Cloud Run
ENV_VARS="GCP_PROJECT_ID=${GCP_PROJECT_ID}"
ENV_VARS="${ENV_VARS},GCP_REGION=${GCP_REGION}"
ENV_VARS="${ENV_VARS},ZENDESK_MOCK_MODE=${ZENDESK_MOCK_MODE}"
if [[ -n "$ZENDESK_SUBDOMAIN" ]]; then
  ENV_VARS="${ENV_VARS},ZENDESK_SUBDOMAIN=${ZENDESK_SUBDOMAIN}"
fi
if [[ -n "$ZENDESK_EMAIL" ]]; then
  ENV_VARS="${ENV_VARS},ZENDESK_EMAIL=${ZENDESK_EMAIL}"
fi
if [[ -n "${APP_BRAND:-}" ]]; then
  ENV_VARS="${ENV_VARS},APP_BRAND=${APP_BRAND}"
fi
if [[ -n "${ZENDESK_ESCALATION_PRIORITY:-}" ]]; then
  ENV_VARS="${ENV_VARS},ZENDESK_ESCALATION_PRIORITY=${ZENDESK_ESCALATION_PRIORITY}"
fi

# 6. Deploy Cloud Run Service
SERVICE_NAME="cxas-zendesk-connector"
echo -e "\n${BLUE}Deploying Cloud Run service: ${SERVICE_NAME}...${NC}"

# Note: Pinning --max-instances=1 ensures stateful in-memory escalation poller consistency
# if distributed session persistence (Redis/Spanner) is not attached.
DEPLOY_CMD=(
  gcloud run deploy "${SERVICE_NAME}"
  --source .
  --region "${GCP_REGION}"
  --project "${GCP_PROJECT_ID}"
  --platform "managed"
  --allow-unauthenticated
  --set-env-vars "${ENV_VARS}"
  --max-instances 1
  --min-instances 0
  --cpu 1
  --memory 512Mi
  --timeout 300
  --quiet
)

if [[ ${#SECRET_ENV_FLAG[@]} -gt 0 ]]; then
  DEPLOY_CMD+=("${SECRET_ENV_FLAG[@]}")
fi

"${DEPLOY_CMD[@]}"

# 7. Retrieve Service URL
SERVICE_URL=$(gcloud run services describe "${SERVICE_NAME}" \
  --platform managed \
  --region "${GCP_REGION}" \
  --project "${GCP_PROJECT_ID}" \
  --format 'value(status.url)')

echo -e "\n${GREEN}================================================================${NC}"
echo -e "${GREEN}Deployment Successful!${NC}"
echo -e "${GREEN}Cloud Run Service URL:${NC} ${SERVICE_URL}"
echo -e "${GREEN}MCP Server Endpoint:${NC}   ${SERVICE_URL}/mcp"
echo -e "${GREEN}Relay API Endpoint:${NC}    ${SERVICE_URL}/api"
echo -e "${GREEN}Health Endpoint:${NC}       ${SERVICE_URL}/api/health"
echo -e "${GREEN}================================================================${NC}"

# 8. Output Next Step: Setup CXAS
echo -e "\n${BLUE}Next Step: Wire CX Agent Studio tools and instructions:${NC}"
if [[ -n "$CXAS_APP_ID" ]]; then
  echo -e "Run the automated CXAS configuration script:\n"
  echo -e "  ${YELLOW}python3 scripts/setup_cxas.py --project-id ${GCP_PROJECT_ID} --location ${CXAS_LOCATION} --app-id ${CXAS_APP_ID} --backend-url ${SERVICE_URL}${NC}\n"
else
  echo -e "Run the automated CXAS configuration script with your app ID:\n"
  echo -e "  ${YELLOW}python3 scripts/setup_cxas.py --project-id ${GCP_PROJECT_ID} --location ${CXAS_LOCATION} --app-id <CXAS_APP_ID> --backend-url ${SERVICE_URL}${NC}\n"
fi
