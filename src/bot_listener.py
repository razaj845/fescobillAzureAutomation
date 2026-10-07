#!/usr/bin/env python3
"""
FESCO Bill Bot  –  Telegram front-end for fesco_bill_automation.py

Commands (see /help inside Telegram for the list that applies to you):
    /run  /getbillbyref  /lastbill  /history  /saved  /save  /unsave
    /status  /cancel  /mylimit  /subscribe  /unsubscribe  /paid

Admin only:
    /adduser  /removeuser  /users  /setlimit  /allowref  /denyref
    /activity  /logs  /health  /reload  /backup
"""
import argparse
import hashlib
import itertools
import json
import logging
import logging.handlers
import os
import queue
import re
import shutil
import signal
import subprocess
import sys
import threading
import time
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

import requests
import telebot
from dotenv import load_dotenv
from telebot import types

import db

# Cloud-mode modules — only imported when CLOUD_MODE=1
try:
    import job_queue
    import worker_table
    _AZURE_AVAILABLE = True
except ImportError:
    _AZURE_AVAILABLE = False

# --------------------------------------------------------------------------- #
# Paths & environment
# --------------------------------------------------------------------------- #
# CLOUD_MODE=1  →  bot runs on Azure Container App, no local scraping.
#                  Jobs are queued in bot.db and claimed by PC workers via the
#                  built-in job API (Flask, port WORKER_API_PORT).
# CLOUD_MODE unset →  original single-PC behaviour, unchanged.
BASE_DIR     = Path(__file__).resolve().parent   # .../src/
PROJECT_ROOT = BASE_DIR.parent                   # project root

load_dotenv(PROJECT_ROOT / ".env")
logger = logging.getLogger("fesco.bot")

BOT_TOKEN    = os.getenv("TELEGRAM_BOT_TOKEN")
WORKER_URL   = os.getenv("CLOUDFLARE_WORKER_URL", "").rstrip("/")
CLOUD_MODE      = os.getenv("CLOUD_MODE", "").strip() in ("1", "true", "yes")
WORKER_API_PORT = int(os.getenv("WORKER_API_PORT", "8765"))
WORKER_API_KEY  = os.getenv("WORKER_API_KEY", "")
# Container Apps: public HTTPS URL Azure assigns (e.g. https://fescobill.eastus.azurecontainerapps.io)
WEBHOOK_URL     = os.getenv("WEBHOOK_URL", "").rstrip("/")
AZURE_CONN_STR  = os.getenv("AZURE_STORAGE_CONNECTION_STRING", "")
PLAYWRIGHT_SCRIPT_PATH = (
    os.getenv("PLAYWRIGHT_SCRIPT_PATH") or str(BASE_DIR / "fesco_bill_automation.py")
)

_ch = os.getenv("BILL_CHECK_HOUR", "").strip()
BILL_CHECK_HOUR: int | None = int(_ch) if _ch.isdigit() and 0 <= int(_ch) <= 23 else None

DIRECT_API_URL = "https://api.telegram.org/bot{0}/{1}"
WORKER_API_URL = f"{WORKER_URL}/bot{{0}}/{{1}}" if WORKER_URL else DIRECT_API_URL
CONFIG_PATH    = PROJECT_ROOT / "config" / "config.json"


def parse_id_list(env_name: str) -> list[int]:
    return [int(x) for x in os.getenv(env_name, "").split(",") if x.strip().isdigit()]


def _path_env(name: str, default: Path) -> Path:
    return Path(os.getenv(name) or default)


# --------------------------------------------------------------------------- #
# Access control — static (.env) lists
# --------------------------------------------------------------------------- #
ALLOWED_USER_IDS = parse_id_list("ALLOWED_USER_ID")
COMMAND_ACCESS = {
    "run":          set(parse_id_list("RUN_USER_IDS")),
    "getbillbyref": set(parse_id_list("GETBILLBYREF_USER_IDS")),
}
VALID_COMMANDS       = ("run", "getbillbyref")
PRIMARY_ADMIN_CHAT_ID = ALLOWED_USER_IDS[0] if ALLOWED_USER_IDS else None
ADMIN_NOTICE_FOR_GETBILLBYREF = True

# --------------------------------------------------------------------------- #
# Paths
# --------------------------------------------------------------------------- #
BILLS_DIR         = Path(os.getenv("BILLS_DIR")         or PROJECT_ROOT / "data/bills")
BILL_HISTORY_FILE = Path(os.getenv("BILL_HISTORY_FILE") or PROJECT_ROOT / "data/bill_history.json")
RUN_LOCK_FILE     = Path(os.getenv("RUN_LOCK_FILE")     or PROJECT_ROOT / "data/fesco_bill_automation.lock")
BOT_DB_FILE       = _path_env("BOT_DB_FILE",        PROJECT_ROOT / "data/bot.db")
ACTIVITY_LOG_FILE = _path_env("ACTIVITY_LOG_FILE",  PROJECT_ROOT / "logs/activity.log")
BOT_LOCK_FILE     = _path_env("BOT_LOCK_FILE",      PROJECT_ROOT / "bot.lock")
BOT_LOG_FILE      = _path_env("BOT_LOG_FILE",       PROJECT_ROOT / "logs/bot.log")

# Legacy JSON – only for one-time migration
_LEGACY_ACCESS     = _path_env("ACCESS_FILE",     PROJECT_ROOT / "data/access.json")
_LEGACY_SAVED_REFS = _path_env("SAVED_REFS_FILE", PROJECT_ROOT / "data/saved_refs.json")
_LEGACY_USAGE      = _path_env("USAGE_FILE",      PROJECT_ROOT / "data/usage.json")

MAX_SAVED_PER_USER   = 20
REF_PATTERN          = re.compile(r"\d{6,20}")
NICKNAME_PATTERN     = re.compile(r"[A-Za-z][A-Za-z0-9_-]{0,19}")
EMAIL_PATTERN        = re.compile(r"[^@\s]+@[^@\s]+\.[^@\s]+")
RUN_TIMEOUT_SECONDS  = 60 * 60
LOG_MAX_BYTES        = 1_000_000
LOG_BACKUP_COUNT     = 5
STARTED_AT           = time.time()
DISK_ALERT_MB        = int(os.getenv("DISK_ALERT_MB", "500"))
STALE_BILL_DAYS      = 45

# --------------------------------------------------------------------------- #
# Config.json in-memory index  (for ref lookup)
# --------------------------------------------------------------------------- #
_config_lock = threading.Lock()
_config_refs: set[str] = set()          # reference numbers present in config.json
_config_mtime: float = 0.0


def reload_config() -> int:
    """Re-read config.json into memory.  Returns number of bills loaded."""
    global _config_refs, _config_mtime
    try:
        raw    = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
        refs   = {str(b.get("reference_no", "")).strip() for b in raw.get("bills", []) if b.get("reference_no")}
        mtime  = CONFIG_PATH.stat().st_mtime
        with _config_lock:
            _config_refs  = refs
            _config_mtime = mtime
        logger.info("Config loaded: %d bill(s) in config.json", len(refs))
        return len(refs)
    except FileNotFoundError:
        logger.warning("config.json not found at %s", CONFIG_PATH)
        return 0
    except Exception as e:
        logger.warning("Could not load config.json: %s", e)
        return 0


def ref_in_config(ref: str) -> bool:
    with _config_lock:
        return ref in _config_refs


def validate_config() -> list[str]:
    """Parse config.json fully and return a list of human-readable problems."""
    problems: list[str] = []
    if not CONFIG_PATH.exists():
        return ["config.json not found"]
    try:
        raw = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
    except Exception as e:
        return [f"config.json is not valid JSON: {e}"]

    bills = raw.get("bills", [])
    if not bills:
        problems.append("config.json has no 'bills' entries")

    for i, b in enumerate(bills):
        label  = b.get("label") or b.get("reference_no") or f"bills[{i}]"
        ref    = str(b.get("reference_no", "")).strip()
        if not ref:
            problems.append(f"{label}: missing reference_no")
        elif not REF_PATTERN.fullmatch(ref):
            problems.append(f"{label}: reference_no '{ref}' is not all digits")

        recip = b.get("recipients", {})
        for addr in recip.get("emails", []):
            a = addr if isinstance(addr, str) else addr.get("address", "")
            if a and not EMAIL_PATTERN.fullmatch(a):
                problems.append(f"{label}: invalid email address '{a}'")
        for cid in recip.get("telegram_chat_ids", []):
            cid_s = cid if isinstance(cid, str) else str(cid.get("chat_id", ""))
            if cid_s and not re.fullmatch(r"-?\d+", cid_s):
                problems.append(f"{label}: invalid telegram_chat_id '{cid_s}'")

    return problems


def check_env_completeness(config_path: Path) -> list[str]:
    """Cross-check .env credentials against what config.json actually uses."""
    issues: list[str] = []
    try:
        raw   = json.loads(config_path.read_text(encoding="utf-8"))
        bills = raw.get("bills", [])
    except Exception:
        return []

    needs_email    = any(b.get("recipients", {}).get("emails")              for b in bills)
    needs_wa       = any(b.get("recipients", {}).get("whatsapp_numbers")    for b in bills)
    needs_telegram = any(b.get("recipients", {}).get("telegram_chat_ids")   for b in bills)

    if needs_email and not (os.getenv("GMAIL_ADDRESS") and os.getenv("GMAIL_APP_PASSWORD")):
        issues.append("config.json has email recipients but GMAIL_ADDRESS / GMAIL_APP_PASSWORD not set")
    if needs_wa and not (os.getenv("GREEN_API_INSTANCE_ID") and os.getenv("GREEN_API_TOKEN")):
        issues.append("config.json has WhatsApp recipients but GREEN_API_INSTANCE_ID / GREEN_API_TOKEN not set")
    if needs_telegram and not os.getenv("TELEGRAM_BOT_TOKEN"):
        issues.append("config.json has Telegram recipients but TELEGRAM_BOT_TOKEN not set")
    return issues


# --------------------------------------------------------------------------- #
# Telegram API – Cloudflare Worker with automatic fallback
# --------------------------------------------------------------------------- #
_using_fallback   = False
_fallback_lock    = threading.Lock()
telebot.apihelper.API_URL = WORKER_API_URL
bot = telebot.TeleBot(BOT_TOKEN)


