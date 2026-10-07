#!/usr/bin/env python3
"""
worker.py  —  Local PC worker for Architecture 1 (Container Apps edition).

Polls Azure Storage Queue for pending jobs, runs fesco_bill_automation.py
locally (Playwright + browser), and the automation script sends the bill
directly to the user on Telegram.  No HTTP connection to the Container App
is required — workers only talk to Azure Storage.

Configuration in worker/worker.env:
  AZURE_STORAGE_CONNECTION_STRING = DefaultEndpointsProtocol=https;...
  WORKER_NAME                     = PC-Office   (unique per PC)
  STARTUP_DELAY                   = 120         (seconds after boot, optional)
"""
import json
import logging
import logging.handlers
import os
import subprocess
import sys
import time
from pathlib import Path

from dotenv import load_dotenv

WORKER_DIR   = Path(__file__).resolve().parent
PROJECT_ROOT = WORKER_DIR.parent

load_dotenv(WORKER_DIR / "worker.env")
load_dotenv(PROJECT_ROOT / ".env")

# ── Config ────────────────────────────────────────────────────────────────────
VERSION           = "2.0.0"
AZURE_CONN_STR    = os.getenv("AZURE_STORAGE_CONNECTION_STRING", "")
WORKER_NAME       = os.getenv("WORKER_NAME") or os.environ.get("COMPUTERNAME", "unknown-pc")
AUTOMATION_SCRIPT = (
    os.getenv("AUTOMATION_SCRIPT")
    or str(PROJECT_ROOT / "src" / "fesco_bill_automation.py")
)
STARTUP_DELAY_S   = int(os.getenv("STARTUP_DELAY",          "120"))
POLL_INTERVAL_S   = int(os.getenv("POLL_INTERVAL",          "15"))
HEARTBEAT_S       = int(os.getenv("HEARTBEAT_INTERVAL",     "30"))
JOB_TIMEOUT_S     = int(os.getenv("JOB_TIMEOUT",           "3600"))
VISIBILITY_TIMEOUT = int(os.getenv("VISIBILITY_TIMEOUT",   "600"))  # 10 min

# ── Logging ───────────────────────────────────────────────────────────────────
LOG_FILE = PROJECT_ROOT / "logs" / "worker.log"
LOG_FILE.parent.mkdir(parents=True, exist_ok=True)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    handlers=[
        logging.StreamHandler(sys.stdout),
        logging.handlers.RotatingFileHandler(
            LOG_FILE, maxBytes=1_000_000, backupCount=3, encoding="utf-8"
        ),
    ],
)
log = logging.getLogger("fesco.worker")

# ── Azure clients (lazy init after config is validated) ───────────────────────
_queue_client  = None
_table_client  = None


def _init_azure() -> None:
    global _queue_client, _table_client
    from azure.storage.queue import QueueClient
    from azure.data.tables  import TableServiceClient

    QUEUE_NAME = os.getenv("AZURE_QUEUE_NAME", "fescobill-jobs")
    TABLE_NAME = "fescobillworkers"

    _queue_client = QueueClient.from_connection_string(AZURE_CONN_STR, QUEUE_NAME)
    _table_client = (
        TableServiceClient.from_connection_string(AZURE_CONN_STR)
        .get_table_client(TABLE_NAME)
    )

    # Ensure queue and table exist (idempotent)
    try:
        _queue_client.create_queue()
    except Exception:
        pass
    try:
        TableServiceClient.from_connection_string(AZURE_CONN_STR).create_table(TABLE_NAME)
    except Exception:
        pass

    log.info("Azure Storage Queue and Table clients initialised.")


# ── Heartbeat ─────────────────────────────────────────────────────────────────

def send_heartbeat() -> bool:
    try:
        import socket
        ip = socket.gethostbyname(socket.gethostname())
    except Exception:
        ip = ""
    try:
        from datetime import datetime, timezone
        _table_client.upsert_entity({
            "PartitionKey":   "workers",
            "RowKey":         WORKER_NAME,
            "last_heartbeat": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "ip":             ip,
            "version":        VERSION,
        })
        return True
    except Exception as e:
        log.warning("Heartbeat failed: %s", e)
        return False


# ── Job queue ─────────────────────────────────────────────────────────────────

def claim_job() -> dict | None:
    try:
        msgs = list(_queue_client.receive_messages(
            max_messages=1,
            visibility_timeout=VISIBILITY_TIMEOUT,
        ))
    except Exception as e:
        log.warning("Queue receive error: %s", e)
        return None

    if not msgs:
        return None

    msg = msgs[0]
    try:
        job              = json.loads(msg.content)
        job["_msg_id"]   = msg.id
        job["_pop_receipt"] = msg.pop_receipt
        return job
    except Exception as e:
        log.error("Corrupt queue message, deleting: %s", e)
        try:
            _queue_client.delete_message(msg.id, msg.pop_receipt)
        except Exception:
            pass
        return None


