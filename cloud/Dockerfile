# ─────────────────────────────────────────────────────────────────────────────
# FESCO Bill Bot — Cloud Brain
# Azure Container Apps image (NO Playwright, NO browser)
# ─────────────────────────────────────────────────────────────────────────────
FROM python:3.12-slim

# Keeps Python from generating .pyc files and enables unbuffered output
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

WORKDIR /app

# Install Python dependencies
COPY cloud/requirements.cloud.txt requirements.txt
RUN pip install --no-cache-dir -r requirements.txt

# Copy only the files the Container App needs
# (fesco_bill_automation.py is NOT copied — the VM never scrapes)
COPY src/db.py          src/
COPY src/bot_listener.py src/
COPY src/job_queue.py   src/
COPY src/worker_table.py src/

# Runtime directories — these will be overlaid by Azure Files mounts at runtime
# (data/ for bot.db and bill_history.json, logs/ for bot.log)
RUN mkdir -p data logs config

# Container Apps maps PORT env var; Flask will listen on it
EXPOSE 8080

CMD ["python", "src/bot_listener.py"]