def _switch_to_fallback() -> None:
    global _using_fallback
    with _fallback_lock:
        if _using_fallback:
            return
        _using_fallback = True
    telebot.apihelper.API_URL = DIRECT_API_URL
    logger.warning("Cloudflare Worker unreachable – switched to direct api.telegram.org")
    if PRIMARY_ADMIN_CHAT_ID:
        try:
            bot.send_message(
                PRIMARY_ADMIN_CHAT_ID,
                "⚠️ Cloudflare Worker unreachable.\n"
                "Now using direct Telegram API (api.telegram.org).\n"
                "Bot keeps running – check your Worker.",
            )
        except Exception:
            pass


def _try_restore_worker() -> None:
    global _using_fallback
    if not _using_fallback or not WORKER_URL:
        return
    try:
        requests.get(f"{WORKER_URL}/bot{BOT_TOKEN}/getMe", timeout=5).raise_for_status()
        telebot.apihelper.API_URL = WORKER_API_URL
        with _fallback_lock:
            _using_fallback = False
        logger.info("Cloudflare Worker reachable again – switched back")
        safe_send(PRIMARY_ADMIN_CHAT_ID, "✅ Cloudflare Worker is back. Switched back from direct API.")
    except Exception:
        pass


# --------------------------------------------------------------------------- #
# Small helpers
# --------------------------------------------------------------------------- #
def format_duration(seconds: int) -> str:
    s = max(0, int(seconds))
    if s < 60:       return f"{s}s"
    m, s = divmod(s, 60)
    if m < 60:       return f"{m}m {s}s"
    h, m = divmod(m, 60)
    return f"{h}h {m}m"


def command_args(message) -> str:
    parts = (message.text or "").split(maxsplit=1)
    return parts[1].strip() if len(parts) > 1 else ""


def command_name(message) -> str:
    parts = (message.text or "").split()
    return parts[0].split("@")[0] if parts else "?"


def read_json(path, default):
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
        return data if isinstance(data, type(default)) else default
    except (OSError, ValueError):
        return default


def last_bill_amount(ref: str) -> str | None:
    """Pull the most recent payable amount from bill_history.json."""
    try:
        entries = read_json(BILL_HISTORY_FILE, {}).get(ref, [])
        if entries:
            return entries[-1].get("payable_within_due")
    except Exception:
        pass
    return None


# --------------------------------------------------------------------------- #
# Activity log
# --------------------------------------------------------------------------- #
activity_logger = logging.getLogger("fesco.activity")
activity_logger.propagate = False


def configure_activity_log(path: Path) -> None:
    for h in list(activity_logger.handlers):
        activity_logger.removeHandler(h); h.close()
    h = logging.handlers.RotatingFileHandler(
        path, maxBytes=LOG_MAX_BYTES, backupCount=LOG_BACKUP_COUNT, encoding="utf-8",
    )
    h.setFormatter(logging.Formatter("%(asctime)s | %(message)s"))
    activity_logger.addHandler(h)
    activity_logger.setLevel(logging.INFO)


def activity(event: str, user=None, **fields) -> None:
    parts = [event]
    if user is not None:
        parts.append(f"user={getattr(user,'id',user)} ({getattr(user,'first_name','') or ''})")
    for k, v in fields.items():
        parts.append(f"{k}={str(v).replace(chr(10),' ')}")
    activity_logger.info(" | ".join(parts))
    db.touch_last_activity()


def read_activity_tail(count: int) -> list[str]:
    try:
        return Path(ACTIVITY_LOG_FILE).read_text(
            encoding="utf-8", errors="replace"
        ).splitlines()[-count:]
    except OSError:
        return []


def read_log_tail(count: int) -> list[str]:
    try:
        return BOT_LOG_FILE.read_text(encoding="utf-8", errors="replace").splitlines()[-count:]
    except OSError:
        return []


# --------------------------------------------------------------------------- #
# Access control
# --------------------------------------------------------------------------- #
def is_admin(user_id: int) -> bool:
    return user_id in ALLOWED_USER_IDS


def dynamic_commands(user_id: int) -> set[str]:
    return set(db.get_user_commands(user_id))


def has_access(user_id: int, command: str) -> bool:
    if is_admin(user_id):
        return True
    if db.is_user_expired(user_id):
        return False
    if not db.is_within_allowed_hours(user_id):
        return False
    return (
        user_id in COMMAND_ACCESS.get(command, set())
        or command in dynamic_commands(user_id)
    )


def has_any_access(user_id: int) -> bool:
    if is_admin(user_id):
        return True
    if db.is_user_expired(user_id) or not db.is_within_allowed_hours(user_id):
        return False
    return (
        any(user_id in ids for ids in COMMAND_ACCESS.values())
        or bool(dynamic_commands(user_id))
    )


def allowed_refs(user_id: int):
    return db.get_allowed_refs(user_id)


def refs_blocked(user_id: int, refs: list[str]) -> list[str]:
    if is_admin(user_id): return []
    allowed = allowed_refs(user_id)
    if allowed is None:   return []
    return [r for r in refs if r not in allowed]


def deny(message, command: str) -> None:
    activity("DENIED", message.from_user, cmd=command)
    # Give a time-gate-specific hint
    user_id = message.from_user.id
    gate    = db.get_allowed_hours(user_id)
    if gate and not db.is_within_allowed_hours(user_id):
        bot.reply_to(message, f"⛔ Access is only allowed between {gate[0]:02d}:00 and {gate[1]:02d}:00.")
    elif db.is_user_expired(user_id):
        bot.reply_to(message, "⛔ Your temporary access has expired. Contact the admin.")
    else:
        bot.reply_to(message, "Unauthorized access.")


def check_access(message, command: str) -> bool:
    if not has_access(message.from_user.id, command):
        deny(message, command_name(message))
        return False
    activity("command", message.from_user, cmd=command_name(message))
    return True


def check_any_access(message) -> bool:
    if not has_any_access(message.from_user.id):
        deny(message, command_name(message))
        return False
    activity("command", message.from_user, cmd=command_name(message))
    return True


def check_admin(message) -> bool:
    if not is_admin(message.from_user.id):
        deny(message, command_name(message))
        return False
    activity("command", message.from_user, cmd=command_name(message))
    return True


def describe_limits(limits: dict) -> str:
    parts = [f"{limits[w]}/{w}" for w in ("hour","day") if limits.get(w) is not None]
    return ", ".join(parts) if parts else "unlimited"


# --------------------------------------------------------------------------- #
# Saved nicknames
# --------------------------------------------------------------------------- #
def get_saved(user_id: int) -> dict[str,str]:
    return db.get_saved(user_id)


def resolve_tokens(user_id: int, text: str):
    saved = get_saved(user_id)
    refs, invalid = [], []
    def add(r):
        if r not in refs: refs.append(r)
    for part in re.split(r"[,\s]+", text or ""):
        if not part: continue
        low = part.lower()
        if REF_PATTERN.fullmatch(part):          add(part)
        elif low == "all" and saved:             [add(v) for v in saved.values() if not refs_blocked(user_id,[v])]
        elif low in saved:                       add(saved[low])
        else:                                    invalid.append(part)
    return refs, invalid


def parse_ref_and_email(user_id: int, text: str):
    """Parse 'ref [email]' or 'nickname [email]' from user input.
    Returns (refs, invalid_tokens, email_or_None)."""
    parts = text.strip().split()
    email: str | None = None
    if len(parts) >= 2 and EMAIL_PATTERN.fullmatch(parts[-1]):
        email = parts[-1]
        text  = " ".join(parts[:-1])
    refs, invalid = resolve_tokens(user_id, text)
    return refs, invalid, email


# --------------------------------------------------------------------------- #
# Safe send / markup helpers
# --------------------------------------------------------------------------- #
def safe_send(chat_id, text: str, reply_markup=None, parse_mode: str | None = None):
    if not chat_id: return None
    try:
        return bot.send_message(chat_id, text, reply_markup=reply_markup, parse_mode=parse_mode)
    except Exception as e:
        logger.warning("Could not send to %s: %s", chat_id, e)
        return None


def clear_markup(msg_ref) -> None:
    if not msg_ref: return
    try:
        bot.edit_message_reply_markup(msg_ref[0], msg_ref[1], reply_markup=None)
    except Exception: pass


def msg_ref_of(msg):
    return (msg.chat.id, msg.message_id) if msg else None


def format_script_output(output: str, successful: bool) -> str:
    if not output: return "No output."
    if not successful: return output[-3000:].lstrip()
    readable = []
    for raw in output.splitlines():
        line = raw.strip()
        if not line or "Skipping WhatsApp" in line: continue
        if "] " in line and line[:4].isdigit(): line = line.split("] ",1)[1]
        if line.startswith("attempt "): continue
        readable.append(line)
    if not readable: return "No output."
    selected, total = [], 0
    for line in reversed(readable):
        ln = len(line)+(1 if selected else 0)
        if total+ln > 3000: break
        selected.append(line); total += ln
    return "\n".join(reversed(selected)).lstrip()


# --------------------------------------------------------------------------- #
# Job queue
# --------------------------------------------------------------------------- #
local_job_queue = queue.Queue()
state_lock    = threading.Lock()
waiting_jobs: list = []
current_job         = None
last_finished       = None
job_ids       = itertools.count(1)


class Job:
    def __init__(self, chat_id, user_id, user_name, cmd, title,
                 start_msg=None, admin_text=None, is_scheduled=False):
        self.job_id       = next(job_ids)
        self.chat_id      = chat_id
        self.user_id      = user_id
        self.user_name    = user_name
        self.cmd          = cmd
        self.title        = title
        self.start_msg    = start_msg
        self.admin_text   = admin_text
        self.is_scheduled = is_scheduled
        self.quota_token  = None
        self.cancelled    = False
        self.proc         = None
        self.created_at   = time.time()
        self.started_at: float | None = None
        self.control_msg  = None
        self.queued_msg     = None
        self.remote_job_id: int | None = None   # cloud mode only


def cancel_markup(job: Job):
    kb = types.InlineKeyboardMarkup()
    kb.add(types.InlineKeyboardButton("🛑 Cancel", callback_data=f"cx:{job.job_id}"))
    return kb


def kill_process_tree(proc) -> None:
    try:
        if os.name == "nt":
            subprocess.run(["taskkill","/PID",str(proc.pid),"/T","/F"], capture_output=True)
        else:
            os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
    except Exception:
        try: proc.kill()
        except Exception: pass


