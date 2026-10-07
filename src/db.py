"""
db.py  —  SQLite data store for the FESCO bill bot.

Replaces three JSON flat-files (access.json, saved_refs.json, usage.json)
with a single WAL-mode database that handles concurrent writes safely.

Extra tables added for new features:
  subscriptions  — users who opted in to automatic /run notifications
  payments       — user-logged payment events (/paid command)

Call db.init(path) once at startup, then use the functions below.
All writes are serialised through a module-level lock so the bot's
many handler threads never corrupt data.
"""
from __future__ import annotations

import json
import logging
import sqlite3
import threading
import time
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Any, Optional

_db_path: Optional[Path] = None
_lock = threading.Lock()
log = logging.getLogger("fesco.db")


# --------------------------------------------------------------------------- #
# Initialisation & schema
# --------------------------------------------------------------------------- #

def init(path: Path) -> None:
    """Open (or create) the database and apply the schema. Call once at startup."""
    global _db_path
    _db_path = Path(path)
    _db_path.parent.mkdir(parents=True, exist_ok=True)
    with _open() as conn:
        _apply_schema(conn)


def _open() -> sqlite3.Connection:
    conn = sqlite3.connect(str(_db_path), check_same_thread=False, timeout=30)
    conn.execute("PRAGMA journal_mode=DELETE")
    conn.execute("PRAGMA busy_timeout=30000")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.row_factory = sqlite3.Row
    return conn


def _apply_schema(conn: sqlite3.Connection) -> None:
    conn.executescript("""
        -- Per-window global defaults (hour / day)
        CREATE TABLE IF NOT EXISTS access_defaults (
            window TEXT PRIMARY KEY,
            value  INTEGER
        );

        -- Users managed from Telegram (/adduser etc.)
        CREATE TABLE IF NOT EXISTS users (
            user_id          INTEGER PRIMARY KEY,
            name             TEXT,
            hour_limit       INTEGER,
            day_limit        INTEGER,
            hour_use_default INTEGER NOT NULL DEFAULT 1,
            day_use_default  INTEGER NOT NULL DEFAULT 1
        );

        CREATE TABLE IF NOT EXISTS user_commands (
            user_id INTEGER NOT NULL,
            command TEXT    NOT NULL,
            PRIMARY KEY (user_id, command)
        );

        -- refs restriction: row absent / restricted=0 → any ref allowed
        CREATE TABLE IF NOT EXISTS user_refs_restricted (
            user_id    INTEGER PRIMARY KEY,
            restricted INTEGER NOT NULL DEFAULT 0
        );
        CREATE TABLE IF NOT EXISTS user_allowed_refs (
            user_id INTEGER NOT NULL,
            ref     TEXT    NOT NULL,
            PRIMARY KEY (user_id, ref)
        );

        -- Rolling-window rate-limiting (replaces usage.json)
        CREATE TABLE IF NOT EXISTS usage (
            id        INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id   INTEGER NOT NULL,
            timestamp REAL    NOT NULL,
            count     INTEGER NOT NULL DEFAULT 1,
            token     TEXT
        );
        CREATE INDEX IF NOT EXISTS idx_usage_user_ts ON usage (user_id, timestamp);

        -- Per-user saved nicknames (replaces saved_refs.json)
        CREATE TABLE IF NOT EXISTS saved_refs (
            user_id  INTEGER NOT NULL,
            nickname TEXT    NOT NULL,
            ref      TEXT    NOT NULL,
            PRIMARY KEY (user_id, nickname)
        );

        -- NEW: users who subscribed to /run completion notices
        CREATE TABLE IF NOT EXISTS subscriptions (
            user_id INTEGER PRIMARY KEY
        );

        -- NEW: payment log (/paid command)
        CREATE TABLE IF NOT EXISTS payments (
            id      INTEGER PRIMARY KEY AUTOINCREMENT,
            ref     TEXT    NOT NULL,
            user_id INTEGER NOT NULL,
            paid_at TEXT    NOT NULL,
            note    TEXT
        );

        -- NEW: temporary access expiry (/adduser --expires)
        CREATE TABLE IF NOT EXISTS user_expiry (
            user_id    INTEGER PRIMARY KEY,
            expires_at TEXT NOT NULL          -- ISO-8601 datetime (UTC)
        );

        -- NEW: time-gated access (e.g. 09:00–22:00 only)
        CREATE TABLE IF NOT EXISTS user_allowed_hours (
            user_id     INTEGER PRIMARY KEY,
            hour_start  INTEGER NOT NULL,     -- 0-23 inclusive
            hour_end    INTEGER NOT NULL      -- 0-23 inclusive (end is exclusive)
        );

        -- NEW: rolling job-duration log for estimated-completion in /status
        CREATE TABLE IF NOT EXISTS job_durations (
            id       INTEGER PRIMARY KEY AUTOINCREMENT,
            title    TEXT NOT NULL,
            seconds  REAL NOT NULL,
            recorded TEXT NOT NULL            -- ISO-8601
        );

        -- NEW: last-activity timestamp for silence-alert
        CREATE TABLE IF NOT EXISTS bot_state (
            key   TEXT PRIMARY KEY,
            value TEXT NOT NULL
        );

        -- CLOUD MODE: remote job queue (claimed by PC workers via the job API)
        CREATE TABLE IF NOT EXISTS jobs (
            id            INTEGER PRIMARY KEY AUTOINCREMENT,
            chat_id       INTEGER NOT NULL,
            user_id       INTEGER NOT NULL,
            user_name     TEXT,
            title         TEXT    NOT NULL,
            cmd_args      TEXT    NOT NULL DEFAULT '[]',  -- JSON list of args for automation script
            status        TEXT    NOT NULL DEFAULT 'pending',
            -- pending | claimed | completed | failed | cancelled
            claimed_by    TEXT,
            claimed_at    TEXT,
            created_at    TEXT    NOT NULL,
            completed_at  TEXT,
            notified      INTEGER NOT NULL DEFAULT 0,     -- 1 once bot sent result to user
            result_status TEXT,
            result_output TEXT
        );
        CREATE INDEX IF NOT EXISTS idx_jobs_status_created ON jobs(status, created_at);

        -- CLOUD MODE: worker PC heartbeats
        CREATE TABLE IF NOT EXISTS workers (
            name           TEXT PRIMARY KEY,
            last_heartbeat TEXT NOT NULL,
            ip             TEXT,
            version        TEXT
        );
    """)
    conn.commit()


