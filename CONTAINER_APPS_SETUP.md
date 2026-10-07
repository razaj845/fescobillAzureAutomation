# FESCO Bill Bot — Azure Container Apps Setup
## Architecture 1 (Container Apps edition) — under $6/year on $100 budget

---

## How it works

```
Telegram
   ↕ webhook (HTTPS, Telegram pushes messages to the container)
Azure Container Apps  ──→  Azure Storage Queue  ←── PC workers poll every 15s
(bot logic only,                (job queue)            (Playwright + scraping)
 no browser)                                          Bill sent directly to user
   ↕ read/write
Azure File Share
(bot.db, bill_history.json)
```

**No VM. No always-on compute billing.** The Container App scales to zero when idle — you only pay for the fraction of a second it takes to process each message. Workers on your PCs talk directly to Azure Storage, not to the Container App.

---

## Cost breakdown (12 months)

| Resource | Free tier | You use | Monthly cost |
|---|---|---|---|
| Container App compute | 180K vCPU-s/month free | ~5K vCPU-s/month | **$0** |
| Container App memory | 360K GiB-s/month free | ~10K GiB-s/month | **$0** |
| Azure Storage Queue | — | ~500K ops/month | **~$0.23** |
| Azure File Share (5 GiB) | — | ~1 GiB | **~$0.06** |
| Azure Storage Table | — | minimal | **~$0.01** |
| GitHub Container Registry | 500 MB free | ~200 MB | **$0** |
| **Total** | | | **~$0.30/month = ~$3.60/year** |

**$100 student credit ÷ $0.30/month = 333 months of runtime.** You will never run out.

---

## Prerequisites

Install these on your local machine before starting:

```bash
# 1. Azure CLI
# Windows: https://aka.ms/installazurecliwindows
# Or: winget install Microsoft.AzureCLI

# 2. Docker Desktop
# https://www.docker.com/products/docker-desktop

# 3. Verify both work
az --version
docker --version
```

Create a free GitHub account at github.com if you don't have one.

---

## Part 1 — One-time Azure setup (20 minutes)

### Option A — Automated (recommended)

1. Open `cloud/deploy.sh` in a text editor.
2. Fill in the **FILL THESE IN** section at the top:
   ```bash
   GITHUB_USERNAME="your-github-username"
   GITHUB_TOKEN="ghp_..."          # GitHub PAT — see step below
   TELEGRAM_BOT_TOKEN="123:ABC..." # your bot token from @BotFather
   ALLOWED_USER_ID="123456789"     # your Telegram user ID
   ```
3. **Create a GitHub PAT** (Personal Access Token):
   - Go to github.com → Settings → Developer settings → Personal access tokens → Tokens (classic)
   - Click **Generate new token (classic)**
   - Select scope: `write:packages`
   - Copy the token and paste into `GITHUB_TOKEN=` above
4. Log in to Azure:
   ```bash
   az login
   ```
5. Run the deploy script from the project root:
   ```bash
   bash cloud/deploy.sh
   ```
6. The script prints a summary at the end. **Save the output** — it contains your `AZURE_STORAGE_CONNECTION_STRING` and `WORKER_API_KEY` which you need for each PC.

### Option B — Manual (step by step in Azure Portal)

If you prefer the Portal over CLI:

**1. Create a Resource Group**
- Portal → Resource groups → Create
- Name: `fescobill-rg`, Region: UAE North

**2. Create a Storage Account**
- Portal → Storage accounts → Create
- Name: `fescobillstorage` (must be globally unique, all lowercase)
- Region: UAE North, Redundancy: LRS
- After creation → go to **Access keys** → copy **Connection string**

**3. Create a Storage Queue**
- Storage account → Queues → + Queue
- Name: `fescobill-jobs`

**4. Create a File Share**
- Storage account → File shares → + File share
- Name: `fescobill-data`, Quota: 5 GiB

**5. Build and push the Docker image**
```bash
# From project root
docker build -f cloud/Dockerfile -t ghcr.io/YOUR_GITHUB_USERNAME/fescobill-bot:latest .
echo YOUR_GITHUB_PAT | docker login ghcr.io -u YOUR_GITHUB_USERNAME --password-stdin
docker push ghcr.io/YOUR_GITHUB_USERNAME/fescobill-bot:latest
```

**6. Create Container Apps environment**
- Portal → Container Apps → Create → Container Apps Environment
- Name: `fescobill-env`, Region: UAE North

**7. Create the Container App**
- Portal → Container Apps → Create
- Name: `fescobill-bot`
- Environment: `fescobill-env`
- Image: `ghcr.io/YOUR_GITHUB_USERNAME/fescobill-bot:latest`
- CPU: 0.25, Memory: 0.5 GiB
- Min replicas: 0, Max replicas: 1
- Ingress: Enabled, External, Port 8080
- Add environment variables (see `.env` section below)
- Add volume mount: Azure Files → `fescobill-data` → mount at `/mnt/fescobill-data`

---

## Part 2 — Container App environment variables

Set these in the Container App (Portal → Container App → Containers → Edit):