def run_job(job: Job):
    if job.cancelled:
        return "Script was cancelled.", "Stopped before it started."

    kwargs = {} if os.name == "nt" else {"start_new_session": True}
    proc = subprocess.Popen(
        job.cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        text=True, errors="replace", **kwargs,
    )
    job.proc = proc
    if job.cancelled: kill_process_tree(proc)

    start_time    = time.time()
    stop_progress = threading.Event()

    def _progress_updater():
        avg = db.get_avg_job_duration(job.title)
        for _ in range(999):
            if stop_progress.wait(15): break
            if job.control_msg and not job.cancelled:
                elapsed = format_duration(int(time.time() - start_time))
                eta     = f" / usually ~{format_duration(int(avg))}" if avg else ""
                try:
                    bot.edit_message_text(
                        f"⏳ Fetching from FESCO… ({elapsed} elapsed{eta})",
                        job.control_msg[0], job.control_msg[1],
                        reply_markup=cancel_markup(job),
                    )
                except Exception: pass

    t = threading.Thread(target=_progress_updater, daemon=True, name=f"progress-{job.job_id}")
    t.start()

    try:
        stdout, stderr = proc.communicate(timeout=RUN_TIMEOUT_SECONDS)
    except subprocess.TimeoutExpired:
        stop_progress.set(); kill_process_tree(proc); proc.communicate(); t.join(2)
        return "Script timed out and was stopped.", f"No result after {RUN_TIMEOUT_SECONDS//60} minutes."
    finally:
        stop_progress.set()
    t.join(2)

    duration = time.time() - start_time
    db.record_job_duration(job.title, duration)

    if job.cancelled:    return "Script was cancelled.", "Stopped by /cancel."
    if proc.returncode == 0:
        return "Script finished successfully!", format_script_output(stdout, True)
    error = (
        format_script_output(stderr, False) if stderr and stderr.strip()
        else (format_script_output(stdout, True) if stdout else "Unknown error.")
    )
    return "Script failed with error:", error


def worker() -> None:
    global current_job, last_finished
    while True:
        job = local_job_queue.get()
        with state_lock:
            if job in waiting_jobs: waiting_jobs.remove(job)
            if job.cancelled: continue
            current_job    = job
            job.started_at = time.time()
        status_msg = "Could not launch the file"
        try:
            activity("job_start", job.user_id, title=job.title)
            clear_markup(job.queued_msg)
            if job.start_msg:
                job.control_msg = msg_ref_of(
                    safe_send(job.chat_id, job.start_msg, reply_markup=cancel_markup(job))
                )
            status_msg, output_content = run_job(job)
            clear_markup(job.control_msg)
            bot.send_message(job.chat_id, f"{status_msg}\n\nOutput:\n{output_content}")
            if job.admin_text and PRIMARY_ADMIN_CHAT_ID and job.chat_id != PRIMARY_ADMIN_CHAT_ID:
                safe_send(PRIMARY_ADMIN_CHAT_ID, job.admin_text(status_msg, output_content))
            if PLAYWRIGHT_SCRIPT_PATH in " ".join(job.cmd):
                _notify_subscribers(status_msg, output_content, job.chat_id)
        except Exception as e:
            err = f"Could not launch the file: {e}"
            logger.exception("Job %s crashed", job.job_id)
            try:
                bot.send_message(job.chat_id, err)
                if PRIMARY_ADMIN_CHAT_ID and job.chat_id != PRIMARY_ADMIN_CHAT_ID:
                    bot.send_message(PRIMARY_ADMIN_CHAT_ID, err)
            except Exception: pass
        finally:
            activity("job_end", job.user_id, title=job.title, result=status_msg)
            clear_markup(job.control_msg)
            with state_lock:
                current_job   = None
                last_finished = {"job": job, "status": status_msg, "at": time.time()}
            local_job_queue.task_done()


def _notify_subscribers(status_msg: str, output_content: str, triggering_chat: int) -> None:
    for uid in db.get_all_subscribers():
        if uid != triggering_chat:
            safe_send(uid, f"📬 Bill run: {status_msg}\n\n{output_content[:400].rstrip()}")


def enqueue(job: Job) -> None:
    if CLOUD_MODE:
        _enqueue_remote(job)
        return
    with state_lock:
        ahead = len(waiting_jobs) + (1 if current_job else 0)
        waiting_jobs.append(job)
    local_job_queue.put(job)
    if ahead:
        job.queued_msg = msg_ref_of(
            safe_send(job.chat_id, f"⏳ Queued. {ahead} job(s) ahead – will start automatically.",
                      reply_markup=cancel_markup(job))
        )


def _enqueue_remote(job: Job) -> None:
    """Cloud mode: push the job to Azure Storage Queue for a worker PC to claim."""
    online   = worker_table.count_online_workers() if _AZURE_AVAILABLE else 0
    raw_args = job.cmd[2:] if len(job.cmd) > 2 else []
    job_id   = job_queue.create_job(
        chat_id   = job.chat_id,
        user_id   = job.user_id,
        user_name = job.user_name,
        title     = job.title,
        cmd_args  = json.dumps(raw_args),
    )
    job.remote_job_id = job_id
    if online:
        msg = f"⏳ Queued — {online} worker PC(s) available. You'll receive your bill shortly."
    else:
        msg = (
            "⏳ Queued — no worker PCs are currently online.\n"
            "Your bill will be fetched automatically as soon as one comes online."
        )
    safe_send(job.chat_id, msg)
    activity("enqueue_remote", job.user_id, title=job.title, job_id=str(job_id)[:8])


def cancel_jobs(user_id: int, job_id: int | None = None):
    if CLOUD_MODE:
        n = db.cancel_pending_jobs_for_user(user_id)
        if n: activity("cancel_remote", user_id, removed=n)
        return None, n

    running, removed = None, []
    with state_lock:
        if current_job and (job_id is None or current_job.job_id == job_id) \
                and (is_admin(user_id) or current_job.user_id == user_id):
            running = current_job; running.cancelled = True
        for job in list(waiting_jobs):
            if job_id is not None and job.job_id != job_id: continue
            if job.user_id == user_id or (job_id is not None and is_admin(user_id)):
                job.cancelled = True; waiting_jobs.remove(job); removed.append(job)
    if running and running.proc: kill_process_tree(running.proc)
    for job in removed:
        clear_markup(job.queued_msg); clear_markup(job.control_msg)
        if job.quota_token: db.refund_usage(job.user_id, job.quota_token)
    if running or removed:
        activity("cancel", user_id, running=bool(running), removed=len(removed))
    return running, len(removed)


def submit_lookup(chat_id, user, refs: list[str], reply_email: str | None = None):
    user_id, name = user.id, user.first_name
    blocked = refs_blocked(user_id, refs)
    if blocked:
        activity("BLOCKED_REF", user, refs=",".join(blocked))
        safe_send(chat_id, "⛔ You are not allowed to look up: " + ", ".join(blocked))
        return None

    refs_text = ", ".join(refs)

    # Determine which refs are in config.json (those get full config delivery)
    in_config = [r for r in refs if ref_in_config(r)]

    cmd = [
        sys.executable, PLAYWRIGHT_SCRIPT_PATH,
        "--refs",           ",".join(refs),
        "--reply-chat-id",  str(chat_id),
        "--force-resend",
    ]
    # Only skip email for refs NOT in config and no manual email provided
    if not in_config and not reply_email:
        cmd.append("--no-email")
    if reply_email:
        cmd += ["--reply-email", reply_email]

    admin_text = None
    if ADMIN_NOTICE_FOR_GETBILLBYREF:
        extra = f" (+ email: {reply_email})" if reply_email else ""
        admin_text = lambda s, o: f"ℹ️ {name} used /getbillbyref for {refs_text}{extra}.\n{s}"

    job = Job(
        chat_id=chat_id, user_id=user_id, user_name=name, cmd=cmd,
        title=f"bill check for {refs_text}",
        start_msg=f"▶️ Starting: bill check for {refs_text}",
        admin_text=admin_text,
    )

    token = uuid.uuid4().hex
    ok, refusal = db.check_and_consume(user_id, len(refs), token)
    if not ok:
        activity("RATE_LIMITED", user, refs=refs_text)
        safe_send(chat_id, refusal); return None
    if not is_admin(user_id): job.quota_token = token

    note = f" (also emailing {reply_email})" if reply_email else ""
    if in_config:
        note += f" (using config.json delivery for: {', '.join(in_config)})"
    activity("lookup", user, refs=refs_text)
    safe_send(chat_id, f"Got {len(refs)} reference number(s): {refs_text}{note}")
    enqueue(job)
    return job


# --------------------------------------------------------------------------- #
# /help  /start  /myaccess
# --------------------------------------------------------------------------- #
@bot.message_handler(commands=["help","start","myaccess"])
def help_command(message) -> None:
    uid = message.from_user.id
    if not has_any_access(uid):
        activity("DENIED", message.from_user, cmd=command_name(message))
        bot.reply_to(message, f"Unauthorized access.\nYour Telegram ID is {uid} — send it to the admin to request access.")
        return
    activity("command", message.from_user, cmd=command_name(message))
    lines = ["Commands you can use:"]
    if has_access(uid, "run"):       lines.append("/run — check all configured bills")
    if has_access(uid, "getbillbyref"):
        lines += [
            "/getbillbyref [ref or nickname [email]] — fetch specific bill(s)\n"
            "   • If the ref is in config.json → delivers to all configured recipients\n"
            "   • Otherwise → sends only to you on Telegram\n"
            "   • Add an email address to also send by email, e.g.: /getbillbyref shop user@email.com",
            "/lastbill <ref or nickname> — show the last saved bill image",
            "/history <ref or nickname> — recent bills with amounts and trend",
            "/saved — your saved nicknames",
            "/save <nickname> <ref> — save a nickname",
            "/unsave <nickname> — remove a nickname",
            "/mylimit — your lookup limits and usage",
            "/paid <ref or nickname> [note] — log a payment",
            "/subscribe — get notified when any bill run completes",
            "/unsubscribe — stop those notifications",
        ]
    lines += ["/status — what is running right now","/cancel — stop your running / waiting jobs","/help — this list"]
    if is_admin(uid):
        lines += [
            "\nAdmin commands:",
            "/users — everyone with access, limits and allowed refs",
            "/adduser <id> <run|getbillbyref|all> [name] [--expires 7d|24h|2w]",
            "/removeuser <id> [run|getbillbyref]",
            "/setlimit <id|default> <hour|day> <number|off|default>",
            "/sethours <id> <start>-<end> — time-gate access (e.g. 9-22), or 'off'",
            "/allowref <id> <ref,ref | any>",
            "/denyref <id> <ref,ref>",
            "/activity [n] — recent activity log",
            "/logs [n] — last n lines of bot.log (default 20)",
            "/health — bot, system and network health",
            "/reload — reload config.json without restarting",
            "/backup — send bot.db to this chat",
        ]
    bot.reply_to(message, "\n".join(lines))