# --------------------------------------------------------------------------- #
# One-time migration from legacy JSON files
# --------------------------------------------------------------------------- #

def migrate_from_json(
    access_path:     Optional[Path],
    saved_refs_path: Optional[Path],
    usage_path:      Optional[Path],
) -> None:
    """Import legacy JSON files into SQLite (skips files that don't exist).
    Safe to call on every startup — uses INSERT OR IGNORE throughout."""
    with _lock, _open() as conn:
        # --- access.json -------------------------------------------------- #
        if access_path and access_path.exists():
            try:
                data = json.loads(access_path.read_text(encoding="utf-8"))
                for window, value in (data.get("defaults") or {}).items():
                    conn.execute(
                        "INSERT OR IGNORE INTO access_defaults(window, value) VALUES(?,?)",
                        (window, value),
                    )
                for uid_str, entry in (data.get("users") or {}).items():
                    try:
                        uid = int(uid_str)
                    except ValueError:
                        continue
                    hour_in  = "hour" in entry
                    day_in   = "day"  in entry
                    conn.execute(
                        """INSERT OR IGNORE INTO users
                           (user_id, name, hour_limit, day_limit,
                            hour_use_default, day_use_default)
                           VALUES(?,?,?,?,?,?)""",
                        (uid, entry.get("name"),
                         entry.get("hour") if hour_in else None,
                         entry.get("day")  if day_in  else None,
                         0 if hour_in else 1,
                         0 if day_in  else 1),
                    )
                    for cmd in entry.get("commands", []):
                        conn.execute(
                            "INSERT OR IGNORE INTO user_commands(user_id, command) VALUES(?,?)",
                            (uid, cmd),
                        )
                    refs = entry.get("refs")
                    if refs is not None:
                        conn.execute(
                            "INSERT OR IGNORE INTO user_refs_restricted(user_id,restricted) VALUES(?,1)",
                            (uid,),
                        )
                        for ref in refs:
                            conn.execute(
                                "INSERT OR IGNORE INTO user_allowed_refs(user_id,ref) VALUES(?,?)",
                                (uid, ref),
                            )
                log.info("Migrated access.json → SQLite")
            except Exception as exc:
                log.warning("access.json migration failed: %s", exc)

        # --- saved_refs.json ---------------------------------------------- #
        if saved_refs_path and saved_refs_path.exists():
            try:
                data = json.loads(saved_refs_path.read_text(encoding="utf-8"))
                for uid_str, nicks in data.items():
                    try:
                        uid = int(uid_str)
                    except ValueError:
                        continue
                    for nick, ref in nicks.items():
                        conn.execute(
                            "INSERT OR IGNORE INTO saved_refs(user_id,nickname,ref) VALUES(?,?,?)",
                            (uid, nick.lower(), ref),
                        )
                log.info("Migrated saved_refs.json → SQLite")
            except Exception as exc:
                log.warning("saved_refs.json migration failed: %s", exc)

        # --- usage.json --------------------------------------------------- #
        if usage_path and usage_path.exists():
            try:
                data = json.loads(usage_path.read_text(encoding="utf-8"))
                cutoff = time.time() - 86400
                for uid_str, entries in data.items():
                    try:
                        uid = int(uid_str)
                    except ValueError:
                        continue
                    for e in entries:
                        if len(e) >= 2 and e[0] > cutoff:  # only import recent records
                            conn.execute(
                                "INSERT INTO usage(user_id,timestamp,count,token) VALUES(?,?,?,?)",
                                (uid, e[0], e[1], e[2] if len(e) > 2 else None),
                            )
                log.info("Migrated usage.json → SQLite")
            except Exception as exc:
                log.warning("usage.json migration failed: %s", exc)

        conn.commit()


