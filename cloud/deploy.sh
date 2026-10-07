#!/bin/bash
# deploy.sh — No Docker required. Azure builds the image in the cloud.
# Run once from your project root: bash cloud/deploy.sh
set -euo pipefail

# ── FILL THESE IN ─────────────────────────────────────────────────────────────
TELEGRAM_BOT_TOKEN="YOUR_TELEGRAM_BOT_TOKEN"
ALLOWED_USER_ID="YOUR_TELEGRAM_ID"
CLOUDFLARE_WORKER_URL=""           # leave blank if not using Cloudflare Worker

# ── Resource names (change only if you want something different) ───────────────
RESOURCE_GROUP="fescobill-rg"
LOCATION="uaenorth"
STORAGE_ACCOUNT="fescobillstorage" # all lowercase, 3-24 chars, globally unique
CONTAINER_ENV="fescobill-env"
CONTAINER_APP="fescobill-bot"
QUEUE_NAME="fescobill-jobs"
FILESHARE_NAME="fescobill-data"
# ─────────────────────────────────────────────────────────────────────────────

UPDATE_ONLY=${1:-""}

echo "═══════════════════════════════════════════════════"
echo "  FESCO Bot — Azure Container Apps deploy"
echo "  No Docker needed — image built in Azure"
echo "═══════════════════════════════════════════════════"

echo "[0] Checking Azure login..."
az account show --query "name" -o tsv || { echo "Run: az login"; exit 1; }

# ── UPDATE MODE: just rebuild and redeploy ─────────────────────────────────────
if [[ "$UPDATE_ONLY" == "--update" ]]; then
    echo "UPDATE MODE — rebuilding image in Azure..."
    az containerapp up \
        --name "$CONTAINER_APP" \
        --resource-group "$RESOURCE_GROUP" \
        --source . \
        --dockerfile cloud/Dockerfile
    echo "✅ Update done."
    exit 0
fi

# ── FULL DEPLOY ────────────────────────────────────────────────────────────────
echo ""
echo "[1/6] Creating resource group..."
az group create --name "$RESOURCE_GROUP" --location "$LOCATION" -o table

echo ""
echo "[2/6] Creating storage account..."
az storage account create \
    --name "$STORAGE_ACCOUNT" \
    --resource-group "$RESOURCE_GROUP" \
    --location "$LOCATION" \
    --sku Standard_LRS \
    -o table

AZURE_CONN_STR=$(az storage account show-connection-string \
    --name "$STORAGE_ACCOUNT" --resource-group "$RESOURCE_GROUP" \
    --query connectionString -o tsv)

STORAGE_KEY=$(az storage account keys list \
    --account-name "$STORAGE_ACCOUNT" --resource-group "$RESOURCE_GROUP" \
    --query "[0].value" -o tsv)

echo ""
echo "[3/6] Creating Storage Queue and File Share..."
az storage queue create  --name "$QUEUE_NAME"    --connection-string "$AZURE_CONN_STR" -o table
az storage share create  --name "$FILESHARE_NAME" --connection-string "$AZURE_CONN_STR" --quota 5 -o table

echo ""
echo "[4/6] Creating Container Apps environment with Azure Files mount..."
az containerapp env create \
    --name "$CONTAINER_ENV" \
    --resource-group "$RESOURCE_GROUP" \
    --location "$LOCATION" \
    -o table

az containerapp env storage set \
    --name "$CONTAINER_ENV" \
    --resource-group "$RESOURCE_GROUP" \
    --storage-name fescobill-data \
    --azure-file-account-name "$STORAGE_ACCOUNT" \
    --azure-file-account-key "$STORAGE_KEY" \
    --azure-file-share-name "$FILESHARE_NAME" \
    --access-mode ReadWrite \
    -o table

echo ""
echo "[5/6] Building image in Azure and deploying..."
echo "      (This takes 3-5 minutes — Azure builds your image in the cloud)"

WORKER_API_KEY=$(python3 -c "import secrets; print(secrets.token_hex(32))")

az containerapp up \
    --name "$CONTAINER_APP" \
    --resource-group "$RESOURCE_GROUP" \
    --location "$LOCATION" \
    --environment "$CONTAINER_ENV" \
    --source . \
    --dockerfile cloud/Dockerfile \
    --ingress external \
    --target-port 8080 \
    --env-vars \
        "CLOUD_MODE=1" \
        "AZURE_STORAGE_CONNECTION_STRING=$AZURE_CONN_STR" \
        "AZURE_QUEUE_NAME=$QUEUE_NAME" \
        "TELEGRAM_BOT_TOKEN=$TELEGRAM_BOT_TOKEN" \
        "CLOUDFLARE_WORKER_URL=$CLOUDFLARE_WORKER_URL" \
        "ALLOWED_USER_ID=$ALLOWED_USER_ID" \
        "WORKER_API_KEY=$WORKER_API_KEY" \
        "BOT_DB_FILE=/mnt/fescobill-data/bot.db" \
        "BOT_LOG_FILE=/mnt/fescobill-data/bot.log" \
        "ACTIVITY_LOG_FILE=/mnt/fescobill-data/activity.log" \
        "BILL_HISTORY_FILE=/mnt/fescobill-data/bill_history.json"

echo ""
echo "[6/6] Getting app URL and setting webhook..."
APP_URL=$(az containerapp show \
    --name "$CONTAINER_APP" --resource-group "$RESOURCE_GROUP" \
    --query "properties.configuration.ingress.fqdn" -o tsv)

az containerapp update \
    --name "$CONTAINER_APP" --resource-group "$RESOURCE_GROUP" \
    --set-env-vars "WEBHOOK_URL=https://$APP_URL" \
    -o table

# ── Apply volume mount (File Share) ───────────────────────────────────────────
az containerapp update \
    --name "$CONTAINER_APP" --resource-group "$RESOURCE_GROUP" \
    --volume-mounts "[{\"volumeName\":\"fescobill-data\",\"mountPath\":\"/mnt/fescobill-data\"}]" \
    --volumes "[{\"name\":\"fescobill-data\",\"storageType\":\"AzureFile\",\"storageName\":\"fescobill-data\"}]" \
    -o table

echo ""
echo "═══════════════════════════════════════════════════"
echo "  ✅ Deployment complete!"
echo "  App URL: https://$APP_URL"
echo ""
echo "  SAVE THESE for worker.env on each PC:"
echo "  AZURE_STORAGE_CONNECTION_STRING=$AZURE_CONN_STR" | cut -c1-80
echo "  WORKER_API_KEY=$WORKER_API_KEY"
echo "═══════════════════════════════════════════════════"