# --------------------------------------------------------------------------- #
# /status
# --------------------------------------------------------------------------- #
@bot.message_handler(commands=["status"])
def status_command(message) -> None:
    if not check_any_access(message): return
    uid = message.from_user.id
    now = time.time()

    if CLOUD_MODE:
        pending = job_queue.get_pending_count() if _AZURE_AVAILABLE else "?"
        workers = worker_table.get_online_workers() if _AZURE_AVAILABLE else []
        lines = [
            "☁️ Cloud mode (Azure Container Apps)",
            f"• Workers online: {len(workers)}" +
            ((" — " + ", ".join(w['name'] for w in workers)) if workers else " (none)"),
            f"• Jobs in queue: {pending}",
        ]
        bot.reply_to(message, "\n".join(lines))
        return

    with state_lock:
        cur   = current_job
        waits = list(waiting_jobs)
        last  = last_finished

    def describe(job):
        return (f"{job.title} ({job.user_name})" if is_admin(uid) or job.user_id==uid
                else "another user's job")

    lines = []
    if cur:
        elapsed = format_duration(now - (cur.started_at or now))
        avg     = db.get_avg_job_duration(cur.title)
        eta     = f" / usually ~{format_duration(int(avg))}" if avg else ""
        lines.append(f"▶️ Running: {describe(cur)} — {elapsed}{eta}")
    else:
        lines.append("✅ Idle — nothing is running.")
    if waits:
        lines.append(f"⏳ Waiting: {len(waits)}")
        for i, j in enumerate(waits, 1): lines.append(f"  {i}. {describe(j)}")
    if last and (is_admin(uid) or last["job"].user_id == uid):
        lines.append(f"\nLast job: {last['job'].title}\n{last['status']} ({format_duration(now-last['at'])} ago)")
    bot.reply_to(message, "\n".join(lines))


@bot.message_handler(commands=["workers"])
def workers_command(message) -> None:
    if not check_admin(message): return
    if not CLOUD_MODE:
        bot.reply_to(message, "This command is only available in cloud mode."); return
    workers = worker_table.get_online_workers() if _AZURE_AVAILABLE else []
    if not workers:
        bot.reply_to(message, "🔴 No worker PCs are currently online."); return
    lines = [f"🟢 {len(workers)} worker(s) online:"]
    for w in workers:
        lines.append(f"• {w['name']}  last seen: {w['last_heartbeat'][:16]}  IP: {w.get('ip','?')}")
    bot.reply_to(message, "\n".join(lines))


# --------------------------------------------------------------------------- #
# /run
# --------------------------------------------------------------------------- #
@bot.message_handler(commands=["run"])
def trigger_external_script(message) -> None:
    if not check_access(message, "run"): return
    name = message.from_user.first_name
    cmd  = [sys.executable, PLAYWRIGHT_SCRIPT_PATH, "--force-resend", "--no-email"]
    job  = Job(
        chat_id=message.chat.id, user_id=message.from_user.id, user_name=name,
        cmd=cmd, title="full bill run",
        admin_text=lambda s,o: f"ℹ️ {name} triggered the script.\n{s}\n\nOutput:\n{o}",
    )
    reply = bot.reply_to(message, "⏳ Starting full bill run…", reply_markup=cancel_markup(job))
    job.control_msg = msg_ref_of(reply)
    enqueue(job)


# --------------------------------------------------------------------------- #
# /getbillbyref
# --------------------------------------------------------------------------- #
def saved_buttons(user_id: int):
    kb = types.InlineKeyboardMarkup(row_width=2)
    buttons, seen = [], set()
    for nick, ref in get_saved(user_id).items():
        if ref in seen or refs_blocked(user_id, [ref]): continue
        seen.add(ref)
        amount = last_bill_amount(ref)
        label  = f"📄 {nick}" + (f" — Rs.{amount}" if amount else "")
        buttons.append(types.InlineKeyboardButton(label, callback_data=f"ref:{ref}"))
    if buttons:
        kb.add(*buttons)
        if len(buttons) > 1:
            kb.add(types.InlineKeyboardButton("📚 All saved", callback_data="ref:all"))
    kb.add(types.InlineKeyboardButton("✖️ Cancel", callback_data="cancel_prompt"))
    return kb


@bot.message_handler(commands=["getbillbyref"])
def ask_for_reference(message) -> None:
    if not check_access(message, "getbillbyref"): return
    uid         = message.from_user.id
    inline_text = command_args(message)

    if inline_text:
        refs, invalid, email = parse_ref_and_email(uid, inline_text)
        if refs and not invalid:
            submit_lookup(message.chat.id, message.from_user, refs, reply_email=email)
        else:
            bot.reply_to(message,
                "Not a valid reference number or saved nickname: "
                + ", ".join(invalid or [inline_text])
                + "\nReference numbers must be digits only. Use /saved to see your nicknames.",
            )
        return

    text = (
        "Send the reference number.\n"
        "For more than one, separate with commas: 12345678901234, 12345678901235\n"
        "To also send by email, add the address after the ref: 12345678901234 you@email.com\n"
    )
    if get_saved(uid):
        text += "Or tap a saved nickname below.\n"
    text += "\n(Send /cancel to abort.)"
    msg = bot.reply_to(message, text, reply_markup=saved_buttons(uid))
    bot.register_next_step_handler(msg, receive_references)


def receive_references(message) -> None:
    if not has_access(message.from_user.id, "getbillbyref"):
        bot.reply_to(message, "Unauthorized access.")
        bot.register_next_step_handler(message, receive_references)
        return
    text = (message.text or "").strip()
    if text.lower().startswith("/cancel"):
        bot.reply_to(message, "Request cancelled. (Send /cancel again to stop a running script.)")
        return
    if text.startswith("/"):
        bot.reply_to(message, "Previous reference request cancelled.")
        bot.process_new_messages([message]); return

    refs, invalid, email = parse_ref_and_email(message.from_user.id, text)
    if not refs or invalid:
        msg = bot.reply_to(message,
            "Invalid input" + (f": {', '.join(invalid)}" if invalid else "")
            + ".\nSend a reference number, optionally followed by an email address. Try again (or /cancel).",
        )
        bot.register_next_step_handler(msg, receive_references); return
    submit_lookup(message.chat.id, message.from_user, refs, reply_email=email)


@bot.callback_query_handler(
    func=lambda call: bool(call.data) and (call.data.startswith("ref:") or call.data=="cancel_prompt")
)
def prompt_button(call) -> None:
    uid, chat_id = call.from_user.id, call.message.chat.id
    if not has_access(uid, "getbillbyref"):
        bot.answer_callback_query(call.id, "Unauthorized access."); return
    bot.clear_step_handler_by_chat_id(chat_id)
    clear_markup((chat_id, call.message.message_id))
    if call.data == "cancel_prompt":
        bot.answer_callback_query(call.id, "Cancelled."); return
    choice = call.data[4:]
    if choice == "all":
        refs = [r for r in dict.fromkeys(get_saved(uid).values()) if not refs_blocked(uid,[r])]
    elif REF_PATTERN.fullmatch(choice): refs = [choice]
    else:                               refs = []
    if not refs: bot.answer_callback_query(call.id, "Nothing saved."); return
    bot.answer_callback_query(call.id)
    activity("button_lookup", call.from_user, choice=choice)
    submit_lookup(chat_id, call.from_user, refs)


# --------------------------------------------------------------------------- #
# /saved  /save  /unsave
# --------------------------------------------------------------------------- #
@bot.message_handler(commands=["saved"])
def saved_command(message) -> None:
    if not check_access(message, "getbillbyref"): return
    saved = get_saved(message.from_user.id)
    if not saved:
        bot.reply_to(message, "No saved nicknames yet.\nSave one with: /save shop 12345678901234"); return
    history = read_json(BILL_HISTORY_FILE, {})
    lines = ["Your saved references:"]
    for nick, ref in saved.items():
        entries = history.get(ref, [])
        amount  = entries[-1].get("payable_within_due") if entries else None
        lines.append(f"• {nick} → {ref}" + (f"  (Rs. {amount})" if amount else ""))
    lines.append("\nUse them like: /getbillbyref shop   (or  /getbillbyref all)")
    bot.reply_to(message, "\n".join(lines))


@bot.message_handler(commands=["save"])
def save_command(message) -> None:
    if not check_access(message, "getbillbyref"): return
    uid   = message.from_user.id
    parts = command_args(message).split()
    if len(parts) != 2:
        bot.reply_to(message, "Usage: /save <nickname> <reference number>\nExample: /save shop 12345678901234"); return
    nick, ref = parts[0], parts[1]
    if not NICKNAME_PATTERN.fullmatch(nick) or nick.lower()=="all":
        bot.reply_to(message, 'Nickname must start with a letter, use only letters/digits/- or _ (max 20 chars), cannot be "all".'); return
    if not REF_PATTERN.fullmatch(ref):
        bot.reply_to(message, "The reference number must be digits only."); return
    if refs_blocked(uid, [ref]):
        bot.reply_to(message, "⛔ You are not allowed to use that reference number."); return
    if nick.lower() not in get_saved(uid) and db.count_saved(uid) >= MAX_SAVED_PER_USER:
        bot.reply_to(message, f"You can save at most {MAX_SAVED_PER_USER} nicknames. Remove one with /unsave first."); return
    db.save_ref(uid, nick, ref)
    bot.reply_to(message, f"Saved: {nick.lower()} → {ref}")


@bot.message_handler(commands=["unsave"])
def unsave_command(message) -> None:
    if not check_access(message, "getbillbyref"): return
    nick = command_args(message).strip().lower()
    if not nick: bot.reply_to(message, "Usage: /unsave <nickname>"); return
    if not db.unsave_ref(message.from_user.id, nick):
        bot.reply_to(message, f'No saved nickname called "{nick}". See /saved.'); return
    bot.reply_to(message, f"Removed: {nick}")