# --------------------------------------------------------------------------- #
# Access-control defaults
# --------------------------------------------------------------------------- #

def get_access_defaults() -> dict[str, Any]:
    with _open() as conn:
        rows = conn.execute("SELECT window, value FROM access_defaults").fetchall()
    return {r["window"]: r["value"] for r in rows}


def set_access_default(window: str, value: Optional[int]) -> None:
    with _lock, _open() as conn:
        conn.execute(
            "INSERT OR REPLACE INTO access_defaults(window, value) VALUES(?,?)",
            (window, value),
        )
        conn.commit()


# --------------------------------------------------------------------------- #
# Per-user access control
# --------------------------------------------------------------------------- #

def get_user_commands(user_id: int) -> list[str]:
    with _open() as conn:
        rows = conn.execute(
            "SELECT command FROM user_commands WHERE user_id=?", (user_id,),
        ).fetchall()
    return [r["command"] for r in rows]


def effective_limits(user_id: int) -> dict[str, Optional[int]]:
    """{'hour': int|None, 'day': int|None} — None = unlimited."""
    with _open() as conn:
        row      = conn.execute("SELECT * FROM users WHERE user_id=?", (user_id,)).fetchone()
        defs_rows = conn.execute("SELECT window, value FROM access_defaults").fetchall()
    defaults = {r["window"]: r["value"] for r in defs_rows}
    result   = {}
    for w in ("hour", "day"):
        if row and not row[f"{w}_use_default"]:
            result[w] = row[f"{w}_limit"]
        else:
            result[w] = defaults.get(w)
    return result


def get_allowed_refs(user_id: int) -> Optional[list[str]]:
    """None = any ref; list (possibly empty) = restricted to these."""
    with _open() as conn:
        rr = conn.execute(
            "SELECT restricted FROM user_refs_restricted WHERE user_id=?", (user_id,),
        ).fetchone()
        if not rr or not rr["restricted"]:
            return None
        rows = conn.execute(
            "SELECT ref FROM user_allowed_refs WHERE user_id=?", (user_id,),
        ).fetchall()
    return [r["ref"] for r in rows]


def user_exists(user_id: int) -> bool:
    with _open() as conn:
        return conn.execute(
            "SELECT 1 FROM users WHERE user_id=?", (user_id,),
        ).fetchone() is not None


def get_all_users() -> dict[int, dict]:
    """Return {user_id: {name, commands, refs, hour, day}} for every managed user."""
    with _open() as conn:
        uids = [r["user_id"] for r in conn.execute("SELECT user_id FROM users").fetchall()]
    return {uid: _build_user_dict(uid) for uid in uids}


def _build_user_dict(user_id: int) -> dict:
    with _open() as conn:
        row  = conn.execute("SELECT * FROM users WHERE user_id=?", (user_id,)).fetchone()
        cmds = [r["command"] for r in conn.execute(
            "SELECT command FROM user_commands WHERE user_id=?", (user_id,)).fetchall()]
        defs = {r["window"]: r["value"] for r in conn.execute(
            "SELECT window, value FROM access_defaults").fetchall()}
        rr   = conn.execute(
            "SELECT restricted FROM user_refs_restricted WHERE user_id=?", (user_id,),
        ).fetchone()
        refs = None
        if rr and rr["restricted"]:
            refs = [r["ref"] for r in conn.execute(
                "SELECT ref FROM user_allowed_refs WHERE user_id=?", (user_id,)).fetchall()]
    if not row:
        return {"name": None, "commands": cmds, "refs": refs, "hour": None, "day": None}
    return {
        "name":     row["name"],
        "commands": cmds,
        "refs":     refs,
        "hour":     (defs.get("hour") if row["hour_use_default"] else row["hour_limit"]),
        "day":      (defs.get("day")  if row["day_use_default"]  else row["day_limit"]),
    }