def delete_job(job: dict) -> None:
    try:
        _queue_client.delete_message(job["_msg_id"], job["_pop_receipt"])
    except Exception as e:
        log.warning("delete_job failed — job may be re-delivered after visibility timeout: %s", e)


def release_job(job: dict) -> None:
    """Make the job visible to other workers immediately (e.g. on clean shutdown)."""
    try:
        _queue_client.update_message(
            job["_msg_id"], job["_pop_receipt"], visibility_timeout=0
        )
    except Exception:
        pass


# ── Job execution ─────────────────────────────────────────────────────────────

def run_job(job: dict) -> tuple[str, str]:
    try:
        cmd_args = json.loads(job.get("cmd_args") or "[]")
    except (json.JSONDecodeError, TypeError):
        return "Script failed with error:", "Invalid cmd_args in job."

    cmd = [sys.executable, AUTOMATION_SCRIPT] + cmd_args
    log.info("Running job %s: '%s'", job.get("id","")[:8], job.get("title",""))
    log.info("Command: %s", " ".join(cmd))

    try:
        proc = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            errors="replace",
            timeout=JOB_TIMEOUT_S,
            cwd=PROJECT_ROOT,
        )
        stdout = (proc.stdout or "").strip()
        stderr = (proc.stderr or "").strip()

        if proc.returncode == 0:
            return "Script finished successfully!", _trim(stdout or "No output.")
        return "Script failed with error:", _trim(stderr or stdout or "Unknown error.")

    except subprocess.TimeoutExpired:
        return "Script timed out.", f"No result after {JOB_TIMEOUT_S // 60} minutes."
    except Exception as e:
        return "Script could not launch.", str(e)


def _trim(text: str, max_len: int = 3000) -> str:
    text = text.strip()
    return text[-max_len:].lstrip() if len(text) > max_len else text


# ── Main loop ─────────────────────────────────────────────────────────────────

def _validate() -> list[str]:
    problems = []
    if not AZURE_CONN_STR:
        problems.append("AZURE_STORAGE_CONNECTION_STRING not set in worker.env")
    if not Path(AUTOMATION_SCRIPT).is_file():
        problems.append(f"AUTOMATION_SCRIPT not found: {AUTOMATION_SCRIPT}")
    return problems


def main() -> None:
    problems = _validate()
    if problems:
        for p in problems:
            print(f"ERROR: {p}")
        sys.exit(1)

    log.info("Worker '%s' v%s starting", WORKER_NAME, VERSION)
    log.info("Script: %s", AUTOMATION_SCRIPT)

    if STARTUP_DELAY_S > 0:
        log.info("Startup delay: %ds (waiting for network and OneDrive sync)…", STARTUP_DELAY_S)
        time.sleep(STARTUP_DELAY_S)

    _init_azure()

    # Announce online
    for attempt in range(1, 6):
        if send_heartbeat():
            log.info("✅ First heartbeat sent — worker is online and visible to the bot.")
            break
        log.warning("Heartbeat failed (attempt %d/5), retrying in 15s…", attempt)
        time.sleep(15)
    else:
        log.error("Could not send heartbeat after 5 attempts. Check AZURE_STORAGE_CONNECTION_STRING.")
        sys.exit(1)

    last_heartbeat_t = time.time()
    current_job      = None

    try:
        while True:
            now = time.time()

            # Periodic heartbeat
            if now - last_heartbeat_t >= HEARTBEAT_S:
                send_heartbeat()
                last_heartbeat_t = time.time()

            # Try to claim a job from the queue
            job = claim_job()

            if not job:
                time.sleep(POLL_INTERVAL_S)
                continue

            current_job = job
            job_short   = job.get("id", "")[:8]
            log.info("Claimed job %s: '%s'", job_short, job.get("title", ""))

            result_status, result_output = run_job(job)
            is_ok = "successfully" in result_status.lower()

            if is_ok:
                log.info("Job %s completed: %s", job_short, result_status)
            else:
                log.warning("Job %s failed: %s", job_short, result_status)

            # Delete from queue — job is done (result was sent to Telegram by the automation script)
            delete_job(job)
            current_job      = None
            last_heartbeat_t = time.time()   # reset after completing a job

    except KeyboardInterrupt:
        log.info("Shutting down…")
        if current_job:
            log.info("Releasing current job back to queue for another worker.")
            release_job(current_job)
        sys.exit(0)


if __name__ == "__main__":
    main()