# --------------------------------------------------------------------------- #
# /lastbill  /history
# --------------------------------------------------------------------------- #
def single_ref_from_args(message, usage: str):
    arg = command_args(message)
    if not arg:
        saved = get_saved(message.from_user.id)
        extra = ("\nYour nicknames: " + ", ".join(saved)) if saved else ""
        bot.reply_to(message, usage + extra); return None
    refs, invalid = resolve_tokens(message.from_user.id, arg.split()[0])
    if len(refs) != 1 or invalid:
        bot.reply_to(message, f"Not a valid reference number or saved nickname: {arg.split()[0]}"); return None
    if refs_blocked(message.from_user.id, refs):
        activity("BLOCKED_REF", message.from_user, refs=refs[0])
        bot.reply_to(message, "⛔ You are not allowed to look up that reference number."); return None
    return refs[0]


@bot.message_handler(commands=["lastbill"])
def lastbill_command(message) -> None:
    if not check_access(message, "getbillbyref"): return
    ref = single_ref_from_args(message, "Usage: /lastbill <reference number or nickname>")
    if not ref: return
    image = BILLS_DIR / f"{ref}.png"
    if not image.exists():
        bot.reply_to(message, f"No saved bill image for {ref} yet. Use /getbillbyref to fetch it first."); return
    mtime    = image.stat().st_mtime
    saved_at = datetime.fromtimestamp(mtime).strftime("%d %b %Y, %I:%M %p")
    age_days = (time.time() - mtime) / 86400
    stale    = age_days > STALE_BILL_DAYS
    try:
        with open(image, "rb") as f:
            caption = f"Reference {ref}\nSaved copy from {saved_at} (not refreshed)."
            if stale:
                caption += f"\n⚠️ This image is {int(age_days)} days old — run /getbillbyref to refresh it."
            else:
                caption += "\nUse /getbillbyref for the latest."
            bot.send_photo(message.chat.id, f, caption=caption)
    except Exception as e:
        bot.reply_to(message, f"Could not send the saved bill: {e}")


@bot.message_handler(commands=["history"])
def history_command(message) -> None:
    if not check_access(message, "getbillbyref"): return
    ref = single_ref_from_args(message, "Usage: /history <reference number or nickname>")
    if not ref: return
    entries = read_json(BILL_HISTORY_FILE, {}).get(ref, [])
    if not entries:
        bot.reply_to(message, f"No history for {ref} yet."); return
    months  = list(reversed(entries[-6:]))
    amounts = []
    lines   = [f"📋 Recent bills for {ref}:"]
    for e in months:
        paid_marker = ""
        for p in db.get_payment_history(ref):
            if e.get("bill_month","") and e["bill_month"] in p.get("paid_at",""):
                paid_marker = " ✅ paid"; break
        amt = e.get("payable_within_due")
        if amt:
            try: amounts.append(float(str(amt).replace(",","")))
            except ValueError: pass
        lines.append(
            f"• {e.get('bill_month') or '?'} — {e.get('units') or '?'} units — "
            f"Rs. {amt or '?'} — due {e.get('due_date') or '?'}{paid_marker}"
        )
    # Trend + year-over-year
    if len(amounts) >= 2:
        diff = amounts[0] - amounts[1]
        icon = "📈" if diff > 0 else "📉"
        lines.append(f"\n{icon} {'Up' if diff>0 else 'Down'} Rs. {abs(diff):,.0f} from last month")
    if len(entries) >= 13:
        try:
            this_y = float(str(entries[-1].get("payable_within_due","")).replace(",",""))
            last_y = float(str(entries[-13].get("payable_within_due","")).replace(",",""))
            diff   = this_y - last_y
            lines.append(f"📅 Year-over-year: {'Up' if diff>=0 else 'Down'} Rs. {abs(diff):,.0f}")
        except (ValueError, TypeError): pass
    bot.reply_to(message, "\n".join(lines))


# --------------------------------------------------------------------------- #
# /mylimit  /subscribe  /unsubscribe  /paid
# --------------------------------------------------------------------------- #
@bot.message_handler(commands=["mylimit"])
def mylimit_command(message) -> None:
    if not check_access(message, "getbillbyref"): return
    uid = message.from_user.id
    if is_admin(uid): bot.reply_to(message, "You are an admin — no limits."); return
    report = db.usage_report(uid)
    lines  = ["Your lookup limits:"]
    for w, label in (("hour","last hour"),("day","last 24 hours")):
        used, limit = report[w]
        lines.append(f"• {label}: {used} used" + (f" of {limit}" if limit else " (no limit)"))
    ar = allowed_refs(uid)
    lines.append("• references: " + ("any" if ar is None else (", ".join(ar) or "none allowed")))
    gate = db.get_allowed_hours(uid)
    if gate: lines.append(f"• access hours: {gate[0]:02d}:00–{gate[1]:02d}:00")
    exp  = db.get_user_expiry(uid)
    if exp: lines.append(f"• access expires: {exp.strftime('%d %b %Y %H:%M UTC')}")
    bot.reply_to(message, "\n".join(lines))


@bot.message_handler(commands=["subscribe"])
def subscribe_command(message) -> None:
    if not check_any_access(message): return
    if db.subscribe(message.from_user.id):
        bot.reply_to(message, "✅ Subscribed. You'll receive a summary whenever a bill run completes.\nUse /unsubscribe to stop.")
    else:
        bot.reply_to(message, "You are already subscribed. Use /unsubscribe to stop.")


@bot.message_handler(commands=["unsubscribe"])
def unsubscribe_command(message) -> None:
    if not check_any_access(message): return
    if db.unsubscribe(message.from_user.id):
        bot.reply_to(message, "✅ Unsubscribed.")
    else:
        bot.reply_to(message, "You were not subscribed. Use /subscribe to start.")


@bot.message_handler(commands=["paid"])
def paid_command(message) -> None:
    if not check_access(message, "getbillbyref"): return
    parts = command_args(message).split(maxsplit=1)
    if not parts:
        bot.reply_to(message, "Usage: /paid <ref or nickname> [note]\nExample: /paid shop via JazzCash"); return
    refs, invalid = resolve_tokens(message.from_user.id, parts[0])
    if not refs or invalid:
        bot.reply_to(message, f"Not a valid reference number or saved nickname: {parts[0]}"); return
    ref  = refs[0]
    note = parts[1].strip() if len(parts)>1 else None
    if refs_blocked(message.from_user.id, [ref]):
        bot.reply_to(message, "⛔ You are not allowed to use that reference number."); return
    paid_at = db.log_payment(ref, message.from_user.id, note)
    activity("paid", message.from_user, ref=ref, note=note or "")
    msg = f"✅ Payment logged for {ref} at {paid_at}."
    if note: msg += f"\nNote: {note}"
    bot.reply_to(message, msg)


# --------------------------------------------------------------------------- #
# Admin: /adduser /removeuser /users /setlimit /sethours /allowref /denyref
# --------------------------------------------------------------------------- #
def parse_commands(text: str) -> list[str] | None:
    wanted = []
    for part in text.lower().split(","):
        part = part.strip().lstrip("/")
        if part=="all":       wanted += list(VALID_COMMANDS)
        elif part in VALID_COMMANDS: wanted.append(part)
        else:                 return None
    return sorted(set(wanted))


def parse_expiry(text: str) -> datetime | None:
    """Parse '7d', '24h', '2w' → UTC datetime.  Returns None if invalid."""
    m = re.fullmatch(r"(\d+)([dhwDHW])", text.strip())
    if not m: return None
    n, unit = int(m.group(1)), m.group(2).lower()
    delta = {"d": timedelta(days=n), "h": timedelta(hours=n), "w": timedelta(weeks=n)}.get(unit)
    return datetime.now(timezone.utc) + delta if delta else None


@bot.message_handler(commands=["adduser"])
def adduser_command(message) -> None:
    if not check_admin(message): return
    raw   = command_args(message).split()
    # syntax: /adduser <id> <cmds> [name ...] [--expires 7d]
    expires: datetime | None = None
    tokens = list(raw)
    if "--expires" in tokens:
        idx = tokens.index("--expires")
        if idx + 1 < len(tokens):
            expires = parse_expiry(tokens[idx+1])
            if not expires:
                bot.reply_to(message, "Invalid --expires value. Use e.g. 7d, 24h, 2w."); return
            tokens = tokens[:idx] + tokens[idx+2:]
        else:
            bot.reply_to(message, "--expires needs a duration (e.g. --expires 7d)."); return

    if len(tokens) < 2 or not tokens[0].isdigit() or parse_commands(tokens[1]) is None:
        bot.reply_to(message,
            "Usage: /adduser <telegram id> <run|getbillbyref|all> [name] [--expires 7d|24h|2w]\n"
            "Example: /adduser 123456789 getbillbyref Ali --expires 30d"); return

    uid, cmds = int(tokens[0]), parse_commands(tokens[1])
    name = " ".join(tokens[2:]).strip()[:40] or None
    if is_admin(uid): bot.reply_to(message, "That person is already an admin."); return

    db.add_user(uid, cmds, name)
    if expires: db.set_user_expiry(uid, expires)
    activity("ADMIN adduser", message.from_user, target=uid, commands=",".join(cmds),
             expires=expires.strftime("%d %b %Y %H:%M UTC") if expires else "never")

    exp_note = f"\nAccess expires: {expires.strftime('%d %b %Y %H:%M UTC')}" if expires else ""
    bot.reply_to(message, f"✅ {uid}{' (' + name + ')' if name else ''} can now use: {', '.join(cmds)}{exp_note}")
    safe_send(uid, f"You now have access to: {', '.join(cmds)}. Send /help to see what you can do.{exp_note}")


@bot.message_handler(commands=["removeuser"])
def removeuser_command(message) -> None:
    if not check_admin(message): return
    parts = command_args(message).split()
    if not parts or not parts[0].isdigit() or len(parts)>2:
        bot.reply_to(message,"Usage: /removeuser <telegram id> [run|getbillbyref]\nWithout a command removes the person completely."); return
    uid, cmds = int(parts[0]), None
    if len(parts)==2:
        cmds = parse_commands(parts[1])
        if cmds is None: bot.reply_to(message,"The command must be run, getbillbyref or all."); return
    existed = db.remove_user(uid, cmds)
    activity("ADMIN removeuser", message.from_user, target=uid, commands=",".join(cmds) if cmds else "ALL")
    env_cmds = [c for c,ids in COMMAND_ACCESS.items() if uid in ids]
    text = "✅ Updated." if existed else f"{uid} was not in the managed list."
    if env_cmds: text += f"\n⚠️ {uid} is also in .env for: {', '.join(env_cmds)} — remove them there too."
    bot.reply_to(message, text)