def add_user(user_id: int, commands: list[str], name: Optional[str] = None) -> None:
    with _lock, _open() as conn:
        conn.execute(
            "INSERT OR IGNORE INTO users(user_id, name, hour_use_default, day_use_default)"
            " VALUES(?,?,1,1)",
            (user_id, name),
        )
        if name:
            conn.execute("UPDATE users SET name=? WHERE user_id=?", (name, user_id))
        for cmd in commands:
            conn.execute(
                "INSERT OR IGNORE INTO user_commands(user_id, command) VALUES(?,?)",
                (user_id, cmd),
            )
        conn.commit()


def remove_user(user_id: int, commands: Optional[list[str]] = None) -> bool:
    """Remove a user entirely, or just specific commands.  Returns True if the user existed."""
    with _lock, _open() as conn:
        existed = conn.execute(
            "SELECT 1 FROM users WHERE user_id=?", (user_id,),
        ).fetchone() is not None
        if not existed:
            return False
        if commands is None:
            for tbl in ("user_commands", "user_allowed_refs",
                        "user_refs_restricted", "users"):
                conn.execute(f"DELETE FROM {tbl} WHERE user_id=?", (user_id,))
        else:
            for cmd in commands:
                conn.execute(
                    "DELETE FROM user_commands WHERE user_id=? AND command=?",
                    (user_id, cmd),
                )
            has_cmds  = conn.execute(
                "SELECT 1 FROM user_commands WHERE user_id=?", (user_id,)).fetchone()
            has_refs  = conn.execute(
                "SELECT 1 FROM user_refs_restricted WHERE user_id=? AND restricted=1",
                (user_id,)).fetchone()
            has_limits = conn.execute(
                "SELECT 1 FROM users WHERE user_id=?"
                " AND (hour_use_default=0 OR day_use_default=0)",
                (user_id,)).fetchone()
            if not has_cmds and not has_refs and not has_limits:
                conn.execute("DELETE FROM users WHERE user_id=?", (user_id,))
        conn.commit()
    return True


def set_user_limit(user_id: int, window: str, value) -> None:
    """value: int → set that limit; None → unlimited; 'default' → use global default."""
    with _lock, _open() as conn:
        conn.execute(
            "INSERT OR IGNORE INTO users(user_id, hour_use_default, day_use_default)"
            " VALUES(?,1,1)",
            (user_id,),
        )
        if value == "default":
            conn.execute(
                f"UPDATE users SET {window}_use_default=1, {window}_limit=NULL"
                " WHERE user_id=?",
                (user_id,),
            )
        else:
            conn.execute(
                f"UPDATE users SET {window}_use_default=0, {window}_limit=?"
                " WHERE user_id=?",
                (value, user_id),
            )
        conn.commit()


def add_allowed_refs(user_id: int, new_refs: list[str]) -> list[str]:
    """Append refs to this user's allow-list (creates the restriction if first call).
    Returns the full list afterwards."""
    with _lock, _open() as conn:
        conn.execute(
            "INSERT OR IGNORE INTO users(user_id, hour_use_default, day_use_default)"
            " VALUES(?,1,1)",
            (user_id,),
        )
        conn.execute(
            "INSERT OR REPLACE INTO user_refs_restricted(user_id, restricted) VALUES(?,1)",
            (user_id,),
        )
        for ref in new_refs:
            conn.execute(
                "INSERT OR IGNORE INTO user_allowed_refs(user_id, ref) VALUES(?,?)",
                (user_id, ref),
            )
        conn.commit()
        rows = conn.execute(
            "SELECT ref FROM user_allowed_refs WHERE user_id=?", (user_id,),
        ).fetchall()
    return [r["ref"] for r in rows]


def remove_allowed_refs(user_id: int, remove_refs: list[str]) -> list[str]:
    """Remove refs from this user's allow-list.  Returns the remaining list."""
    with _lock, _open() as conn:
        for ref in remove_refs:
            conn.execute(
                "DELETE FROM user_allowed_refs WHERE user_id=? AND ref=?",
                (user_id, ref),
            )
        conn.commit()
        rows = conn.execute(
            "SELECT ref FROM user_allowed_refs WHERE user_id=?", (user_id,),
        ).fetchall()
    return [r["ref"] for r in rows]


