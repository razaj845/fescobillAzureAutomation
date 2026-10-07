"""
job_queue.py  —  Azure Storage Queue wrapper for the cloud job queue.

Replaces the SQLite jobs table from the VM-based design.
Both the Container App AND each PC worker talk to this queue directly
using the same AZURE_STORAGE_CONNECTION_STRING — no HTTP API needed.

The Container App creates jobs.
Workers claim and delete jobs by receiving and processing queue messages.
"""
from __future__ import annotations

import json
import logging
import os
import uuid
from datetime import datetime, timezone
from typing import Optional

log = logging.getLogger("fesco.job_queue")

QUEUE_NAME  = os.getenv("AZURE_QUEUE_NAME", "fescobill-jobs")
_CONN_STR   = ""   # set via init()


def init(connection_string: str) -> None:
    """Call once at startup with the Azure Storage connection string."""
    global _CONN_STR
    _CONN_STR = connection_string
    _ensure_queue()


def _client():
    from azure.storage.queue import QueueClient
    return QueueClient.from_connection_string(_CONN_STR, QUEUE_NAME)


def _ensure_queue() -> None:
    try:
        _client().create_queue()
        log.info("Storage Queue '%s' ready.", QUEUE_NAME)
    except Exception:
        pass   # already exists — that's fine


# --------------------------------------------------------------------------- #
# Container App side: create jobs
# --------------------------------------------------------------------------- #

def create_job(
    chat_id:   int,
    user_id:   int,
    user_name: str,
    title:     str,
    cmd_args:  str,   # JSON-encoded list of CLI args
) -> str:
    """Enqueue a new job.  Returns the job UUID."""
    job_id = str(uuid.uuid4())
    job = {
        "id":         job_id,
        "chat_id":    chat_id,
        "user_id":    user_id,
        "user_name":  user_name,
        "title":      title,
        "cmd_args":   cmd_args,
        "created_at": datetime.now(timezone.utc).isoformat(),
    }
    _client().send_message(json.dumps(job))
    log.info("Job %s queued: %s", job_id[:8], title)
    return job_id


def get_pending_count() -> int:
    """Approximate number of jobs waiting in the queue."""
    try:
        props = _client().get_queue_properties()
        return props.approximate_message_count or 0
    except Exception:
        return 0


# --------------------------------------------------------------------------- #
# Worker side: claim and complete jobs
# --------------------------------------------------------------------------- #

def claim_job(visibility_timeout: int = 600) -> Optional[dict]:
    """
    Pop the next job off the queue.
    Returns a dict with the job data PLUS internal fields needed to delete it:
      _msg_id, _pop_receipt
    The caller MUST call delete_job() after processing.
    visibility_timeout: seconds the message stays invisible to other workers
    while this worker processes it (default 10 min).
    """
    try:
        msgs = list(_client().receive_messages(
            max_messages=1,
            visibility_timeout=visibility_timeout,
        ))
    except Exception as e:
        log.warning("claim_job receive error: %s", e)
        return None

    if not msgs:
        return None

    msg = msgs[0]
    try:
        job = json.loads(msg.content)
    except (json.JSONDecodeError, Exception) as e:
        log.error("Corrupt queue message, deleting: %s", e)
        try:
            _client().delete_message(msg.id, msg.pop_receipt)
        except Exception:
            pass
        return None

    job["_msg_id"]      = msg.id
    job["_pop_receipt"] = msg.pop_receipt
    return job


def delete_job(job: dict) -> None:
    """Remove the job from the queue after successful processing."""
    try:
        _client().delete_message(job["_msg_id"], job["_pop_receipt"])
    except Exception as e:
        log.warning("delete_job failed (job may be re-delivered): %s", e)


def release_job(job: dict) -> None:
    """Make the job visible again immediately (e.g. worker is shutting down)."""
    try:
        _client().update_message(
            job["_msg_id"], job["_pop_receipt"],
            visibility_timeout=0,
        )
    except Exception as e:
        log.warning("release_job failed: %s", e)