@bot.message_handler(commands=["users"])
def users_command(message) -> None:
    if not check_admin(message): return
    all_users = db.get_all_users()
    defaults  = db.get_access_defaults()
    ids: set[int] = set(ALLOWED_USER_IDS)
    for s in COMMAND_ACCESS.values(): ids |= s
    ids |= set(all_users)
    lines = []
    for uid in sorted(ids, key=lambda u: (not is_admin(u), u)):
        entry = all_users.get(uid, {})
        name  = f" {entry['name']}" if entry.get("name") else ""
        if is_admin(uid): lines.append(f"👑 {uid}{name} — admin"); continue
        cmds  = sorted({c for c,s in COMMAND_ACCESS.items() if uid in s} | set(entry.get("commands",[])))
        refs  = entry.get("refs")
        exp   = db.get_user_expiry(uid)
        gate  = db.get_allowed_hours(uid)
        extra = ""
        if exp:  extra += f" | expires {exp.strftime('%d %b')}"
        if gate: extra += f" | hours {gate[0]:02d}-{gate[1]:02d}"
        lines.append(
            f"• {uid}{name} — {', '.join(cmds) or 'no cmds'}"
            f" | limit: {describe_limits(db.effective_limits(uid))}"
            f" | refs: {'any' if refs is None else len(refs)}{extra}"
        )
    lines.append(f"\nDefault limit: {describe_limits({w: defaults.get(w) for w in db.WINDOWS})}")
    bot.reply_to(message, ("\n".join(lines) or "No users configured.")[:3900])


def parse_limit_value(text: str):
    low = text.lower()
    if low in ("off","none","unlimited"): return None
    if low == "default":                  return "default"
    if low.isdigit() and int(low)<=1_000_000: return int(low)
    return False


@bot.message_handler(commands=["setlimit"])
def setlimit_command(message) -> None:
    if not check_admin(message): return
    parts = command_args(message).split()
    usage = ("Usage: /setlimit <id|default> <hour|day> <number|off|default>\n"
             "Examples: /setlimit 123456789 hour 5  |  /setlimit default day 30")
    if len(parts)!=3 or parts[1].lower() not in db.WINDOWS or (parts[0]!="default" and not parts[0].isdigit()):
        bot.reply_to(message,usage); return
    window, value = parts[1].lower(), parse_limit_value(parts[2])
    if value is False: bot.reply_to(message,usage); return
    if parts[0]=="default":
        if value=="default": bot.reply_to(message,usage); return
        db.set_access_default(window, value); target_text="everyone without their own limit"
    else:
        uid = int(parts[0])
        if is_admin(uid): bot.reply_to(message,"Admins are never limited."); return
        db.set_user_limit(uid, window, value); target_text=str(uid)
    activity("ADMIN setlimit", message.from_user, target=parts[0], window=window, value=parts[2])
    shown = "the default" if value=="default" else ("unlimited" if value is None else f"{value} per {window}")
    bot.reply_to(message, f"✅ Lookup limit for {target_text} ({window}): {shown}")


@bot.message_handler(commands=["sethours"])
def sethours_command(message) -> None:
    if not check_admin(message): return
    parts = command_args(message).split()
    usage = "Usage: /sethours <telegram id> <start>-<end>  (e.g. 9-22)  or  off\nExample: /sethours 123456789 9-22"
    if len(parts) != 2 or not parts[0].isdigit():
        bot.reply_to(message, usage); return
    uid = int(parts[0])
    if parts[1].lower() == "off":
        db.remove_allowed_hours(uid)
        activity("ADMIN sethours", message.from_user, target=uid, hours="off")
        bot.reply_to(message, f"✅ Time gate removed for {uid} — they can access any time."); return
    m = re.fullmatch(r"(\d{1,2})-(\d{1,2})", parts[1])
    if not m or not (0<=int(m.group(1))<=23) or not (0<=int(m.group(2))<=23):
        bot.reply_to(message, usage); return
    start, end = int(m.group(1)), int(m.group(2))
    db.set_allowed_hours(uid, start, end)
    activity("ADMIN sethours", message.from_user, target=uid, hours=f"{start}-{end}")
    bot.reply_to(message, f"✅ {uid} can now only access between {start:02d}:00 and {end:02d}:00.")


@bot.message_handler(commands=["allowref"])
def allowref_command(message) -> None:
    if not check_admin(message): return
    parts = command_args(message).split(maxsplit=1)
    usage = ("Usage: /allowref <telegram id> <ref[,ref...] | any>\n"
             "First /allowref restricts the person to listed refs only.\n"
             "/allowref <id> any removes the restriction.")
    if len(parts)!=2 or not parts[0].isdigit(): bot.reply_to(message,usage); return
    uid = int(parts[0])
    if is_admin(uid): bot.reply_to(message,"Admins can look up any reference."); return
    if parts[1].strip().lower()=="any":
        db.set_allowed_refs_unrestricted(uid)
        activity("ADMIN allowref", message.from_user, target=uid, refs="any")
        bot.reply_to(message, f"✅ {uid} may look up any reference."); return
    refs = [r for r in re.split(r"[,\s]+", parts[1]) if r]
    if not refs or any(not REF_PATTERN.fullmatch(r) for r in refs):
        bot.reply_to(message,"Reference numbers must be digits only.\n"+usage); return
    full = db.add_allowed_refs(uid, refs)
    activity("ADMIN allowref", message.from_user, target=uid, refs=",".join(refs))
    bot.reply_to(message, f"✅ {uid} may now look up only: {', '.join(full)}")


@bot.message_handler(commands=["denyref"])
def denyref_command(message) -> None:
    if not check_admin(message): return
    parts = command_args(message).split(maxsplit=1)
    if len(parts)!=2 or not parts[0].isdigit():
        bot.reply_to(message,"Usage: /denyref <telegram id> <ref[,ref...]>"); return
    uid  = int(parts[0])
    refs = [r for r in re.split(r"[,\s]+", parts[1]) if r]
    if allowed_refs(uid) is None:
        bot.reply_to(message, f"{uid} has no restriction yet. Use /allowref first."); return
    left = db.remove_allowed_refs(uid, refs)
    activity("ADMIN denyref", message.from_user, target=uid, refs=",".join(refs))
    bot.reply_to(message, f"✅ {uid} may now look up: " + (", ".join(left) if left else "nothing"))


# --------------------------------------------------------------------------- #
# /cancel  +  Cancel button
# --------------------------------------------------------------------------- #
@bot.message_handler(commands=["cancel"])
def cancel_command(message) -> None:
    if not check_any_access(message): return
    running, removed = cancel_jobs(message.from_user.id)
    parts = []
    if running:  parts.append("🛑 Stopping the running script…")
    if removed:  parts.append(f"Removed {removed} waiting job(s).")
    bot.reply_to(message, "\n".join(parts) if parts else "Nothing to cancel.")


@bot.callback_query_handler(func=lambda call: bool(call.data) and call.data.startswith("cx:"))
def cancel_button(call) -> None:
    if not has_any_access(call.from_user.id):
        bot.answer_callback_query(call.id,"Unauthorized access."); return
    try:   job_id = int(call.data[3:])
    except ValueError: bot.answer_callback_query(call.id); return
    running, removed = cancel_jobs(call.from_user.id, job_id=job_id)
    if running:   bot.answer_callback_query(call.id,"Stopping the script…")
    elif removed: bot.answer_callback_query(call.id,"Removed from the queue.")
    else:
        with state_lock:
            still = (current_job and current_job.job_id==job_id) or any(j.job_id==job_id for j in waiting_jobs)
        if still: bot.answer_callback_query(call.id,"Only the owner (or admin) can cancel this.")
        else:
            bot.answer_callback_query(call.id,"That job has already finished.")
            clear_markup((call.message.chat.id, call.message.message_id))


# --------------------------------------------------------------------------- #
# Admin: /activity /logs /health /reload /backup
# --------------------------------------------------------------------------- #
@bot.message_handler(commands=["activity"])
def activity_command(message) -> None:
    if not check_admin(message): return
    arg   = command_args(message)
    count = int(arg) if arg.isdigit() else 15
    lines = read_activity_tail(max(1,min(count,50)))
    if not lines: bot.reply_to(message,"No activity recorded yet."); return
    bot.reply_to(message, "\n".join(lines)[-3900:])


@bot.message_handler(commands=["logs"])
def logs_command(message) -> None:
    if not check_admin(message): return
    arg   = command_args(message)
    count = max(1, min(int(arg) if arg.isdigit() else 20, 100))
    lines = read_log_tail(count)
    if not lines: bot.reply_to(message,"No log entries found."); return
    bot.reply_to(message, f"Last {len(lines)} lines of bot.log:\n\n" + "\n".join(lines)[-3800:])


@bot.message_handler(commands=["reload"])
def reload_command(message) -> None:
    if not check_admin(message): return
    problems = validate_config()
    n = reload_config()
    if problems:
        bot.reply_to(message, f"⚠️ Config reloaded ({n} bills) but has problems:\n• " + "\n• ".join(problems))
    else:
        bot.reply_to(message, f"✅ Config reloaded — {n} bill(s) in config.json.")
    activity("ADMIN reload", message.from_user, bills=n)


@bot.message_handler(commands=["backup"])
def backup_command(message) -> None:
    if not check_admin(message): return
    if not BOT_DB_FILE.exists():
        bot.reply_to(message,"bot.db not found."); return
    try:
        with open(BOT_DB_FILE, "rb") as f:
            size_kb = BOT_DB_FILE.stat().st_size // 1024
            bot.send_document(
                message.chat.id, f,
                caption=f"bot.db — {size_kb} KB — {datetime.now():%d %b %Y %H:%M}",
                visible_file_name="bot.db",
            )
        activity("ADMIN backup", message.from_user)
    except Exception as e:
        bot.reply_to(message, f"Could not send backup: {e}")


def _ping_fesco() -> tuple[bool, float]:
    try:
        t0 = time.time()
        r  = requests.head("https://bill.pitc.com.pk/fescobill", timeout=10, allow_redirects=True)
        return r.status_code < 500, (time.time()-t0)*1000
    except Exception:
        return False, 0.0