def set_allowed_refs_unrestricted(user_id: int) -> None:
    """Remove any reference restriction (user may look up any ref)."""
    with _lock, _open() as conn:
        conn.execute(
            "DELETE FROM user_allowed_refs    WHERE user_id=?", (user_id,),
        )
        conn.execute(
            "DELETE FROM user_refs_restricted WHERE user_id=?", (user_id,),
        )
        conn.commit()


# --------------------------------------------------------------------------- #
# Rate-limiting (usage)
# --------------------------------------------------------------------------- #

WINDOWS = {"hour": 3600, "day": 86400}


def _fmt_dur(seconds: int) -> str:
    s = max(0, seconds)
    if s < 60:
        return f"{s}s"
    m, s = divmod(s, 60)
    if m < 60:
        return f"{m}m {s}s"
    h, m = divmod(m, 60)
    return f"{h}h {m}m"


def check_and_consume(user_id: int, count: int, token: str) -> tuple[bool, Optional[str]]:
    """Try to reserve `count` lookups.  Returns (ok, error_message_or_None)."""
    limits = effective_limits(user_id)
    now    = time.time()
    with _lock, _open() as conn:
        worst_wait, refusal = 0, None
        for window, seconds in WINDOWS.items():
            limit = limits.get(window)
            if limit is None:
                continue
            rows = conn.execute(
                "SELECT timestamp, count FROM usage WHERE user_id=? AND timestamp>?"
                " ORDER BY timestamp",
                (user_id, now - seconds),
            ).fetchall()
            used = sum(r["count"] for r in rows)
            if count > limit:
                return False, (
                    f"⛔ This request has {count} reference number(s) but your"
                    f" limit is {limit} per {window}."
                )
            if used + count > limit:
                need, freed, wait = used + count - limit, 0, seconds
                for r in rows:
                    freed += r["count"]
                    if freed >= need:
                        wait = r["timestamp"] + seconds - now
                        break
                if wait >= worst_wait:
                    worst_wait = wait
                    refusal = (
                        f"⛔ Lookup limit reached: {used} of {limit} used in the"
                        f" last {window}.\nYou can look up {count} more in about"
                        f" {_fmt_dur(int(wait + 1))}."
                    )
        if refusal:
            return False, refusal
        conn.execute(
            "INSERT INTO usage(user_id, timestamp, count, token) VALUES(?,?,?,?)",
            (user_id, now, count, token),
        )
        conn.commit()
    return True, None


def refund_usage(user_id: int, token: str) -> None:
    with _lock, _open() as conn:
        conn.execute(
            "DELETE FROM usage WHERE user_id=? AND token=?", (user_id, token),
        )
        conn.commit()


def usage_report(user_id: int) -> dict[str, tuple[int, Optional[int]]]:
    """{'hour': (used, limit), 'day': (used, limit)}"""
    limits = effective_limits(user_id)
    now    = time.time()
    result = {}
    with _open() as conn:
        for window, seconds in WINDOWS.items():
            row = conn.execute(
                "SELECT COALESCE(SUM(count),0) AS total"
                " FROM usage WHERE user_id=? AND timestamp>?",
                (user_id, now - seconds),
            ).fetchone()
            result[window] = (row["total"], limits.get(window))
    return result


def prune_old_usage() -> None:
    """Delete usage records older than 24 h.  Call from a housekeeping thread."""
    cutoff = time.time() - 86400
    with _lock, _open() as conn:
        conn.execute("DELETE FROM usage WHERE timestamp<?", (cutoff,))
        conn.commit()


# --------------------------------------------------------------------------- #
# Saved reference nicknames
# --------------------------------------------------------------------------- #

def get_saved(user_id: int) -> dict[str, str]:
    with _open() as conn:
        rows = conn.execute(
            "SELECT nickname, ref FROM saved_refs WHERE user_id=?", (user_id,),
        ).fetchall()
    return {r["nickname"]: r["ref"] for r in rows}


def count_saved(user_id: int) -> int:
    with _open() as conn:
        row = conn.execute(
            "SELECT COUNT(*) AS n FROM saved_refs WHERE user_id=?", (user_id,),
        ).fetchone()
    return row["n"]


def save_ref(user_id: int, nickname: str, ref: str) -> None:
    with _lock, _open() as conn:
        conn.execute(
            "INSERT OR REPLACE INTO saved_refs(user_id, nickname, ref) VALUES(?,?,?)",
            (user_id, nickname.lower(), ref),
        )
        conn.commit()