```
CLOUD_MODE                        = 1
AZURE_STORAGE_CONNECTION_STRING   = DefaultEndpointsProtocol=https;AccountName=...
AZURE_QUEUE_NAME                  = fescobill-jobs
TELEGRAM_BOT_TOKEN                = your-bot-token
CLOUDFLARE_WORKER_URL             = https://your-worker.workers.dev
ALLOWED_USER_ID                   = your-telegram-id
WORKER_API_KEY                    = (generate: python -c "import secrets; print(secrets.token_hex(32))")
WEBHOOK_URL                       = https://fescobill-bot.HASH.uaenorth.azurecontainerapps.io
BOT_DB_FILE                       = /mnt/fescobill-data/bot.db
BOT_LOG_FILE                      = /mnt/fescobill-data/bot.log
ACTIVITY_LOG_FILE                 = /mnt/fescobill-data/activity.log
BILL_HISTORY_FILE                 = /mnt/fescobill-data/bill_history.json
```

> `WEBHOOK_URL` is the URL shown on the Container App's overview page.
> Do NOT include a trailing slash.

---

## Part 3 — Set up each PC worker (5 minutes per PC)

Each PC keeps its existing fescobill setup. The worker runs alongside it.

### 3.1 Create `worker\worker.env`

```
copy worker\worker.env.example worker\worker.env
```

Open `worker\worker.env` and fill in:
```env
AZURE_STORAGE_CONNECTION_STRING=DefaultEndpointsProtocol=https;AccountName=fescobillstorage;...
WORKER_NAME=PC-Office     ← change this on every PC (PC-Office, PC-Bedroom, etc.)
```

The connection string is the same one from the Storage Account — all PCs use the same one.

### 3.2 Run the installer

```batch
worker\install_worker.bat
```

This:
1. Installs `azure-storage-queue` and `azure-data-tables` via pip
2. Creates a Task Scheduler task that starts the worker 2 minutes after login
3. Starts the worker immediately

### 3.3 Confirm it works

Open `logs\worker.log`:
```
Worker 'PC-Office' v2.0.0 starting
Startup delay: 120s...
Azure Storage Queue and Table clients initialised.
✅ First heartbeat sent — worker is online and visible to the bot.
```

On Telegram: `🟢 Worker 'PC-Office' came online.`

---

## Part 4 — Test end-to-end

```
/status        →  ☁️ Cloud mode — Workers online: 1 (PC-Office) — Jobs in queue: 0
/workers       →  🟢 1 worker online: PC-Office  last seen: ...
/getbillbyref 123456789012
               →  ⏳ Queued — 1 worker PC(s) available. You'll receive your bill shortly.
               →  [15 seconds later] ✅ Script finished successfully! [bill image arrives]
/health        →  full status including worker count and queue depth
```

---

## File placement — complete picture

```
fescobill\                         ← your project root on each PC
│
├── .env                           ← unchanged (Telegram, Gmail, WhatsApp credentials)
│
├── src\
│   ├── db.py                      ← replace with version from fescobill_cloud.zip
│   ├── bot_listener.py            ← replace with version from fescobill_cloud.zip
│   ├── job_queue.py               ← NEW — from fescobill_aca.zip
│   ├── worker_table.py            ← NEW — from fescobill_aca.zip
│   └── fesco_bill_automation.py   ← unchanged (stays on PCs only)
│
├── worker\
│   ├── worker.py                  ← NEW — from fescobill_aca.zip
│   ├── worker.env                 ← create from worker.env.example
│   ├── worker.env.example         ← NEW — from fescobill_aca.zip
│   ├── install_worker.bat         ← NEW — from fescobill_aca.zip (run once)
│   └── requirements.worker.txt   ← NEW — from fescobill_aca.zip
│
├── cloud\
│   ├── Dockerfile                 ← NEW — from fescobill_aca.zip
│   ├── requirements.cloud.txt     ← NEW — from fescobill_aca.zip
│   └── deploy.sh                  ← NEW — from fescobill_aca.zip (run once)
│
└── config\
    └── config.json                ← unchanged
```

**On Azure Container Apps — only these files are needed:**
```
src/db.py
src/bot_listener.py
src/job_queue.py
src/worker_table.py
config/config.json    (copy from your PC)
```
`fesco_bill_automation.py` is **NOT** on the Container App. It never scrapes.

---

## Updating the bot after code changes

```bash
# From your project root
bash cloud/deploy.sh --update
```

This rebuilds the Docker image, pushes it, and tells the Container App to use the new version. Workers on PCs update by replacing their local files — no reinstall needed.

---

## If something goes wrong

**Container App logs:**
```bash
az containerapp logs show --name fescobill-bot --resource-group fescobill-rg --follow
```

**Restart the Container App:**
```bash
az containerapp revision restart --name fescobill-bot --resource-group fescobill-rg --revision LATEST
```

**Worker not connecting:**
- Check `logs\worker.log` on the PC
- Verify `AZURE_STORAGE_CONNECTION_STRING` is identical in `worker\worker.env` and the Container App
- Check the Storage Queue exists: Portal → Storage Account → Queues

**Bot not responding on Telegram:**
- Check `WEBHOOK_URL` is set correctly (no trailing slash, starts with `https://`)
- Check `TELEGRAM_BOT_TOKEN` is correct
- Telegram webhook must be HTTPS — Container Apps provides this automatically