def startup_problems() -> list[str]:
    p = []
    if not ALLOWED_USER_IDS:
        p.append("ALLOWED_USER_ID is empty — nobody is admin")
    if CLOUD_MODE:
        if not AZURE_CONN_STR:
            p.append("AZURE_STORAGE_CONNECTION_STRING not set")
        if not WEBHOOK_URL:
            p.append("WEBHOOK_URL not set — Telegram cannot reach this container")
    else:
        # Local mode only: script must exist locally
        if not PLAYWRIGHT_SCRIPT_PATH:
            p.append("PLAYWRIGHT_SCRIPT_PATH not set")
        elif not Path(PLAYWRIGHT_SCRIPT_PATH).is_file():
            p.append(f"Script not found: {PLAYWRIGHT_SCRIPT_PATH}")
    p += validate_config()
    p += check_env_completeness(CONFIG_PATH)
    return p


@bot.message_handler(commands=["health"])
def health_command(message) -> None:
    if not check_admin(message): return
    now   = time.time()
    lines = [
        "🩺 Bot health",
        f"• Uptime: {format_duration(now-STARTED_AT)}",
        f"• Python: {sys.version.split()[0]}",
        f"• Schedule: {'every day at %02d:00' % BILL_CHECK_HOUR if BILL_CHECK_HOUR is not None else 'not configured (set BILL_CHECK_HOUR in .env)'}",
        f"• API: {'⚠️ direct api.telegram.org (Worker down)' if _using_fallback else '✅ Cloudflare Worker'}",
        f"• Config: {len(_config_refs)} bill(s) in config.json",
    ]
    p = startup_problems()
    lines.append("• Script: " + ("OK" if not p else p[0]))
    with state_lock: cur, waiting = current_job, len(waiting_jobs)
    lines.append("• Queue: " + (f"running '{cur.title}', {waiting} waiting" if cur else f"idle, {waiting} waiting"))
    lines.append("• Script lock: " + ("present (run in progress or crashed)" if RUN_LOCK_FILE.exists() else "free"))
    if not CLOUD_MODE:
        reachable, ms = _ping_fesco()
        lines.append(f"• FESCO website: {'✅ reachable' if reachable else '❌ unreachable'}" + (f" ({ms:.0f} ms)" if reachable else ""))
    else:
        lines.append(f"• FESCO website: not checked (workers scrape from their own IPs)")
    try:
        files   = list(BILLS_DIR.iterdir()) if BILLS_DIR.exists() else []
        size_mb = sum(f.stat().st_size for f in files if f.is_file())/1_048_576
        free_mb = shutil.disk_usage(BILLS_DIR if BILLS_DIR.exists() else ".").free/1_048_576
        lines.append(f"• Saved bills: {len(files)} files, {size_mb:.1f} MB | {free_mb:,.0f} MB free" +
                     (" ⚠️ LOW" if free_mb < DISK_ALERT_MB else ""))
    except OSError as e:
        lines.append(f"• Saved bills: could not read ({e})")
    if BILL_HISTORY_FILE.exists():
        lines.append(f"• Last successful fetch: {datetime.fromtimestamp(BILL_HISTORY_FILE.stat().st_mtime):%d %b %Y, %I:%M %p}")
    if last_finished:
        lines.append(f"• Last job: {last_finished['job'].title} — {last_finished['status']} ({format_duration(now-last_finished['at'])} ago)")
    all_users = db.get_all_users()
    lines.append(f"• People: {len(ALLOWED_USER_IDS)} admin(s), {len(all_users)} managed, {len(db.get_all_subscribers())} subscriber(s)")
    if CLOUD_MODE and _AZURE_AVAILABLE:
        workers = worker_table.get_online_workers()
        lines.append(f"• Workers online: {len(workers)}" +
                     (" — " + ", ".join(w['name'] for w in workers) if workers else " (none)"))
        lines.append(f"• Jobs in queue: {job_queue.get_pending_count()}")
    bot.reply_to(message, "\n".join(lines))


# --------------------------------------------------------------------------- #
# Startup, single-instance, polling loop
# --------------------------------------------------------------------------- #
def setup_logging() -> None:
    BOT_LOG_FILE.parent.mkdir(parents=True, exist_ok=True)
    handler = logging.handlers.RotatingFileHandler(
        BOT_LOG_FILE, maxBytes=LOG_MAX_BYTES, backupCount=LOG_BACKUP_COUNT, encoding="utf-8",
    )
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        handlers=[handler, logging.StreamHandler(sys.stdout)],
    )
    logging.getLogger("telebot").setLevel(logging.WARNING)
    ACTIVITY_LOG_FILE.parent.mkdir(parents=True, exist_ok=True)
    configure_activity_log(ACTIVITY_LOG_FILE)
    sys.excepthook = lambda et,e,tb: logger.critical("Uncaught exception", exc_info=(et,e,tb))
    threading.excepthook = lambda a: logger.critical(
        "Uncaught exception in thread %s", getattr(a.thread,"name","?"),
        exc_info=(a.exc_type, a.exc_value, a.exc_traceback),
    )


_instance_guard = None


def acquire_single_instance(lock_path=None) -> bool:
    global _instance_guard
    path = Path(lock_path or BOT_LOCK_FILE)
    if os.name == "nt":
        import ctypes
        k32 = ctypes.WinDLL("kernel32", use_last_error=True)
        k32.CreateMutexW.argtypes = [ctypes.c_void_p, ctypes.c_int, ctypes.c_wchar_p]
        k32.CreateMutexW.restype  = ctypes.c_void_p
        name   = "Global\\FescoBillBot-" + hashlib.md5(str(path.resolve()).lower().encode()).hexdigest()
        handle = k32.CreateMutexW(None, False, name)
        if not handle: return True
        if ctypes.get_last_error() == 183:
            k32.CloseHandle.argtypes = [ctypes.c_void_p]; k32.CloseHandle(handle); return False
        _instance_guard = handle; return True
    import fcntl
    handle = open(path, "a+")
    try: fcntl.flock(handle.fileno(), fcntl.LOCK_EX|fcntl.LOCK_NB)
    except OSError: handle.close(); return False
    handle.seek(0); handle.truncate(); handle.write(str(os.getpid())); handle.flush()
    _instance_guard = handle; return True


def announce_startup() -> None:
    if CLOUD_MODE and _AZURE_AVAILABLE and WEBHOOK_URL:
        # Register Telegram webhook (idempotent — safe to call on every cold start)
        try:
            bot.remove_webhook()
            time.sleep(1)
            bot.set_webhook(url=f"{WEBHOOK_URL}/{BOT_TOKEN}")
            logger.info("Telegram webhook registered at %s", WEBHOOK_URL)
        except Exception as e:
            logger.error("Failed to register webhook: %s", e)

    if not PRIMARY_ADMIN_CHAT_ID: return
    mode = "☁️ Azure Container Apps" if CLOUD_MODE else "🖥️ Local mode"
    text = f"✅ FESCO bot online — {mode} ({datetime.now():%d %b %Y, %I:%M %p})."
    if CLOUD_MODE and _AZURE_AVAILABLE:
        workers = worker_table.count_online_workers()
        text += f"\n• Workers online: {workers}"
        text += f"\n• Jobs in queue: {job_queue.get_pending_count()}"
    if BILL_CHECK_HOUR is not None:
        text += f"\n🕐 Auto-run at {BILL_CHECK_HOUR:02d}:00 daily."
    p = startup_problems()
    if p: text += "\n⚠️ Problems:\n- " + "\n- ".join(p)
    for _ in range(3):
        if safe_send(PRIMARY_ADMIN_CHAT_ID, text): return
        time.sleep(5)


def scheduled_runner() -> None:
    if BILL_CHECK_HOUR is None: return
    logger.info("Scheduled auto-run enabled at %02d:00 daily", BILL_CHECK_HOUR)
    while True:
        now    = datetime.now()
        target = now.replace(hour=BILL_CHECK_HOUR, minute=0, second=0, microsecond=0)
        if target <= now: target += timedelta(days=1)
        time.sleep((target - now).total_seconds())
        if not PRIMARY_ADMIN_CHAT_ID: continue
        cmd = [sys.executable, PLAYWRIGHT_SCRIPT_PATH]
        job = Job(chat_id=PRIMARY_ADMIN_CHAT_ID, user_id=0, user_name="scheduler",
                  cmd=cmd, title="scheduled bill run",
                  start_msg="⏳ Starting scheduled daily bill run…", is_scheduled=True)
        activity("scheduled_run")
        enqueue(job)


SILENCE_ALERT_HOURS = 24


def housekeeping() -> None:
    """Periodic: prune usage, expire users, disk alert, silence alert."""
    silence_alerted = False
    while True:
        time.sleep(3600)
        try:
            # Prune stale rate-limit records
            db.prune_old_usage()

            # Remove expired users
            expired = db.remove_expired_users()
            for uid in expired:
                logger.info("Removed expired user %s", uid)
                safe_send(uid, "ℹ️ Your temporary access has expired. Contact the admin to renew.")
                if PRIMARY_ADMIN_CHAT_ID:
                    safe_send(PRIMARY_ADMIN_CHAT_ID, f"ℹ️ Temporary access for {uid} has expired and been removed.")

            # Disk-space alert
            try:
                free_mb = shutil.disk_usage(BILLS_DIR if BILLS_DIR.exists() else ".").free / 1_048_576
                if free_mb < DISK_ALERT_MB:
                    safe_send(PRIMARY_ADMIN_CHAT_ID,
                              f"⚠️ Low disk space: only {free_mb:,.0f} MB free "
                              f"(threshold: {DISK_ALERT_MB} MB).\n"
                              f"Bills folder: {BILLS_DIR}")
            except Exception: pass

            # Silence alert — no commands for 24 h AND no scheduled run
            last_act = db.get_last_activity()
            if last_act:
                hours_silent = (datetime.now(timezone.utc) - last_act).total_seconds() / 3600
                if hours_silent > SILENCE_ALERT_HOURS and not silence_alerted:
                    safe_send(PRIMARY_ADMIN_CHAT_ID,
                              f"⚠️ No bot activity for over {int(hours_silent)} hours.\n"
                              "If this is unexpected, the bot may have stopped working.")
                    silence_alerted = True
                elif hours_silent < 1:
                    silence_alerted = False  # reset after activity resumes

        except Exception as e:
            logger.warning("Housekeeping error: %s", e)