def unsave_ref(user_id: int, nickname: str) -> bool:
    with _lock, _open() as conn:
        c = conn.execute(
            "DELETE FROM saved_refs WHERE user_id=? AND nickname=?",
            (user_id, nickname.lower()),
        )
        conn.commit()
    return c.rowcount > 0


# --------------------------------------------------------------------------- #
# Subscriptions  (new feature)
# --------------------------------------------------------------------------- #

def subscribe(user_id: int) -> bool:
    """Returns True if newly subscribed, False if already was."""
    with _lock, _open() as conn:
        already = conn.execute(
            "SELECT 1 FROM subscriptions WHERE user_id=?", (user_id,),
        ).fetchone()
        if already:
            return False
        conn.execute("INSERT INTO subscriptions(user_id) VALUES(?)", (user_id,))
        conn.commit()
    return True


def unsubscribe(user_id: int) -> bool:
    with _lock, _open() as conn:
        c = conn.execute("DELETE FROM subscriptions WHERE user_id=?", (user_id,))
        conn.commit()
    return c.rowcount > 0


def is_subscribed(user_id: int) -> bool:
    with _open() as conn:
        return conn.execute(
            "SELECT 1 FROM subscriptions WHERE user_id=?", (user_id,),
        ).fetchone() is not None


def get_all_subscribers() -> list[int]:
    with _open() as conn:
        rows = conn.execute("SELECT user_id FROM subscriptions").fetchall()
    return [r["user_id"] for r in rows]


# --------------------------------------------------------------------------- #
# Payment log  (new feature)
# --------------------------------------------------------------------------- #

def log_payment(ref: str, user_id: int, note: Optional[str] = None) -> str:
    """Record a payment and return the ISO-8601 timestamp string."""
    paid_at = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    with _lock, _open() as conn:
        conn.execute(
            "INSERT INTO payments(ref, user_id, paid_at, note) VALUES(?,?,?,?)",
            (ref, user_id, paid_at, note),
        )
        conn.commit()
    return paid_at


def get_payment_history(ref: str) -> list[dict]:
    with _open() as conn:
        rows = conn.execute(
            "SELECT paid_at, user_id, note FROM payments"
            " WHERE ref=? ORDER BY id DESC LIMIT 10",   # id is AUTOINCREMENT → safe insertion-order proxy
            (ref,),
        ).fetchall()
    return [{"paid_at": r["paid_at"], "user_id": r["user_id"], "note": r["note"]}
            for r in rows]


# --------------------------------------------------------------------------- #
# Temporary access expiry  (new)
# --------------------------------------------------------------------------- #

def set_user_expiry(user_id: int, expires_at: datetime) -> None:
    """Set an expiry time for a managed user.  expires_at is UTC."""
    ts = expires_at.strftime("%Y-%m-%dT%H:%M:%SZ")
    with _lock, _open() as conn:
        conn.execute(
            "INSERT OR REPLACE INTO user_expiry(user_id, expires_at) VALUES(?,?)",
            (user_id, ts),
        )
        conn.commit()


