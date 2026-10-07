"""
worker_table.py  —  Azure Storage Table for worker PC heartbeats.

Workers write their own row every 30 seconds.
The Container App reads the table for /workers and /status commands.

Table: fescobillworkers
  PartitionKey = "workers"
  RowKey       = worker name (e.g. "PC-Office")
  Fields       = last_heartbeat (ISO-8601), ip, version
"""
from __future__ import annotations

import logging
import os
from datetime import datetime, timedelta, timezone
from typing import Optional

log = logging.getLogger("fesco.worker_table")

TABLE_NAME = "fescobillworkers"
_CONN_STR  = ""


def init(connection_string: str) -> None:
    global _CONN_STR
    _CONN_STR = connection_string
    _ensure_table()


def _service():
    from azure.data.tables import TableServiceClient
    return TableServiceClient.from_connection_string(_CONN_STR)


def _table():
    return _service().get_table_client(TABLE_NAME)


def _ensure_table() -> None:
    try:
        _service().create_table(TABLE_NAME)
        log.info("Storage Table '%s' ready.", TABLE_NAME)
    except Exception:
        pass   # already exists


# --------------------------------------------------------------------------- #
# Worker side: update own heartbeat
# --------------------------------------------------------------------------- #

def update_heartbeat(name: str, ip: str = "", version: str = "") -> None:
    try:
        _table().upsert_entity({
            "PartitionKey":    "workers",
            "RowKey":          name,
            "last_heartbeat":  datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "ip":              ip or "",
            "version":         version or "",
        })
    except Exception as e:
        log.warning("update_heartbeat failed: %s", e)


# --------------------------------------------------------------------------- #
# Container App side: read worker status
# --------------------------------------------------------------------------- #

def get_online_workers(timeout_minutes: int = 3) -> list[dict]:
    """Return workers that sent a heartbeat within the last timeout_minutes."""
    cutoff = (
        datetime.now(timezone.utc) - timedelta(minutes=timeout_minutes)
    ).isoformat(timespec="seconds")
    try:
        entities = _table().query_entities(
            f"PartitionKey eq 'workers' and last_heartbeat ge '{cutoff}'"
        )
        return [
            {
                "name":           e["RowKey"],
                "last_heartbeat": e.get("last_heartbeat", ""),
                "ip":             e.get("ip", ""),
                "version":        e.get("version", ""),
            }
            for e in entities
        ]
    except Exception as e:
        log.warning("get_online_workers failed: %s", e)
        return []


def count_online_workers(timeout_minutes: int = 3) -> int:
    return len(get_online_workers(timeout_minutes))


def was_recently_online(name: str, within_minutes: int = 4) -> bool:
    cutoff = (
        datetime.now(timezone.utc) - timedelta(minutes=within_minutes)
    ).isoformat(timespec="seconds")
    try:
        entity = _table().get_entity("workers", name)
        return entity.get("last_heartbeat", "") >= cutoff
    except Exception:
        return False