_WORKER_RETRY_INTERVAL = 600


def _run_webhook_server() -> None:
    """Cloud mode: receive Telegram updates via HTTP webhook (no polling)."""
    try:
        from flask import Flask, request as freq, jsonify
    except ImportError:
        logger.critical("Flask not installed. Run: pip install flask")
        sys.exit(1)

    app = Flask("fescobill-webhook")
    # Silence Flask/Werkzeug request logs — our own logger covers what we need
    import logging as _lg
    _lg.getLogger("werkzeug").setLevel(_lg.WARNING)

    @app.route("/health")
    def health():
        return jsonify({"ok": True, "mode": "cloud"})

    @app.route(f"/{BOT_TOKEN}", methods=["POST"])
    def webhook():
        try:
            update = telebot.types.Update.de_json(freq.json)
            bot.process_new_updates([update])
        except Exception as e:
            logger.error("Webhook update error: %s", e)
        return "ok", 200

    port = int(os.getenv("PORT", "8080"))
    logger.info("Webhook server listening on 0.0.0.0:%d", port)
    app.run(host="0.0.0.0", port=port, threaded=True, use_reloader=False)


def run_forever() -> None:
    if CLOUD_MODE:
        _run_webhook_server()   # blocks — Flask runs until the container stops
        return

    backoff, next_worker_check = 5, 0.0
    while True:
        if _using_fallback and WORKER_URL and time.time() > next_worker_check:
            _try_restore_worker(); next_worker_check = time.time() + _WORKER_RETRY_INTERVAL
        try:
            bot.polling(non_stop=True, timeout=20, long_polling_timeout=20)
            backoff = 5
        except KeyboardInterrupt:
            raise
        except Exception as e:
            err = str(e).lower()
            is_network = any(kw in err for kw in ("connection","resolve","getaddrinfo","timeout","ssl","protocol"))
            if WORKER_URL and not _using_fallback and is_network:
                _switch_to_fallback(); next_worker_check = time.time() + _WORKER_RETRY_INTERVAL; backoff=5
            else:
                logger.exception("Polling crashed — restarting in %ss", backoff)
        time.sleep(backoff); backoff = min(backoff*2, 300)


EXIT_ALREADY_RUNNING = 4


def main() -> None:
    parser = argparse.ArgumentParser(description="FESCO Bill Telegram bot")
    parser.add_argument("--check", action="store_true", help="Verify config and exit")
    args = parser.parse_args()

    if args.check:
        load_dotenv(PROJECT_ROOT / ".env")
        issues = []
        if not os.getenv("TELEGRAM_BOT_TOKEN"):
            issues.append("TELEGRAM_BOT_TOKEN missing")
        if os.getenv("CLOUD_MODE","").strip() in ("1","true","yes"):
            if not os.getenv("WORKER_API_KEY"):
                issues.append("WORKER_API_KEY missing (required in cloud mode)")
        else:
            script = os.getenv("PLAYWRIGHT_SCRIPT_PATH") or str(BASE_DIR / "fesco_bill_automation.py")
            if not Path(script).is_file():
                issues.append(f"Script not found: {script}")
        issues += validate_config()
        issues += check_env_completeness(CONFIG_PATH)
        if issues:
            print("PROBLEMS:\n- " + "\n- ".join(issues)); sys.exit(1)
        print("OK"); sys.exit(0)

    if not BOT_TOKEN:
        print("TELEGRAM_BOT_TOKEN must be set in .env"); sys.exit(1)
    if not WORKER_URL:
        logger.warning("CLOUDFLARE_WORKER_URL not set — using direct api.telegram.org")
        telebot.apihelper.API_URL = DIRECT_API_URL
    if not acquire_single_instance():
        print("Another copy of the bot is already running — exiting.")
        sys.exit(EXIT_ALREADY_RUNNING)

    setup_logging()
    logger.info("Starting. Python: %s | CWD: %s | Script: %s", sys.executable, os.getcwd(), PLAYWRIGHT_SCRIPT_PATH)

    db.init(BOT_DB_FILE)
    db.migrate_from_json(_LEGACY_ACCESS, _LEGACY_SAVED_REFS, _LEGACY_USAGE)
    reload_config()

    if CLOUD_MODE:
        if not _AZURE_AVAILABLE:
            print("ERROR: azure-storage-queue and azure-data-tables must be installed for cloud mode.")
            print("  pip install azure-storage-queue azure-data-tables")
            sys.exit(1)
        if not AZURE_CONN_STR:
            print("ERROR: AZURE_STORAGE_CONNECTION_STRING must be set in .env for cloud mode.")
            sys.exit(1)
        job_queue.init(AZURE_CONN_STR)
        worker_table.init(AZURE_CONN_STR)
        logger.info("CLOUD MODE — Azure Container Apps. Webhook on port %s.", os.getenv("PORT","8080"))
    else:
        threading.Thread(target=worker, daemon=True, name="job-worker").start()

    threading.Thread(target=announce_startup, daemon=True, name="startup-notice").start()
    threading.Thread(target=scheduled_runner, daemon=True, name="scheduler").start()
    threading.Thread(target=housekeeping,     daemon=True, name="housekeeping").start()

    logger.info("Bot is running. Waiting for commands…")
    run_forever()


if __name__ == "__main__":
    main()


# --------------------------------------------------------------------------- #
# Cloud mode — job API (Flask, runs in a background thread)
# --------------------------------------------------------------------------- #

def _start_job_api() -> None:
    """Embedded Flask REST API for worker PCs.  Runs on WORKER_API_PORT."""
    try:
        import functools
        from flask import Flask, request, jsonify
    except ImportError:
        logger.error("Flask not installed — run: pip install flask. Job API disabled.")
        return

    api = Flask("fescobill-job-api")
    api.logger.disabled = True
    import logging as _lg
    _lg.getLogger("werkzeug").setLevel(_lg.WARNING)

    def _require_key(f):
        @functools.wraps(f)
        def wrapper(*args, **kwargs):
            if not WORKER_API_KEY:
                return jsonify({"error": "WORKER_API_KEY not configured on VM"}), 500
            if request.headers.get("X-API-Key") != WORKER_API_KEY:
                return jsonify({"error": "unauthorized"}), 401
            return f(*args, **kwargs)
        return wrapper

    @api.route("/api/v1/health")
    def health():
        return jsonify({
            "ok": True,
            "workers_online": db.count_online_workers(),
            "jobs_pending": db.get_pending_count(),
        })

    @api.route("/api/v1/jobs/claim", methods=["POST"])
    @_require_key
    def claim_job():
        data        = request.get_json(silent=True) or {}
        worker_name = data.get("worker_name", "unknown")
        is_new      = not db.worker_was_recently_online(worker_name)
        db.update_worker_heartbeat(worker_name, request.remote_addr, data.get("version", ""))
        if is_new:
            threading.Thread(
                target=lambda: safe_send(
                    PRIMARY_ADMIN_CHAT_ID,
                    f"🟢 Worker '{worker_name}' is now online."
                ),
                daemon=True,
            ).start()
        job = db.claim_next_job(worker_name)
        return jsonify({"job": job})

    @api.route("/api/v1/jobs/<int:job_id>/complete", methods=["POST"])
    @_require_key
    def complete_job(job_id):
        data = request.get_json(silent=True) or {}
        db.complete_job(job_id, data.get("result_status", "Completed"),
                        data.get("result_output", ""))
        return jsonify({"ok": True})

    @api.route("/api/v1/jobs/<int:job_id>/fail", methods=["POST"])
    @_require_key
    def fail_job(job_id):
        data = request.get_json(silent=True) or {}
        db.fail_job(job_id, data.get("error", "Unknown error"))
        return jsonify({"ok": True})

    @api.route("/api/v1/workers/heartbeat", methods=["POST"])
    @_require_key
    def worker_heartbeat():
        data        = request.get_json(silent=True) or {}
        worker_name = data.get("name", "unknown")
        is_new      = not db.worker_was_recently_online(worker_name)
        db.update_worker_heartbeat(worker_name, request.remote_addr, data.get("version", ""))
        if is_new:
            threading.Thread(
                target=lambda: safe_send(
                    PRIMARY_ADMIN_CHAT_ID,
                    f"🟢 Worker '{worker_name}' came online."
                ),
                daemon=True,
            ).start()
        return jsonify({"ok": True, "pending_jobs": db.get_pending_count()})

    @api.route("/api/v1/workers")
    @_require_key
    def list_workers():
        return jsonify({"workers": db.get_online_workers()})

    logger.info("Job API listening on 0.0.0.0:%d", WORKER_API_PORT)
    api.run(host="0.0.0.0", port=WORKER_API_PORT, threaded=True, use_reloader=False)


def _result_dispatcher() -> None:
    """Polls for completed jobs and sends results back to users."""
    while True:
        time.sleep(5)
        try:
            for job in db.get_completed_unnotified():
                status = job.get("result_status") or "Done"
                output = job.get("result_output") or ""
                icon   = "✅" if job["status"] == "completed" else "❌"
                safe_send(job["chat_id"], f"{icon} {status}\n\nOutput:\n{output}"[:4000])
                db.mark_notified(job["id"])
                logger.info("Dispatched result for job #%d to chat %d", job["id"], job["chat_id"])
        except Exception as e:
            logger.warning("Result dispatcher error: %s", e)


def _worker_monitor() -> None:
    """Detects workers going offline and alerts the admin."""
    known: set[str] = {w["name"] for w in db.get_online_workers()}
    while True:
        time.sleep(60)
        try:
            online = {w["name"] for w in db.get_online_workers()}
            for name in known - online:
                safe_send(PRIMARY_ADMIN_CHAT_ID, f"🔴 Worker '{name}' went offline.")
            known = online
        except Exception as e:
            logger.warning("Worker monitor error: %s", e)


def _stale_job_recovery() -> None:
    """Puts claimed jobs whose worker died back to pending (every 5 min)."""
    while True:
        time.sleep(300)
        try:
            n = db.recover_stale_claims(timeout_minutes=10)
            if n:
                logger.warning("Recovered %d stale job(s) back to pending.", n)
                safe_send(PRIMARY_ADMIN_CHAT_ID,
                          f"⚠️ {n} job(s) were stuck (worker went offline mid-job) "
                          f"and have been re-queued.")
        except Exception as e:
            logger.warning("Stale-job recovery error: %s", e)