def get_user_expiry(user_id: int) -> Optional[datetime]:
    """Return the expiry datetime (UTC) for user_id, or None if no expiry is set."""
    with _open() as conn:
        row = conn.execute(
            "SELECT expires_at FROM user_expiry WHERE user_id=?", (user_id,),
        ).fetchone()
    if not row:
        return None
    try:
        return datetime.strptime(row["expires_at"], "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
    except ValueError:
        return None


def is_user_expired(user_id: int) -> bool:
    """Return True if the user's access window has passed."""
    exp = get_user_expiry(user_id)
    if exp is None:
        return False
    return datetime.now(timezone.utc) > exp


def remove_expired_users() -> list[int]:
    """Delete users whose access has expired.  Returns list of removed user_ids."""
    now_ts = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    with _lock, _open() as conn:
        rows = conn.execute(
            "SELECT user_id FROM user_expiry WHERE expires_at <= ?", (now_ts,),
        ).fetchall()
        expired = [r["user_id"] for r in rows]
        for uid in expired:
            for tbl in ("user_commands", "user_allowed_refs",
                        "user_refs_restricted", "users", "user_expiry",
                        "user_allowed_hours"):
                conn.execute(f"DELETE FROM {tbl} WHERE user_id=?", (uid,))
        conn.commit()
    return expired


# --------------------------------------------------------------------------- #
# Time-gated access  (new)
# --------------------------------------------------------------------------- #

def set_allowed_hours(user_id: int, hour_start: int, hour_end: int) -> None:
    """Restrict a user to hour_start..hour_end (local bot time).  hour_end is exclusive."""
    with _lock, _open() as conn:
        conn.execute(
            "INSERT OR REPLACE INTO user_allowed_hours(user_id,hour_start,hour_end) VALUES(?,?,?)",
            (user_id, hour_start, hour_end),
        )
        conn.commit()


def remove_allowed_hours(user_id: int) -> None:
    with _lock, _open() as conn:
        conn.execute("DELETE FROM user_allowed_hours WHERE user_id=?", (user_id,))
        conn.commit()


def get_allowed_hours(user_id: int) -> Optional[tuple[int, int]]:
    """Return (hour_start, hour_end) or None if no gate is set."""
    with _open() as conn:
        row = conn.execute(
            "SELECT hour_start, hour_end FROM user_allowed_hours WHERE user_id=?", (user_id,),
        ).fetchone()
    return (row["hour_start"], row["hour_end"]) if row else None


def is_within_allowed_hours(user_id: int) -> bool:
    """True if the current hour is within the user's allowed window (or no gate is set)."""
    gate = get_allowed_hours(user_id)
    if gate is None:
        return True
    current_hour = datetime.now().hour
    start, end = gate
    if start <= end:
        return start <= current_hour < end
    # Overnight window, e.g. 22..06
    return current_hour >= start or current_hour < end


# --------------------------------------------------------------------------- #
# Job duration tracking  (new)
# --------------------------------------------------------------------------- #

def record_job_duration(title: str, seconds: float) -> None:
    ts = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    with _lock, _open() as conn:
        conn.execute(
            "INSERT INTO job_durations(title, seconds, recorded) VALUES(?,?,?)",
            (title, seconds, ts),
        )
        # Keep only last 20 records per title
        conn.execute(
            """DELETE FROM job_durations WHERE id NOT IN (
                   SELECT id FROM job_durations WHERE title=?
                   ORDER BY id DESC LIMIT 20
               ) AND title=?""",
            (title, title),
        )
        conn.commit()


def get_avg_job_duration(title: str) -> Optional[float]:
    """Return the rolling average duration in seconds for this job title, or None."""
    with _open() as conn:
        row = conn.execute(
            "SELECT AVG(seconds) AS avg FROM job_durations WHERE title=?", (title,),
        ).fetchone()
    val = row["avg"] if row else None
    return float(val) if val is not None else None


# --------------------------------------------------------------------------- #
# Bot-state / silence-alert  (new)
# --------------------------------------------------------------------------- #

def touch_last_activity() -> None:
    ts = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    with _lock, _open() as conn:
        conn.execute(
            "INSERT OR REPLACE INTO bot_state(key, value) VALUES('last_activity',?)", (ts,),
        )
        conn.commit()


def get_last_activity() -> Optional[datetime]:
    with _open() as conn:
        row = conn.execute(
            "SELECT value FROM bot_state WHERE key='last_activity'",
        ).fetchone()
    if not row:
        return None
    try:
        return datetime.strptime(row["value"], "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
    except ValueError:
        return None


# --------------------------------------------------------------------------- #
# Cloud job queue  (new)
# --------------------------------------------------------------------------- #

def create_job(
    chat_id: int,
    user_id: int,
    user_name: str,
    title: str,
    cmd_args: str,          # JSON-encoded list of CLI args
) -> int:
    """Insert a new pending job and return its id."""
    created_at = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    with _lock, _open() as conn:
        cur = conn.execute(
            """INSERT INTO jobs(chat_id,user_id,user_name,title,cmd_args,status,created_at)
               VALUES(?,?,?,?,?,'pending',?)""",
            (chat_id, user_id, user_name, title, cmd_args, created_at),
        )
        conn.commit()
    return cur.lastrowid


def claim_next_job(worker_name: str) -> Optional[dict]:
    """Atomically claim the oldest pending job. Returns job dict or None."""
    now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    with _lock, _open() as conn:
        row = conn.execute(
            "SELECT id FROM jobs WHERE status='pending' ORDER BY id ASC LIMIT 1"
        ).fetchone()
        if not row:
            return None
        job_id = row["id"]
        conn.execute(
            "UPDATE jobs SET status='claimed', claimed_by=?, claimed_at=? WHERE id=? AND status='pending'",
            (worker_name, now, job_id),
        )
        conn.commit()
        # Re-fetch to confirm the claim was ours (handles concurrent workers)
        job = conn.execute("SELECT * FROM jobs WHERE id=? AND claimed_by=?",
                           (job_id, worker_name)).fetchone()
    if not job:
        return None   # another worker got it first
    return dict(job)


def complete_job(job_id: int, result_status: str, result_output: str) -> None:
    now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    with _lock, _open() as conn:
        conn.execute(
            """UPDATE jobs
               SET status='completed', completed_at=?, result_status=?, result_output=?
               WHERE id=?""",
            (now, result_status, result_output, job_id),
        )
        conn.commit()


def fail_job(job_id: int, error: str) -> None:
    now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    with _lock, _open() as conn:
        conn.execute(
            """UPDATE jobs
               SET status='failed', completed_at=?, result_status='Script failed with error:',
                   result_output=?
               WHERE id=?""",
            (now, error, job_id),
        )
        conn.commit()


def cancel_job(job_id: int) -> bool:
    with _lock, _open() as conn:
        c = conn.execute(
            "UPDATE jobs SET status='cancelled' WHERE id=? AND status='pending'",
            (job_id,),
        )
        conn.commit()
    return c.rowcount > 0


def cancel_pending_jobs_for_user(user_id: int) -> int:
    """Cancel all pending (not yet claimed) jobs for this user. Returns count."""
    with _lock, _open() as conn:
        c = conn.execute(
            "UPDATE jobs SET status='cancelled' WHERE user_id=? AND status='pending'",
            (user_id,),
        )
        conn.commit()
    return c.rowcount


def get_job(job_id: int) -> Optional[dict]:
    with _open() as conn:
        row = conn.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone()
    return dict(row) if row else None


def get_pending_count() -> int:
    with _open() as conn:
        row = conn.execute(
            "SELECT COUNT(*) AS n FROM jobs WHERE status IN ('pending','claimed')"
        ).fetchone()
    return row["n"]


def get_completed_unnotified() -> list[dict]:
    with _open() as conn:
        rows = conn.execute(
            "SELECT * FROM jobs WHERE status IN ('completed','failed') AND notified=0"
            " ORDER BY id ASC LIMIT 50"
        ).fetchall()
    return [dict(r) for r in rows]


def mark_notified(job_id: int) -> None:
    with _lock, _open() as conn:
        conn.execute("UPDATE jobs SET notified=1 WHERE id=?", (job_id,))
        conn.commit()


def recover_stale_claims(timeout_minutes: int = 10) -> int:
    """Put stuck 'claimed' jobs (worker died) back to 'pending'. Returns count recovered."""
    cutoff = (
        datetime.now(timezone.utc) - timedelta(minutes=timeout_minutes)
    ).strftime("%Y-%m-%dT%H:%M:%SZ")
    with _lock, _open() as conn:
        c = conn.execute(
            "UPDATE jobs SET status='pending', claimed_by=NULL, claimed_at=NULL"
            " WHERE status='claimed' AND claimed_at < ?",
            (cutoff,),
        )
        conn.commit()
    return c.rowcount


# --------------------------------------------------------------------------- #
# Worker heartbeats  (new)
# --------------------------------------------------------------------------- #

def update_worker_heartbeat(name: str, ip: str = "", version: str = "") -> None:
    now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    with _lock, _open() as conn:
        conn.execute(
            "INSERT OR REPLACE INTO workers(name, last_heartbeat, ip, version) VALUES(?,?,?,?)",
            (name, now, ip or "", version or ""),
        )
        conn.commit()


def get_online_workers(timeout_minutes: int = 3) -> list[dict]:
    cutoff = (
        datetime.now(timezone.utc) - timedelta(minutes=timeout_minutes)
    ).strftime("%Y-%m-%dT%H:%M:%SZ")
    with _open() as conn:
        rows = conn.execute(
            "SELECT * FROM workers WHERE last_heartbeat >= ? ORDER BY name",
            (cutoff,),
        ).fetchall()
    return [dict(r) for r in rows]


def count_online_workers(timeout_minutes: int = 3) -> int:
    return len(get_online_workers(timeout_minutes))


def worker_was_recently_online(name: str, within_minutes: int = 4) -> bool:
    cutoff = (
        datetime.now(timezone.utc) - timedelta(minutes=within_minutes)
    ).strftime("%Y-%m-%dT%H:%M:%SZ")
    with _open() as conn:
        row = conn.execute(
            "SELECT 1 FROM workers WHERE name=? AND last_heartbeat >= ?",
            (name, cutoff),
        ).fetchone()
    return row is not None
