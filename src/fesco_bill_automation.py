#!/usr/bin/env python3
"""
FESCO Electricity Bill Automation
==================================
Fetches electricity bills from https://bill.pitc.com.pk/fescobill using
Playwright, then emails a nicely formatted copy of each bill (and optionally
a Telegram message and/or a WhatsApp message) to the recipients configured
in config.json. WhatsApp messages are sent through Green API.

Setup
-----
    pip install -r requirements.txt
    playwright install chromium
    cp .env.example .env                   # fill in your secrets
    cp config.example.json config.json     # add your reference numbers

Run
---
    python fesco_bill_automation.py
    python fesco_bill_automation.py --config myconfig.json --headed
    python fesco_bill_automation.py --no-telegram --no-whatsapp

    # Look up specific references only and send the bill ONLY to one Telegram chat
    # (this is what the bot's /getbillbyref does; config.json recipients are not used):
    python fesco_bill_automation.py --refs 12345678901234,12345678901235 --reply-chat-id 123456789

Reliability
-----------
    * A lock file (--lock-file) stops two runs overlapping - scheduler, bot and manual runs share it.
    * fesco_bill_automation.log rotates at ~1 MB (5 old copies are kept).
    * If the FESCO page no longer matches what the script expects, ONE alert is sent to
      ADMIN_ALERT_EMAIL / ADMIN_ALERT_TELEGRAM_CHAT_ID ("site layout may have changed").
    * --cleanup-days N (or BILLS_RETENTION_DAYS in .env) deletes saved PDFs/PNGs older than N days.

Per-person delivery choices (config.json)
-----------------------------------------
Every recipient can be a plain string (gets the default content) or an object that
chooses exactly what that person receives with "send": a list of "text", "png", "pdf".

    "emails":            ["a@x.com", {"address": "b@x.com", "send": ["pdf"]}],
    "telegram_chat_ids": ["111", {"chat_id": "222", "send": ["text"]}],
    "whatsapp_numbers":  ["9233...", {"number": "9230...", "send": ["text", "pdf"]}]

    text = the bill summary message   png = bill image   pdf = bill PDF
    "enabled": false on an entry pauses that person without deleting them.
    Optional top-level "delivery_defaults": {"email": [...], "telegram": [...], "whatsapp": [...]}
    changes the default for everyone who has no "send" of their own.
    Built-in defaults (same as before): email = text+png+pdf, telegram = text+png,
    whatsapp = text (+ the --whatsapp-pdf / --whatsapp-png flag if used).

See README.md for full setup instructions (Gmail App Password, Telegram bot,
and Green API).
"""

from __future__ import annotations

import argparse
import html
import atexit
import json
import logging
import random
import logging.handlers
import os
import smtplib
import sys
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from email.mime.application import MIMEApplication
from email.mime.image import MIMEImage
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from pathlib import Path
from typing import Any, Optional

import phonenumbers
import requests
from dotenv import load_dotenv
from phonenumbers import NumberParseException
from playwright.sync_api import BrowserContext, Page, TimeoutError as PWTimeout, sync_playwright

# --------------------------------------------------------------------------- #
# Constants & logging
# --------------------------------------------------------------------------- #

BILL_URL = "https://bill.pitc.com.pk/fescobill"

# Project root: two levels up from this file (which lives in src/)
PROJECT_ROOT = Path(__file__).resolve().parent.parent


def telegram_api_base() -> str:
    """Telegram API address. Set CLOUDFLARE_WORKER_URL (or TELEGRAM_API_BASE) in .env if Telegram is
    blocked where you live and you proxy it through your own Cloudflare Worker; otherwise the
    official API is used. Read at call time so values from .env are picked up."""
    return (os.getenv("TELEGRAM_API_BASE") or os.getenv("CLOUDFLARE_WORKER_URL") or "https://api.telegram.org").rstrip("/")


OUTPUT_DIR = PROJECT_ROOT / "data" / "bills"  # local PDFs / screenshots are saved here
LOG_FILE = PROJECT_ROOT / "logs" / "fesco_bill_automation.log"
LOG_MAX_BYTES = 1_000_000   # rotate the log at ~1 MB ...
LOG_BACKUP_COUNT = 5        # ... and keep 5 old copies (fesco_bill_automation.log.1 ... .5)
LOCK_FILE_DEFAULT = PROJECT_ROOT / "data" / "fesco_bill_automation.lock"
LOCK_STALE_AFTER_SECONDS = 3 * 3600  # a lock older than this is treated as left over from a crash
STATE_FILE_DEFAULT = PROJECT_ROOT / "data" / "state.json"
HISTORY_FILE_DEFAULT = PROJECT_ROOT / "data" / "bill_history.json"  # read by the Telegram bot for /history
MAX_HISTORY_PER_BILL = 24
MAX_ATTEMPT_HISTORY = 10

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.handlers.RotatingFileHandler(
            LOG_FILE, maxBytes=LOG_MAX_BYTES, backupCount=LOG_BACKUP_COUNT, encoding="utf-8"
        ),
        logging.StreamHandler(sys.stdout),
    ],
)
log = logging.getLogger("fesco")

# JS run inside the rendered bill page to pull out every field we need.
# Matches the fixed template classes used by bill.pitc.com.pk (label-row /
# val-space pairs, right panel date cells, late-payment surcharge columns).
SCRAPE_JS = r"""
() => {
  const result = {};

  const firstCell = document.querySelector('.consumer-detail-card--gbn .grid-col-cell');
  if (firstCell) {
    const vals = firstCell.querySelectorAll('.val-space');
    result.reference_no   = vals[0] ? vals[0].textContent.trim() : null;
    result.consumer_id    = vals[1] ? vals[1].textContent.trim() : null;
    result.name_address   = vals[2] ? vals[2].textContent.replace(/\s+/g, ' ').trim() : null;
  }

  document.querySelectorAll('.meter-info-cell').forEach(cell => {
    const lblEl = cell.querySelector('.en-lbl');
    const valEl = cell.querySelector('.val-space');
    if (!lblEl || !valEl) return;
    const label = lblEl.textContent.trim().toUpperCase();
    const val = valEl.textContent.trim();
    if (label === 'PREVIOUS READING') result.previous_reading = val;
    if (label === 'PRESENT READING') result.present_reading = val;
    if (label === 'UNITS') result.units = val;
  });

  const billMonthCell = Array.from(document.querySelectorAll('.right-section-cell'))
    .find(c => {
      const l = c.querySelector('.right-panel-en');
      return l && l.textContent.includes('BILL MONTH');
    });
  if (billMonthCell) {
    const v = billMonthCell.querySelector('.right-main-val');
    result.bill_month = v ? v.textContent.trim() : null;
  }

  document.querySelectorAll('.right-grid-cell').forEach(cell => {
    const lblEl = cell.querySelector('.right-panel-en');
    const valEl = cell.querySelector('.right-panel-date-val');
    if (!lblEl || !valEl) return;
    const label = lblEl.textContent.trim().toUpperCase();
    const val = valEl.textContent.trim();
    if (label === 'READING DATE') result.reading_date = val;
    if (label === 'ISSUE DATE') result.issue_date = val;
  });

  const dueCell = document.querySelector('.right-section-cell--due');
  if (dueCell) {
    const v = dueCell.querySelector('.right-main-val--due');
    result.due_date = v ? v.textContent.trim() : null;
  }

  const payableEl = document.querySelector('.payable-card-amount');
  result.payable_within_due = payableEl ? payableEl.textContent.trim() : null;

  const surchargeCols = document.querySelectorAll('.lp-surcharge-data-col');
  if (surchargeCols.length >= 2) {
    const col0 = surchargeCols[0];
    const col1 = surchargeCols[1];
    result.payable_after_due_till = {
      period: (col0.querySelector('.lp-surcharge-period') || {}).textContent?.trim() || null,
      amount: (col0.querySelector('.lp-surcharge-bottom-val') || {}).textContent?.trim() || null,
    };
    result.payable_after_due_after = {
      period: (col1.querySelector('.lp-surcharge-period') || {}).textContent?.trim() || null,
      amount: (col1.querySelector('.lp-surcharge-bottom-val') || {}).textContent?.trim() || null,
    };
  }

  return result;
}
"""


# --------------------------------------------------------------------------- #
# Config models
# --------------------------------------------------------------------------- #

@dataclass
class Recipients:
    emails: list[str] = field(default_factory=list)
    telegram_chat_ids: list[str] = field(default_factory=list)
    whatsapp_numbers: list[str] = field(default_factory=list)  # E.164 w/o "+", e.g. "923001234567"
    # Per-person delivery choices: {(channel, target): ("text", "png", "pdf")}
    send_overrides: dict[tuple[str, str], tuple[str, ...]] = field(default_factory=dict)
    # Config-wide defaults per channel (from "delivery_defaults" in config.json)
    channel_defaults: dict[str, tuple[str, ...]] = field(default_factory=dict)

    def send_for(self, channel: str, target: str) -> Optional[tuple[str, ...]]:
        """What this person asked to receive, or None to use the built-in default."""
        return self.send_overrides.get((channel, target)) or self.channel_defaults.get(channel)


@dataclass
class BillRequest:
    reference_no: str
    label: str = ""
    search_by: str = "refno"   # "refno" (Reference No) or "appno" (Customer ID)
    ru_code: str = ""          # "" (U) or "R" -- matches the site's U/R dropdown
    recipients: Recipients = field(default_factory=Recipients)


# WhatsApp recipients can be entered in whatever format is natural for that
# country - "+1 323-456-7890", "+92 300 1234567", "0300-1234567",
# "923001234567" - rather than forcing everyone into one clean E.164 shape
# by hand. normalize_whatsapp_number() uses Google's libphonenumber (via the
# `phonenumbers` package) to parse and validate each one properly:
#   - a leading "+" (or "00") is enough on its own - the country is read
#     straight from the number, no region needed.
#   - a *local* number with no country code (e.g. a Pakistani "0300...")
#     needs a `region` hint (an ISO code like "PK" or "US") to know which
#     country it belongs to - see "default_whatsapp_region" / "whatsapp_region"
#     in config.json.
def normalize_whatsapp_number(raw: str, region: Optional[str]) -> str:
    raw = raw.strip()
    try:
        parsed = phonenumbers.parse(raw, region)
    except NumberParseException as exc:
        hint = f" (and no default region is set to interpret a local number)" if not region else ""
        raise ValueError(f"could not parse WhatsApp number '{raw}'{hint}: {exc}") from exc

    if not phonenumbers.is_valid_number(parsed):
        raise ValueError(
            f"'{raw}' doesn't look like a valid number"
            + (f" for region '{region}'" if region else "")
        )

    e164 = phonenumbers.format_number(parsed, phonenumbers.PhoneNumberFormat.E164)
    return e164.lstrip("+")  # WhatsApp's click-to-chat URL wants digits only, no "+"


VALID_SEND = {"text", "png", "pdf", "summary"}  # summary = amount+due date only, no image
_SEND_ALIASES = {
    "image": "png", "photo": "png", "screenshot": "png",
    "document": "pdf", "message": "text",
    "brief": "summary", "short": "summary",
}

# Built-in defaults - identical to how the script behaved before per-person choices existed.
DEFAULT_SEND_EMAIL = ("text", "png", "pdf")
DEFAULT_SEND_TELEGRAM = ("text", "png")


def parse_send(value: Any, where: str) -> Optional[tuple[str, ...]]:
    """Validate a "send" setting. Returns None (= use the default) when missing or unusable."""
    if value is None:
        return None
    items = [value] if isinstance(value, str) else list(value) if isinstance(value, (list, tuple)) else []
    cleaned: list[str] = []
    for item in items:
        key = _SEND_ALIASES.get(str(item).strip().lower(), str(item).strip().lower())
        if key in VALID_SEND:
            if key not in cleaned:
                cleaned.append(key)
        else:
            log.warning("%s: unknown delivery option %r (use text, png, pdf) - ignored", where, item)
    if not cleaned:
        log.warning("%s: no valid \"send\" options - using the default instead", where)
        return None
    return tuple(cleaned)


def parse_recipient(entry: Any, keys: tuple[str, ...], where: str) -> tuple[Optional[str], Optional[tuple[str, ...]]]:
    """A recipient is either a plain string or {"<key>": "...", "send": [...], "enabled": true/false}.
    Returns (target, send_choice). target is None when the entry is disabled or unusable."""
    if isinstance(entry, dict):
        if entry.get("enabled", True) is False:
            log.info("%s: %s is disabled (\"enabled\": false) - skipping", where, entry)
            return None, None
        target = next((str(entry[k]).strip() for k in keys if entry.get(k) not in (None, "")), None)
        if not target:
            log.warning("%s: recipient %s has no %s - skipped", where, entry, " / ".join(keys))
            return None, None
        return target, parse_send(entry.get("send"), f"{where} {target}")
    if isinstance(entry, (str, int)) and str(entry).strip():
        return str(entry).strip(), None
    log.warning("%s: unusable recipient %r - skipped", where, entry)
    return None, None


def load_bills(config_path: Path) -> list[BillRequest]:
    raw = json.loads(config_path.read_text(encoding="utf-8"))
    global_region = raw.get("default_whatsapp_region")  # e.g. "PK" - used only for local numbers

    channel_defaults: dict[str, tuple[str, ...]] = {}
    for channel, value in (raw.get("delivery_defaults") or {}).items():
        parsed = parse_send(value, f"delivery_defaults.{channel}")
        if channel in ("email", "telegram", "whatsapp") and parsed:
            channel_defaults[channel] = parsed

    bills: list[BillRequest] = []
    for item in raw.get("bills", []):
        rec = item.get("recipients", {})
        region = item.get("whatsapp_region", global_region)
        ref_label = str(item.get("reference_no"))
        overrides: dict[tuple[str, str], tuple[str, ...]] = {}

        emails: list[str] = []
        for entry in rec.get("emails", []):
            target, send = parse_recipient(entry, ("address", "email", "to"), f"{ref_label} email")
            if target:
                emails.append(target)
                if send:
                    overrides[("email", target)] = send

        telegram_ids: list[str] = []
        for entry in rec.get("telegram_chat_ids", []):
            target, send = parse_recipient(entry, ("chat_id", "id", "to"), f"{ref_label} telegram")
            if target:
                telegram_ids.append(target)
                if send:
                    overrides[("telegram", target)] = send

        wa_numbers: list[str] = []
        for entry in rec.get("whatsapp_numbers", []):
            target, send = parse_recipient(entry, ("number", "phone", "to"), f"{ref_label} whatsapp")
            if not target:
                continue
            try:
                number = normalize_whatsapp_number(target, region)
            except ValueError as exc:
                # Don't let one typo'd number abort loading the whole config -
                # log it and keep going; every other recipient still gets sent to.
                log.warning("Skipping WhatsApp number for %s: %s", item.get("reference_no"), exc)
                continue
            wa_numbers.append(number)
            if send:
                overrides[("whatsapp", number)] = send

        bills.append(
            BillRequest(
                reference_no=str(item["reference_no"]).strip(),
                label=item.get("label", ""),
                search_by=item.get("search_by", "refno"),
                ru_code=item.get("ru_code", ""),
                recipients=Recipients(
                    emails=emails,
                    telegram_chat_ids=telegram_ids,
                    whatsapp_numbers=wa_numbers,
                    send_overrides=overrides,
                    channel_defaults=dict(channel_defaults),
                ),
            )
        )
    if not bills:
        raise ValueError(f"No 'bills' entries found in {config_path}")
    return bills


# --------------------------------------------------------------------------- #
# Progress / resume state (state.json)
# --------------------------------------------------------------------------- #
#
# Keeps a small record per reference number so re-running the script:
#   - never re-sends a bill for a period it already delivered successfully
#     (dedupe key = "<reference_no>|<bill_month>");
#   - can resume "from where it left off" by skipping already-succeeded
#     bills (--retry-failed-only);
#   - keeps a capped history of recent attempts (success/failure + reason)
#     for troubleshooting, instead of only the last run's log lines.

def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def load_state(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {"version": 1, "bills": {}}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        backup = path.with_suffix(path.suffix + ".bak")
        log.warning("State file %s is corrupt - moving it to %s and starting fresh", path, backup)
        path.rename(backup)
        return {"version": 1, "bills": {}}


def save_state(path: Path, state: dict[str, Any]) -> None:
    # Atomic write (temp file + replace) so a crash mid-save never corrupts
    # the state file - we'd rather lose the very latest update than the
    # whole history.
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(state, indent=2, ensure_ascii=False), encoding="utf-8")
    tmp.replace(path)


def get_bill_state(state: dict[str, Any], reference_no: str) -> dict[str, Any]:
    return state["bills"].setdefault(
        reference_no,
        {
            "label": "",
            "sent_keys": [],        # "<reference_no>|<bill_month>" already delivered
            "attempts": [],         # capped recent attempt history
            "last_status": None,    # "success" | "failed" | "success_skipped_duplicate"
            "last_bill_month": None,
            "last_attempt_at": None,
            "last_success_at": None,
            "consecutive_failures": 0,
        },
    )


def record_attempt(entry: dict[str, Any], outcome: str, error: Optional[str] = None) -> None:
    entry["attempts"].append({"at": now_iso(), "outcome": outcome, "error": error})
    entry["attempts"] = entry["attempts"][-MAX_ATTEMPT_HISTORY:]
    entry["last_attempt_at"] = now_iso()
    entry["last_status"] = outcome
    if outcome in ("success", "success_skipped_duplicate"):
        entry["last_success_at"] = now_iso()
        entry["consecutive_failures"] = 0
    else:
        entry["consecutive_failures"] = entry.get("consecutive_failures", 0) + 1


def record_history(path: Path, bill: "BillRequest", data: dict[str, Any]) -> None:
    """Append a short summary of a successfully fetched bill to bill_history.json.
    Never raises - history is a convenience and must not break a run."""
    try:
        history: dict[str, Any] = {}
        if path.exists():
            try:
                history = json.loads(path.read_text(encoding="utf-8"))
            except json.JSONDecodeError:
                history = {}
        entries = [
            e for e in history.get(bill.reference_no, [])
            if e.get("bill_month") != data.get("bill_month")  # one entry per month: keep the latest
        ]
        entries.append({
            "fetched_at": now_iso(),
            "bill_month": data.get("bill_month"),
            "issue_date": data.get("issue_date"),
            "due_date": data.get("due_date"),
            "units": data.get("units"),
            "present_reading": data.get("present_reading"),
            "payable_within_due": data.get("payable_within_due"),
        })
        history[bill.reference_no] = entries[-MAX_HISTORY_PER_BILL:]
        tmp = path.with_suffix(path.suffix + ".tmp")
        tmp.write_text(json.dumps(history, indent=2, ensure_ascii=False), encoding="utf-8")
        tmp.replace(path)
    except Exception as exc:  # noqa: BLE001
        log.warning("Could not update bill history file: %s", exc)


def dedupe_key(reference_no: str, bill_month: Optional[str]) -> str:
    # Falls back to the current year-month if the site's "bill month" field
    # couldn't be read, so we still dedupe sensibly rather than not at all.
    period = bill_month or datetime.now().strftime("%Y-%m")
    return f"{reference_no}|{period}"


# --------------------------------------------------------------------------- #
# Reliability helpers: run lock, old-file cleanup, "site layout changed" error
# --------------------------------------------------------------------------- #

class SiteLayoutError(RuntimeError):
    """The FESCO page loaded but its form/fields are not what this script expects -
    the site has probably been redesigned and the selectors need updating."""


def pid_alive(pid: int) -> bool:
    """True if a process with this PID is currently running (works on Windows and POSIX)."""
    if pid <= 0:
        return False
    if os.name == "nt":
        import ctypes

        kernel32 = ctypes.windll.kernel32  # type: ignore[attr-defined]
        handle = kernel32.OpenProcess(0x1000, False, pid)  # PROCESS_QUERY_LIMITED_INFORMATION
        if not handle:
            return False
        try:
            code = ctypes.c_ulong()
            if not kernel32.GetExitCodeProcess(handle, ctypes.byref(code)):
                return False
            return code.value == 259  # STILL_ACTIVE
        finally:
            kernel32.CloseHandle(handle)
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


class RunLock:
    """A lock file shared by every way of starting this script (scheduler, bot, by hand),
    so two runs can never fight over the browser, state.json or the bills/ folder.
    A lock left behind by a crashed run (dead PID, or very old) is taken over automatically."""

    _held_paths: set[str] = set()  # locks currently held by THIS process

    def __init__(self, path: Path, wait_seconds: float = 0, stale_after: float = LOCK_STALE_AFTER_SECONDS) -> None:
        self.path = Path(path)
        self.wait_seconds = wait_seconds
        self.stale_after = stale_after
        self.held = False

    def _read_owner(self) -> tuple[int, float]:
        """(pid, age_seconds); pid 0 if the file is unreadable."""
        try:
            age = time.time() - self.path.stat().st_mtime
            pid = int(json.loads(self.path.read_text(encoding="utf-8")).get("pid", 0))
            return pid, age
        except (OSError, ValueError, AttributeError):
            try:
                return 0, time.time() - self.path.stat().st_mtime
            except OSError:
                return 0, 0.0

    def _is_stale(self) -> bool:
        pid, age = self._read_owner()
        if pid and pid == os.getpid():
            # Our own PID in a lock we do not hold = a crashed run whose PID was reused by us.
            return str(self.path) not in RunLock._held_paths
        if pid == 0:
            return age > 60  # unreadable/corrupt - give a just-created lock a moment to be filled in
        return (not pid_alive(pid)) or age > self.stale_after

    def acquire(self) -> bool:
        deadline = time.time() + self.wait_seconds
        last_notice = 0.0
        while True:
            try:
                fd = os.open(str(self.path), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            except FileExistsError:
                if self._is_stale():
                    pid, _age = self._read_owner()
                    log.warning("Removing stale lock file %s (owner pid %s is gone or lock is very old)", self.path, pid or "?")
                    try:
                        self.path.unlink()
                    except FileNotFoundError:
                        pass
                    continue
                if time.time() >= deadline:
                    return False
                if time.time() - last_notice > 30:
                    owner, _ = self._read_owner()
                    log.info("Another run is in progress (pid %s) - waiting for it to finish...", owner or "?")
                    last_notice = time.time()
                time.sleep(2)
                continue
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump({"pid": os.getpid(), "started": now_iso()}, f)
            self.held = True
            RunLock._held_paths.add(str(self.path))
            return True

    def release(self) -> None:
        if self.held:
            try:
                self.path.unlink()
            except FileNotFoundError:
                pass
            self.held = False
            RunLock._held_paths.discard(str(self.path))


def cleanup_old_files(folder: Path, days: int) -> int:
    """Delete saved bill PDFs/PNGs older than `days` days. days <= 0 disables cleanup."""
    if days <= 0 or not folder.exists():
        return 0
    cutoff = time.time() - days * 86400
    removed = 0
    for f in folder.iterdir():
        try:
            if f.is_file() and f.suffix.lower() in (".pdf", ".png") and f.stat().st_mtime < cutoff:
                f.unlink()
                removed += 1
        except OSError as exc:
            log.warning("Could not delete old file %s: %s", f, exc)
    if removed:
        log.info("Cleanup: removed %d bill file(s) older than %d days from %s", removed, days, folder)
    return removed


def _send_admin_alert(
    email_cfg: dict[str, Any],
    admin_email: Optional[str],
    telegram_token: Optional[str],
    admin_chat_id: Optional[str],
    subject: str,
    body: str,
) -> None:
    if admin_email and email_cfg.get("sender_email") and email_cfg.get("sender_password"):
        try:
            msg = MIMEMultipart()
            msg["Subject"] = subject
            msg["From"] = email_cfg["sender_email"]
            msg["To"] = admin_email
            msg.attach(MIMEText(body, "plain", "utf-8"))
            with smtplib.SMTP(email_cfg["smtp_host"], email_cfg["smtp_port"], timeout=30) as server:
                server.starttls()
                server.login(email_cfg["sender_email"], email_cfg["sender_password"])
                server.sendmail(email_cfg["sender_email"], [admin_email], msg.as_string())
            log.info("Sent alert email to admin %s", admin_email)
        except Exception as exc:  # noqa: BLE001
            log.error("Failed to send admin alert email: %s", exc)

    if admin_chat_id and telegram_token:
        try:
            requests.post(
                f"{telegram_api_base()}/bot{telegram_token}/sendMessage",
                data={"chat_id": admin_chat_id, "text": f"\u26a0\ufe0f {subject}\n\n{body}"},
                timeout=20,
            ).raise_for_status()
            log.info("Sent alert to admin Telegram chat %s", admin_chat_id)
        except Exception as exc:  # noqa: BLE001
            log.error("Failed to send admin Telegram alert: %s", exc)


def alert_site_layout_changed(
    email_cfg: dict[str, Any],
    admin_email: Optional[str],
    telegram_token: Optional[str],
    admin_chat_id: Optional[str],
    problems: list[tuple["BillRequest", str]],
) -> None:
    """One alert per run (not one per bill) when the FESCO page no longer looks like the
    layout this script was written for."""
    refs = ", ".join(b.reference_no for b, _ in problems)
    body = (
        "The FESCO bill page loaded, but the script could not find/use the expected form or bill fields.\n"
        "The website has most likely changed its layout, so the selectors in fesco_bill_automation.py "
        "(SCRAPE_JS / wait_for_bill_fields / the #searchTextBox, #btnSearch ... ids) need updating.\n\n"
        f"Affected references: {refs}\nTime: {now_iso()}\nFirst error: {problems[0][1]}"
    )
    _send_admin_alert(email_cfg, admin_email, telegram_token, admin_chat_id,
                      "[FESCO Automation] Site layout may have changed", body)


def alert_admin(
    email_cfg: dict[str, Any],
    admin_email: Optional[str],
    telegram_token: Optional[str],
    admin_chat_id: Optional[str],
    bill: "BillRequest",
    error: str,
) -> None:
    """Optional: notify a human when a bill gives up after all retries, so
    a reference number that's genuinely broken doesn't just go silent."""
    subject = f"[FESCO Automation] Failed to fetch bill {bill.reference_no}"
    body = f"Reference No: {bill.reference_no}\nLabel: {bill.label or '-'}\nTime: {now_iso()}\nError: {error}"
    _send_admin_alert(email_cfg, admin_email, telegram_token, admin_chat_id, subject, body)


# --------------------------------------------------------------------------- #
# Loading-overlay handling
# --------------------------------------------------------------------------- #
#
# bill.pitc.com.pk renders the bill data server-side (it's already sitting in
# the DOM when the page loads) but then plays a cosmetic "Loading your bill"
# overlay on top of it for a RANDOM 3-5 seconds (see the site's own
# `showLoadingBar()`, which just animates a width/percentage and hides the
# overlay on a timer - it never re-checks the data). That means:
#   - scraping the fields never needs to wait on the overlay at all;
#   - but a screenshot/PDF taken while it's up would capture the spinner.
#
# So instead of trusting a fixed sleep (which could be too short on a slow
# run, or waste time on a fast one), we poll for the overlay to clear itself
# for up to `loader_timeout_ms`, and if it's still stuck after that we just
# remove it with JS before capturing. This can never fail - it's strictly
# more reliable than waiting on someone else's animation timer.

def wait_for_loader_to_clear(page: Page, timeout_ms: int) -> bool:
    """Return True if the cosmetic overlay disappeared on its own in time."""
    try:
        page.wait_for_function(
            """() => {
                const el = document.getElementById('loader-container');
                if (!el) return true;
                const style = window.getComputedStyle(el);
                return style.display === 'none' || el.classList.contains('bill-loader--hide');
            }""",
            timeout=timeout_ms,
        )
        return True
    except PWTimeout:
        return False


def force_hide_loader(page: Page) -> None:
    """Belt-and-suspenders: remove the overlay outright. Safe no-op if it's
    already gone or never existed."""
    page.evaluate(
        """() => {
            const el = document.getElementById('loader-container');
            if (el) el.remove();
        }"""
    )


def wait_for_bill_fields(page: Page, timeout_ms: int) -> None:
    """Wait until the bill template has finished populating every scraped field."""
    page.wait_for_function(
        """() => {
            const text = (selector, root = document) => {
                const el = root.querySelector(selector);
                return el ? el.textContent.trim() : '';
            };
            const firstCell = document.querySelector('.consumer-detail-card--gbn .grid-col-cell');
            if (!firstCell) return false;

            const consumerValues = Array.from(firstCell.querySelectorAll('.val-space'))
                .map(el => el.textContent.trim());
            const meter = {};
            document.querySelectorAll('.meter-info-cell').forEach(cell => {
                const label = text('.en-lbl', cell).toUpperCase();
                const value = text('.val-space', cell);
                if (label) meter[label] = value;
            });

            const billMonthCell = Array.from(document.querySelectorAll('.right-section-cell'))
                .find(cell => text('.right-panel-en', cell).includes('BILL MONTH'));
            const billMonth = billMonthCell ? text('.right-main-val', billMonthCell) : '';
            const dateValues = {};
            document.querySelectorAll('.right-grid-cell').forEach(cell => {
                const label = text('.right-panel-en', cell).toUpperCase();
                const value = text('.right-panel-date-val', cell);
                if (label) dateValues[label] = value;
            });

            const dueCell = document.querySelector('.right-section-cell--due');
            const surchargeCols = document.querySelectorAll('.lp-surcharge-data-col');
            const surchargeReady = surchargeCols.length >= 2 &&
                Array.from(surchargeCols).slice(0, 2).every(col =>
                    text('.lp-surcharge-period', col) &&
                    text('.lp-surcharge-bottom-val', col));

            return Boolean(
                consumerValues[0] &&
                consumerValues[1] &&
                consumerValues[2] &&
                billMonth &&
                dateValues['READING DATE'] &&
                dateValues['ISSUE DATE'] &&
                meter['PREVIOUS READING'] &&
                meter['PRESENT READING'] &&
                meter['UNITS'] &&
                dueCell &&
                text('.right-main-val--due', dueCell) &&
                text('.payable-card-amount') &&
                surchargeReady
            );
        }""",
        timeout=timeout_ms,
    )


# --------------------------------------------------------------------------- #
# Scraping + retry
# --------------------------------------------------------------------------- #

def fetch_bill_with_retries(
    context: BrowserContext,
    bill: BillRequest,
    out_dir: Path,
    max_attempts: int,
    loader_timeout_ms: int,
) -> tuple[dict[str, Any], Optional[Path], Optional[Path], list[dict[str, Any]]]:
    """Run the full search -> scrape -> capture flow, retrying the whole
    thing (fresh page, fresh form submission - i.e. a real 'refresh and
    re-enter the reference number') if anything genuinely fails. Returns the
    scraped data, artifact paths, and a per-attempt log for the state file."""
    attempt_log: list[dict[str, Any]] = []
    last_exc: Optional[Exception] = None

    for attempt in range(1, max_attempts + 1):
        page = context.new_page()
        t0 = time.time()
        try:
            log.info(
                "[%s] attempt %d/%d - navigating & searching (%s)",
                bill.reference_no, attempt, max_attempts, bill.label or "-",
            )
            page.goto(BILL_URL, wait_until="networkidle", timeout=30_000)

            try:
                if bill.search_by == "appno":
                    # Clicking this radio triggers the site's own postback/reload.
                    page.check("#rbSearchByList_1", timeout=15_000)
                    page.wait_for_load_state("networkidle")
                else:
                    page.check("#rbSearchByList_0", timeout=15_000)

                if bill.ru_code:
                    page.select_option("#ruCodeTextBox", bill.ru_code, timeout=15_000)

                page.fill("#searchTextBox", bill.reference_no, timeout=15_000)
                page.click("#btnSearch", timeout=15_000)
            except PWTimeout as exc:
                raise SiteLayoutError(
                    "The search form could not be used (a field or button was not found) - "
                    "the FESCO site layout may have changed"
                ) from exc

            try:
                page.wait_for_selector(".consumer-detail-card--gbn", timeout=20_000)
            except PWTimeout as exc:
                snippet = page.inner_text("body")[:400].replace("\n", " ").strip()
                raise RuntimeError(
                    f"No bill rendered for reference {bill.reference_no} "
                    f"(invalid number, or site returned an error). Page said: {snippet!r}"
                ) from exc

            try:
                wait_for_bill_fields(page, timeout_ms=30_000)
            except PWTimeout as exc:
                raise SiteLayoutError(
                    f"Bill fields did not finish populating for reference {bill.reference_no} "
                    "- the FESCO site layout may have changed"
                ) from exc

            data: dict[str, Any] = page.evaluate(SCRAPE_JS)
            data.setdefault("reference_no", bill.reference_no)
            data["label"] = bill.label

            cleared_naturally = wait_for_loader_to_clear(page, loader_timeout_ms)
            if not cleared_naturally:
                log.warning(
                    "[%s] loading overlay still visible after %.0fs - forcing it away before capture",
                    bill.reference_no, loader_timeout_ms / 1000,
                )
            force_hide_loader(page)

            pdf_path, png_path = save_bill_artifacts(page, bill, out_dir)

            elapsed = time.time() - t0
            attempt_log.append({"attempt": attempt, "outcome": "success", "seconds": round(elapsed, 1)})
            page.close()
            return data, pdf_path, png_path, attempt_log

        except Exception as exc:  # noqa: BLE001 - anything here is retryable
            elapsed = time.time() - t0
            is_site_layout = isinstance(exc, SiteLayoutError)
            is_site_down   = isinstance(exc, (ConnectionError, TimeoutError, OSError)) and not is_site_layout
            log.warning(
                "[%s] attempt %d/%d failed after %.1fs (%s): %s",
                bill.reference_no, attempt, max_attempts, elapsed,
                "site-down" if is_site_down else ("layout-change" if is_site_layout else "error"),
                exc,
            )
            attempt_log.append({
                "attempt": attempt, "outcome": "failed",
                "seconds": round(elapsed, 1), "error": str(exc),
                "kind": "site_down" if is_site_down else ("layout_change" if is_site_layout else "fetch_error"),
            })
            last_exc = exc
            page.close()
            if attempt < max_attempts:
                # Full-jitter exponential back-off: sleep between 0 and cap
                cap     = 8 if is_site_down else 30   # shorter wait if site is just down
                backoff = random.uniform(0, min(cap, 2 ** attempt))
                log.info("[%s] retrying in %.1fs…", bill.reference_no, backoff)
                time.sleep(backoff)

    assert last_exc is not None
    last_exc.attempt_log = attempt_log  # type: ignore[attr-defined]
    raise last_exc


def save_bill_artifacts(page: Page, bill: BillRequest, out_dir: Path) -> tuple[Optional[Path], Optional[Path]]:
    """Save a printable PDF and a PNG screenshot of just the bill (not the whole page chrome)."""
    safe_ref = "".join(c for c in bill.reference_no if c.isalnum()) or "bill"
    pdf_path = out_dir / f"{safe_ref}.pdf"
    png_path = out_dir / f"{safe_ref}.png"

    try:
        page.pdf(path=str(pdf_path), print_background=True, format="A4")
    except Exception as exc:  # noqa: BLE001
        log.warning("PDF generation failed for %s (%s); continuing without it", bill.reference_no, exc)
        pdf_path = None

    try:
        locator = page.locator(".a4-container.maincontent").first
        locator.screenshot(path=str(png_path))
    except Exception as exc:  # noqa: BLE001
        log.warning("Screenshot failed for %s (%s); continuing without it", bill.reference_no, exc)
        png_path = None

    return pdf_path, png_path


# --------------------------------------------------------------------------- #
# Email rendering & sending
# --------------------------------------------------------------------------- #

def esc(value: Any) -> str:
    if value in (None, ""):
        return "—"
    return html.escape(str(value))


def render_email_html(data: dict[str, Any], bill: BillRequest, has_screenshot: bool) -> str:
    rows = [
        ("Reference No", data.get("reference_no") or bill.reference_no),
        ("Name &amp; Address", data.get("name_address")),
        ("Bill Month", data.get("bill_month")),
        ("Reading Date", data.get("reading_date")),
        ("Issue Date", data.get("issue_date")),
        ("Previous Reading", data.get("previous_reading")),
        ("Present Reading", data.get("present_reading")),
        ("Units Consumed", data.get("units")),
        ("Due Date", data.get("due_date")),
    ]
    row_html = "".join(
        f'''<tr>
              <td style="padding:10px 16px;border-bottom:1px solid #eef1f4;color:#64748b;
                         font-size:13px;font-weight:600;width:42%;">{label}</td>
              <td style="padding:10px 16px;border-bottom:1px solid #eef1f4;color:#1e293b;
                         font-size:14px;font-weight:600;">{esc(value)}</td>
            </tr>'''
        for label, value in rows
    )

    after_till = data.get("payable_after_due_till") or {}
    after_after = data.get("payable_after_due_after") or {}

    screenshot_row = (
        """<tr><td style="padding:4px 24px 20px 24px;">
             <img src="cid:billshot" alt="Bill"
                  style="width:100%;border-radius:8px;border:1px solid #e2e8f0;display:block;" />
           </td></tr>"""
        if has_screenshot
        else ""
    )

    return f"""<!DOCTYPE html>
<html>
<head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1"></head>
<body style="margin:0;padding:0;background:#f1f5f9;font-family:'Segoe UI',Arial,sans-serif;">
  <table role="presentation" width="100%" cellpadding="0" cellspacing="0" style="background:#f1f5f9;padding:24px 0;">
    <tr><td align="center">
      <table role="presentation" width="600" cellpadding="0" cellspacing="0"
             style="background:#ffffff;border-radius:12px;overflow:hidden;
                    box-shadow:0 2px 10px rgba(0,0,0,0.08);max-width:600px;">

        <tr><td style="background:#7a3b1e;padding:20px 24px;">
          <span style="color:#ffffff;font-size:18px;font-weight:700;">⚡ FESCO Electricity Bill</span><br/>
          <span style="color:#f1d9cc;font-size:12px;">{esc(bill.label) if bill.label else 'Consumer'} &middot; {esc(data.get('bill_month'))}</span>
        </td></tr>

        <tr><td style="padding:0;">
          <table role="presentation" width="100%" cellpadding="0" cellspacing="0">{row_html}</table>
        </td></tr>

        <tr><td style="padding:20px 24px 4px 24px;">
          <table role="presentation" width="100%" cellpadding="0" cellspacing="0">
            <tr>
              <td width="48%" style="background:#ecfdf5;border-radius:10px;padding:14px;vertical-align:top;">
                <div style="color:#047857;font-size:11px;font-weight:700;letter-spacing:.03em;">PAYABLE WITHIN DUE DATE</div>
                <div style="color:#065f46;font-size:23px;font-weight:800;margin-top:4px;">Rs. {esc(data.get('payable_within_due'))}</div>
                <div style="color:#047857;font-size:12px;margin-top:2px;">by {esc(data.get('due_date'))}</div>
              </td>
              <td width="4%">&nbsp;</td>
              <td width="48%" style="background:#fef2f2;border-radius:10px;padding:14px;vertical-align:top;">
                <div style="color:#b91c1c;font-size:11px;font-weight:700;letter-spacing:.03em;">PAYABLE AFTER DUE DATE</div>
                <div style="color:#7f1d1d;font-size:15px;font-weight:800;margin-top:6px;">
                  Rs. {esc(after_till.get('amount'))}
                  <span style="font-weight:500;font-size:11px;">(till {esc(after_till.get('period'))})</span>
                </div>
                <div style="color:#7f1d1d;font-size:15px;font-weight:800;margin-top:4px;">
                  Rs. {esc(after_after.get('amount'))}
                  <span style="font-weight:500;font-size:11px;">({esc(after_after.get('period'))})</span>
                </div>
              </td>
            </tr>
          </table>
        </td></tr>

        {screenshot_row}

        <tr><td style="padding:16px 24px 24px 24px;color:#94a3b8;font-size:11px;
                       text-align:center;border-top:1px solid #eef1f4;">
          Auto-generated bill summary &middot; Source: bill.pitc.com.pk/fescobill<br/>
          This is not an official receipt. Always verify against the original bill before payment.
        </td></tr>

      </table>
    </td></tr>
  </table>
</body>
</html>"""


def render_email_minimal(data: dict[str, Any], bill: BillRequest, has_screenshot: bool) -> str:
    """Short body for people who chose not to receive the full text summary."""
    image = (
        '<p><img src="cid:billshot" alt="Bill" style="max-width:100%;height:auto;border:1px solid #e2e8f0;"></p>'
        if has_screenshot else ""
    )
    return (
        '<html><body style="font-family:Arial,sans-serif;color:#1e293b;">'
        f"<p>FESCO bill for <b>{esc(data.get('bill_month'))}</b> "
        f"(Reference {esc(data.get('reference_no') or bill.reference_no)}).</p>{image}</body></html>"
    )


def send_email(
    cfg: dict[str, Any],
    to_addr: str,
    data: dict[str, Any],
    bill: BillRequest,
    html_body: str,
    pdf_path: Optional[Path],
    png_path: Optional[Path],
) -> None:
    msg = MIMEMultipart("mixed")
    msg["Subject"] = f"FESCO Bill - {data.get('bill_month', '')} - Ref {data.get('reference_no') or bill.reference_no}"
    msg["From"] = cfg["sender_email"]
    msg["To"] = to_addr

    related = MIMEMultipart("related")
    related.attach(MIMEText(html_body, "html", "utf-8"))

    if png_path and png_path.exists():
        with open(png_path, "rb") as f:
            img = MIMEImage(f.read())
        img.add_header("Content-ID", "<billshot>")
        img.add_header("Content-Disposition", "inline", filename=png_path.name)
        related.attach(img)

    msg.attach(related)

    if pdf_path and pdf_path.exists():
        with open(pdf_path, "rb") as f:
            part = MIMEApplication(f.read(), _subtype="pdf")
        part.add_header("Content-Disposition", "attachment", filename=pdf_path.name)
        msg.attach(part)

    with smtplib.SMTP(cfg["smtp_host"], cfg["smtp_port"], timeout=30) as server:
        server.starttls()
        server.login(cfg["sender_email"], cfg["sender_password"])
        server.sendmail(cfg["sender_email"], [to_addr], msg.as_string())

    log.info("Emailed bill %s to %s", bill.reference_no, to_addr)


def send_email_with_retry(
    cfg: dict,
    to_addr: str,
    data: dict,
    bill,
    html_body: str,
    pdf_path,
    png_path,
    max_retries: int = 2,
) -> None:
    """Retry send_email up to max_retries times with linear back-off."""
    last_exc: Exception = RuntimeError("no attempts")
    for attempt in range(max_retries):
        try:
            send_email(cfg, to_addr, data, bill, html_body, pdf_path, png_path)
            return
        except Exception as exc:
            last_exc = exc
            if attempt < max_retries - 1:
                wait = 5 * (attempt + 1)
                log.warning("Email to %s failed (attempt %d/%d): %s — retrying in %ds",
                            to_addr, attempt + 1, max_retries, exc, wait)
                time.sleep(wait)
    raise last_exc


# --------------------------------------------------------------------------- #
# Telegram
# --------------------------------------------------------------------------- #

def send_telegram(
    bot_token: str,
    chat_id: str,
    data: dict[str, Any],
    bill: BillRequest,
    png_path: Optional[Path],
    pdf_path: Optional[Path] = None,
    send: tuple[str, ...] = DEFAULT_SEND_TELEGRAM,
) -> None:
    base = f"{telegram_api_base()}/bot{bot_token}"
    send = tuple(send)
    after_till = data.get("payable_after_due_till") or {}
    after_after = data.get("payable_after_due_after") or {}

    text = (
        f"<b>REFERENCE NUMBER</b>\n{esc(data.get('reference_no') or bill.reference_no)}\n\n"
        f"<b>NAME &amp; ADDRESS</b>\n{esc(data.get('name_address'))}\n\n"
        f"<b>BILL DETAILS</b>\n"
        f"Reading date: {esc(data.get('reading_date'))}\n"
        f"Issue date: {esc(data.get('issue_date'))}\n"
        f"Previous reading: {esc(data.get('previous_reading'))}\n"
        f"Present reading: {esc(data.get('present_reading'))}\n"
        f"Units: {esc(data.get('units'))}\n\n"
        f"<b>PAYMENT</b>\n"
        f"Due date: {esc(data.get('due_date'))}\n"
        f"🟢 Payable by due date: Rs. {esc(data.get('payable_within_due'))}\n"
        f"🔴 Payable after due date: Rs. {esc(after_till.get('amount'))} "
        f"(until {esc(after_till.get('period'))})\n"
        f"⚫ Later amount: Rs. {esc(after_after.get('amount'))} "
        f"({esc(after_after.get('period'))})"
    )

    short_caption = f"FESCO bill {data.get('bill_month') or ''} - Ref {data.get('reference_no') or bill.reference_no}".replace("  ", " ")
    delivered = 0

    if "text" in send:
        resp = requests.post(
            f"{base}/sendMessage",
            data={"chat_id": chat_id, "text": text, "parse_mode": "HTML"},
            timeout=20,
        )
        resp.raise_for_status()
        delivered += 1

    if "png" in send:
        if png_path and png_path.exists():
            with open(png_path, "rb") as f:
                resp = requests.post(
                    f"{base}/sendPhoto",
                    data={"chat_id": chat_id, **({} if "text" in send else {"caption": short_caption})},
                    files={"photo": f},
                    timeout=30,
                )
            resp.raise_for_status()
            delivered += 1
        else:
            log.warning("No bill image available for %s - skipping the image for chat %s", bill.reference_no, chat_id)

    if "pdf" in send:
        if pdf_path and pdf_path.exists():
            with open(pdf_path, "rb") as f:
                resp = requests.post(
                    f"{base}/sendDocument",
                    data={"chat_id": chat_id, **({} if ("text" in send or "png" in send) else {"caption": short_caption})},
                    files={"document": f},
                    timeout=60,
                )
            resp.raise_for_status()
            delivered += 1
        else:
            log.warning("No bill PDF available for %s - skipping the PDF for chat %s", bill.reference_no, chat_id)

    if "summary" in send and "text" not in send:
        # Compact two-liner: amount and due date only, no image
        summary_text = (
            f"📋 <b>{html.escape(bill.label or bill.reference_no)}</b>\n"
            f"Month: {html.escape(str(data.get('bill_month') or '?'))}\n"
            f"Due: {html.escape(str(data.get('due_date') or '?'))}\n"
            f"💰 Rs. {html.escape(str(data.get('payable_within_due') or '?'))}"
            f"  |  {html.escape(str(data.get('units') or '?'))} units"
        )
        resp = requests.post(
            f"{base}/sendMessage",
            data={"chat_id": chat_id, "text": summary_text, "parse_mode": "HTML"},
            timeout=20,
        )
        resp.raise_for_status()
        delivered += 1

    if not delivered:
        raise RuntimeError(f"nothing could be sent to chat {chat_id} (wanted: {', '.join(send)})")

    log.info("Sent Telegram message for %s to chat %s", bill.reference_no, chat_id)


# --------------------------------------------------------------------------- #
# WhatsApp via Green API
# --------------------------------------------------------------------------- #

def render_whatsapp_caption(data: dict[str, Any], bill: BillRequest) -> str:
    after_till = data.get("payable_after_due_till") or {}
    after_after = data.get("payable_after_due_after") or {}

    def v(x: Any) -> str:
        return str(x) if x not in (None, "") else "-"

    return (
        f"*REFERENCE NUMBER*\n{v(data.get('reference_no') or bill.reference_no)}\n\n"
        f"*NAME & ADDRESS*\n{v(data.get('name_address'))}\n\n"
        f"*BILL DETAILS*\n"
        f"Reading date: {v(data.get('reading_date'))}\n"
        f"Issue date: {v(data.get('issue_date'))}\n"
        f"Previous reading: {v(data.get('previous_reading'))}\n"
        f"Present reading: {v(data.get('present_reading'))}\n"
        f"Units: {v(data.get('units'))}\n\n"
        f"*PAYMENT*\n"
        f"Due date: {v(data.get('due_date'))}\n"
        f"🟢 Payable by due date: Rs. {v(data.get('payable_within_due'))}\n"
        f"🔴 Payable after due date: Rs. {v(after_till.get('amount'))} "
        f"(until {v(after_till.get('period'))})\n"
        f"⚫ Later amount: Rs. {v(after_after.get('amount'))} "
        f"({v(after_after.get('period'))})"
    )


def render_whatsapp_summary(data: dict[str, Any], bill: BillRequest) -> str:
    """Compact two-liner summary for WhatsApp 'summary' send mode."""
    def v(x: Any) -> str:
        return str(x) if x not in (None, "") else "-"
    label = bill.label or bill.reference_no
    return (
        f"📋 *{label}*\n"
        f"Month: {v(data.get('bill_month'))}\n"
        f"Due: {v(data.get('due_date'))}\n"
        f"Amount: Rs. {v(data.get('payable_within_due'))} | {v(data.get('units'))} units"
    )


def send_whatsapp_message(
    instance_id: str,
    api_token: str,
    phone: str,
    caption: str,
    bill: BillRequest,
    attachment_path: Optional[Path] = None,
    api_base: str = "https://api.green-api.com",
) -> None:
    phone_digits = "".join(ch for ch in phone if ch.isdigit())
    if not phone_digits:
        raise ValueError(f"'{phone}' has no digits - expected a full number with country code")

    base    = f"{api_base.rstrip('/')}/waInstance{instance_id}"
    chat_id = f"{phone_digits}@c.us"

    if attachment_path and attachment_path.exists():
        with open(attachment_path, "rb") as attachment:
            response = requests.post(
                f"{base}/sendFileByUpload/{api_token}",
                data={"chatId": chat_id, "caption": caption},
                files={"file": (attachment_path.name, attachment, "application/octet-stream")},
                timeout=60,
            )
    else:
        response = requests.post(
            f"{base}/sendMessage/{api_token}",
            json={"chatId": chat_id, "message": caption},
            timeout=30,
        )

    response.raise_for_status()
    try:
        msg_id = response.json().get("idMessage")
        if msg_id:
            log.debug("WhatsApp queued for %s → %s (idMessage: %s)", bill.reference_no, phone, msg_id)
            # Schedule a lightweight receipt check in a background thread
            def _check_receipt(instance: str, token: str, mid: str, api: str, ref: str, ph: str) -> None:
                time.sleep(30)
                try:
                    r = requests.post(
                        f"{api.rstrip('/')}/waInstance{instance}/getMessageStatus/{token}",
                        json={"chatId": f'{ph}@c.us', "idMessage": mid},
                        timeout=15,
                    )
                    status = r.json().get("status", "unknown") if r.ok else "error"
                    if status in ("sent", "delivered", "read"):
                        log.info("WhatsApp receipt for %s → %s: %s", ref, ph, status)
                    else:
                        log.warning("WhatsApp receipt for %s → %s: %s (may not have arrived)", ref, ph, status)
                except Exception as exc:
                    log.debug("WhatsApp receipt check failed for %s: %s", ref, exc)

            import threading as _th
            _th.Thread(
                target=_check_receipt,
                args=(instance_id, api_token, msg_id, api_base, bill.reference_no, phone_digits),
                daemon=True,
                name=f"wa-receipt-{msg_id[:8]}",
            ).start()
    except Exception:
        pass  # receipt tracking is best-effort
    log.info("Sent WhatsApp message for %s to %s through Green API", bill.reference_no, phone)


def send_whatsapp_with_retry(
    instance_id: str,
    api_token: str,
    phone: str,
    caption: str,
    bill: BillRequest,
    attachment_path: Optional[Path] = None,
    api_base: str = "https://api.green-api.com",
    max_retries: int = 2,
) -> None:
    """Retry `send_whatsapp_message` up to `max_retries` times with back-off."""
    last_exc: Exception = RuntimeError("no attempts made")
    for attempt in range(max_retries):
        try:
            send_whatsapp_message(
                instance_id, api_token, phone, caption, bill,
                attachment_path=attachment_path, api_base=api_base,
            )
            return
        except Exception as exc:
            last_exc = exc
            if attempt < max_retries - 1:
                wait = 5 * (attempt + 1)
                log.warning(
                    "WhatsApp to %s failed (attempt %d/%d): %s — retrying in %ds",
                    phone, attempt + 1, max_retries, exc, wait,
                )
                time.sleep(wait)
    raise last_exc

# Main
# --------------------------------------------------------------------------- #

def main() -> None:
    parser = argparse.ArgumentParser(description="Fetch FESCO bills and notify recipients by email / Telegram")
    parser.add_argument("--history", default=HISTORY_FILE_DEFAULT, type=Path, help="Path to the bill history JSON used by the bot's /history (default: bill_history.json)")
    parser.add_argument("--lock-file", default=LOCK_FILE_DEFAULT, type=Path, help="Lock file that stops two runs overlapping (default: fesco_bill_automation.lock)")
    parser.add_argument("--lock-wait", default=900, type=int, help="Seconds to wait for another running instance to finish before giving up (default: 900)")
    parser.add_argument(
        "--cleanup-days", type=int, default=int(os.getenv("BILLS_RETENTION_DAYS", "0") or 0),
        help="Delete saved bill PDFs/PNGs older than this many days (default: BILLS_RETENTION_DAYS from .env, 0 = never)",
    )
    parser.add_argument("--config", default=PROJECT_ROOT / "config" / "config.json", type=Path, help="Path to the bills config JSON")
    parser.add_argument("--state", default=STATE_FILE_DEFAULT, type=Path, help="Path to the progress/history JSON (default: state.json)")
    parser.add_argument("--headed", action="store_true", help="Run the browser with a visible window (debugging)")
    parser.add_argument("--no-email", action="store_true", help="Skip sending emails")
    parser.add_argument("--no-telegram", action="store_true", help="Skip sending Telegram messages")
    parser.add_argument("--no-whatsapp", action="store_true", help="Skip sending WhatsApp messages")
    parser.add_argument(
        "--whatsapp-text-only",
        action="store_true",
        help="Send the scraped bill text on WhatsApp without attaching the bill image",
    )
    parser.add_argument(
        "--whatsapp-pdf",
        action="store_true",
        help="Attach the scraped bill PDF instead of the image on WhatsApp",
    )
    parser.add_argument(
        "--whatsapp-png",
        action="store_true",
        help="Attach the scraped bill PNG on WhatsApp",
    )
    parser.add_argument("--max-attempts", type=int, default=3, help="Retries per bill before giving up (default: 3)")
    parser.add_argument(
        "--loader-timeout", type=float, default=10.0,
        help="Seconds to let the site's loading animation finish before forcing it away for the screenshot (default: 10)",
    )
    parser.add_argument(
        "--force-resend", action="store_true",
        help="Resend a bill even if this reference+month was already delivered before",
    )
    parser.add_argument(
        "--retry-failed-only", action="store_true",
        help="Resume mode: skip bills that already succeeded in state.json, only (re)try the rest",
    )
    parser.add_argument(
        "--refs", default="",
        help="Comma-separated reference numbers. Only these are checked (config.json entries are used if "
             "they match; unknown refs are fetched as ad-hoc bills).",
    )
    parser.add_argument(
        "--reply-chat-id", default="",
        help="Telegram chat id that receives the bill(s) fetched via --refs",
    )
    parser.add_argument(
        "--reply-email", default="",
        help="Extra email address to also send the bill to (when using --refs)",
    )
    args = parser.parse_args()

    run_lock = RunLock(args.lock_file, wait_seconds=args.lock_wait)
    if not run_lock.acquire():
        log.error("Another run is still in progress after waiting %ds (lock file: %s) - exiting", args.lock_wait, args.lock_file)
        sys.exit(3)
    atexit.register(run_lock.release)

    load_dotenv(PROJECT_ROOT / ".env")
    bills = load_bills(args.config)

    manual_mode = bool(args.refs)

    if args.refs:
        # Manual lookup: fetch ONLY these references and deliver ONLY to the person
        # who asked (--reply-chat-id). Recipients saved in config.json (other Telegram
        # chats, WhatsApp, e-mail) are deliberately NOT used. The PDF/PNG is still
        # saved in the bills/ folder like any other run.
        wanted: list[str] = []
        for r in args.refs.split(","):
            r = r.strip()
            if r and r not in wanted:
                wanted.append(r)

        config_by_ref: dict[str, BillRequest] = {}
        for b in bills:
            config_by_ref.setdefault(b.reference_no, b)

        selected: list[BillRequest] = []
        for r in wanted:
            known = config_by_ref.get(r)
            if known:
                # Ref IS in config.json → use config recipients + add the requester's chat
                extra_chats  = [args.reply_chat_id] if args.reply_chat_id and args.reply_chat_id not in known.recipients.telegram_chat_ids else []
                extra_emails = [args.reply_email]   if args.reply_email   and args.reply_email   not in known.recipients.emails else []
                recip = Recipients(
                    emails            = known.recipients.emails + extra_emails,
                    telegram_chat_ids = known.recipients.telegram_chat_ids + extra_chats,
                    whatsapp_numbers  = known.recipients.whatsapp_numbers,
                    send_overrides    = known.recipients.send_overrides,
                    channel_defaults  = known.recipients.channel_defaults,
                )
                log.info("[%s] found in config.json — using configured recipients%s",
                         r, " + reply-chat-id" if extra_chats else "")
            else:
                # Ref NOT in config.json → only reply-chat-id and/or reply-email
                recip = Recipients(
                    telegram_chat_ids = [args.reply_chat_id] if args.reply_chat_id else [],
                    emails            = [args.reply_email]   if args.reply_email   else [],
                )
                log.info("[%s] not in config.json — delivering only to reply-chat-id/email", r)

            selected.append(BillRequest(
                reference_no = r,
                label        = known.label if known else "Manual lookup",
                search_by    = known.search_by if known else "refno",
                ru_code      = known.ru_code   if known else "",
                recipients   = recip,
            ))
        bills = selected
        log.info("Manual lookup for %d reference(s): %s", len(bills), ", ".join(wanted))
    # A manual lookup must NOT touch state.json: otherwise it would mark the bill as
    # "already sent" and the next normal run would skip notifying the real recipients.
    state = {"version": 1, "bills": {}} if manual_mode else load_state(args.state)

    def persist() -> None:
        if not manual_mode:
            save_state(args.state, state)

    email_cfg = {
        "smtp_host": os.getenv("SMTP_HOST", "smtp.gmail.com"),
        "smtp_port": int(os.getenv("SMTP_PORT", "587")),
        "sender_email": os.getenv("GMAIL_ADDRESS"),
        "sender_password": os.getenv("GMAIL_APP_PASSWORD"),
    }
    telegram_token = os.getenv("TELEGRAM_BOT_TOKEN")
    admin_email = os.getenv("ADMIN_ALERT_EMAIL")
    admin_chat_id = os.getenv("ADMIN_ALERT_TELEGRAM_CHAT_ID")

    send_emails = not args.no_email
    send_telegrams = not args.no_telegram

    if send_emails and not (email_cfg["sender_email"] and email_cfg["sender_password"]):
        log.warning("GMAIL_ADDRESS / GMAIL_APP_PASSWORD not set in .env - emails will be skipped")
        send_emails = False

    if send_telegrams and not telegram_token:
        log.info("TELEGRAM_BOT_TOKEN not set - Telegram notifications are disabled (this is fine if unused)")
        send_telegrams = False

    green_api_instance_id = os.getenv("GREEN_API_INSTANCE_ID")
    green_api_token = os.getenv("GREEN_API_TOKEN")
    green_api_url = os.getenv("GREEN_API_URL", "https://api.green-api.com")
    send_whatsapp = not args.no_whatsapp and any(b.recipients.whatsapp_numbers for b in bills)
    if send_whatsapp and not (green_api_instance_id and green_api_token):
        log.warning(
            "GREEN_API_INSTANCE_ID / GREEN_API_TOKEN not set in .env - WhatsApp messages will be skipped"
        )
        send_whatsapp = False

    OUTPUT_DIR.mkdir(exist_ok=True)
    loader_timeout_ms = int(args.loader_timeout * 1000)

    sent = skipped = failed = 0
    layout_problems: list[tuple[BillRequest, str]] = []
    # Collect per-bill results for the batched summary sent at end of run
    successful_bills: list[tuple[BillRequest, dict[str, Any]]] = []
    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=not args.headed)
        context = browser.new_context(viewport={"width": 1280, "height": 1600}, locale="en-US")

        for bill in bills:
            entry = get_bill_state(state, bill.reference_no)
            entry["label"] = bill.label or entry.get("label", "")

            if args.retry_failed_only and entry.get("last_status") in ("success", "success_skipped_duplicate"):
                log.info("Skipping %s - already succeeded previously (--retry-failed-only)", bill.reference_no)
                skipped += 1
                continue

            try:
                data, pdf_path, png_path, _log = fetch_bill_with_retries(
                    context, bill, OUTPUT_DIR, args.max_attempts, loader_timeout_ms
                )
            except Exception as exc:  # noqa: BLE001
                log.error("Giving up on %s after %d attempt(s): %s", bill.reference_no, args.max_attempts, exc)
                record_attempt(entry, "failed", str(exc))
                entry["last_attempt_log"] = getattr(exc, "attempt_log", [])
                persist()
                failed += 1
                if isinstance(exc, SiteLayoutError):
                    layout_problems.append((bill, str(exc)))  # one combined alert after the loop
                elif (admin_email or admin_chat_id) and not manual_mode:
                    alert_admin(email_cfg, admin_email, telegram_token, admin_chat_id, bill, str(exc))
                continue

            record_history(args.history, bill, data)

            key = dedupe_key(bill.reference_no, data.get("bill_month"))
            # A previous attempt may have added the dedupe key before a
            # notification failed. Failed attempts must remain retryable.
            already_sent = key in entry.get("sent_keys", []) and entry.get("last_status") != "failed"

            if already_sent and not args.force_resend:
                log.info(
                    "Bill %s for %s was already sent before - skipping to avoid a duplicate "
                    "(use --force-resend to resend anyway)",
                    bill.reference_no, data.get("bill_month"),
                )
                record_attempt(entry, "success_skipped_duplicate")
                entry["last_bill_month"] = data.get("bill_month")
                persist()
                skipped += 1
                continue

            html_cache: dict[tuple[bool, bool], str] = {}

            if send_emails:
                for addr in bill.recipients.emails:
                    try:
                        choice = bill.recipients.send_for("email", addr) or DEFAULT_SEND_EMAIL
                        want_text, want_png, want_pdf = "text" in choice, "png" in choice, "pdf" in choice
                        has_shot = want_png and bool(png_path)
                        if (want_text, has_shot) not in html_cache:
                            html_cache[(want_text, has_shot)] = (
                                render_email_html(data, bill, has_screenshot=has_shot)
                                if want_text else render_email_minimal(data, bill, has_screenshot=has_shot)
                            )
                        send_email_with_retry(
                            email_cfg, addr, data, bill, html_cache[(want_text, has_shot)],
                            pdf_path if want_pdf else None,
                            png_path if want_png else None,
                        )
                    except Exception as exc:  # noqa: BLE001
                        log.error("Email to %s failed for %s after retries: %s", addr, bill.reference_no, exc)
                        if (admin_email or admin_chat_id) and not manual_mode:
                            alert_admin(email_cfg, admin_email, telegram_token, admin_chat_id,
                                        bill, f"Email delivery to {addr} failed: {exc}")

            telegram_failures: list[str] = []
            if send_telegrams:
                for chat_id in bill.recipients.telegram_chat_ids:
                    try:
                        send_telegram(
                            telegram_token, chat_id, data, bill, png_path, pdf_path,
                            bill.recipients.send_for("telegram", chat_id) or DEFAULT_SEND_TELEGRAM,
                        )
                    except Exception as exc:  # noqa: BLE001
                        log.error("Telegram to %s failed for %s: %s", chat_id, bill.reference_no, exc)
                        telegram_failures.append(f"{chat_id}: {exc}")

            if manual_mode and (telegram_failures or not send_telegrams or not bill.recipients.telegram_chat_ids):
                # The only point of a manual lookup is delivering to the requester - say so if that failed.
                log.error("Bill %s was fetched (saved in %s/) but could NOT be delivered via Telegram", bill.reference_no, OUTPUT_DIR)
                failed += 1
                continue

            whatsapp_failures: list[str] = []
            if send_whatsapp and bill.recipients.whatsapp_numbers:
                wa_caption = render_whatsapp_caption(data, bill)
                # Default (unchanged): text, plus the PDF/PNG only if a --whatsapp-pdf / --whatsapp-png flag was given.
                wa_default: tuple[str, ...] = ("text",) + (("pdf",) if args.whatsapp_pdf else ("png",) if args.whatsapp_png else ())
                for phone in bill.recipients.whatsapp_numbers:
                    try:
                        choice = bill.recipients.send_for("whatsapp", phone) or wa_default

                        # summary mode: compact text, no attachments
                        if "summary" in choice and "text" not in choice:
                            send_whatsapp_with_retry(
                                green_api_instance_id, green_api_token, phone,
                                render_whatsapp_summary(data, bill),
                                bill, attachment_path=None, api_base=green_api_url,
                            )
                        else:
                            files: list[Path] = []
                            if "pdf" in choice and pdf_path and pdf_path.exists():
                                files.append(pdf_path)
                            if "png" in choice and png_path and png_path.exists():
                                files.append(png_path)
                            caption = wa_caption if "text" in choice else ""
                            if files:
                                for index, attachment in enumerate(files):
                                    send_whatsapp_with_retry(
                                        green_api_instance_id, green_api_token, phone,
                                        caption if index == 0 else "",
                                        bill, attachment_path=attachment, api_base=green_api_url,
                                    )
                            else:
                                if "text" not in choice:
                                    log.warning(
                                        "WhatsApp %s wanted %s but no file is available"
                                        " - sending text instead", phone, "/".join(choice),
                                    )
                                send_whatsapp_with_retry(
                                    green_api_instance_id, green_api_token, phone,
                                    wa_caption, bill, attachment_path=None, api_base=green_api_url,
                                )
                    except Exception as exc:  # noqa: BLE001
                        log.error("WhatsApp to %s failed for %s: %s", phone, bill.reference_no, exc)
                        whatsapp_failures.append(f"{phone}: {exc}")

            if whatsapp_failures:
                error = "WhatsApp delivery failed: " + "; ".join(whatsapp_failures)
                record_attempt(entry, "failed", error)
                entry["last_bill_month"] = data.get("bill_month")
                persist()
                failed += 1
                log.error("Bill %s not marked sent because WhatsApp delivery failed", bill.reference_no)
                # Alert admin about the WhatsApp failure
                if (admin_email or admin_chat_id) and not manual_mode:
                    alert_admin(email_cfg, admin_email, telegram_token, admin_chat_id, bill, error)
                continue

            record_attempt(entry, "success")
            entry["last_bill_month"] = data.get("bill_month")
            if key not in entry["sent_keys"]:
                entry["sent_keys"] = (entry["sent_keys"] + [key])[-12:]  # keep last ~12 periods
            persist()  # save after every bill, not just at the end

            successful_bills.append((bill, data))
            sent += 1
            time.sleep(1.5)  # be polite to the server between lookups

        browser.close()

    if layout_problems and not manual_mode and (admin_email or admin_chat_id):
        alert_site_layout_changed(email_cfg, admin_email, telegram_token, admin_chat_id, layout_problems)

    # Send a single combined summary when multiple bills succeeded in one run
    if len(successful_bills) > 1 and send_telegrams and admin_chat_id and not manual_mode:
        lines = [f"📊 Run complete: {sent} bill(s) fetched\n"]
        for b, d in successful_bills:
            label  = b.label or b.reference_no
            amount = d.get("payable_within_due") or "?"
            due    = d.get("due_date") or "?"
            month  = d.get("bill_month") or "?"
            lines.append(f"• {label} — Rs. {amount} — due {due} ({month})")
        if skipped: lines.append(f"\n{skipped} already sent (skipped) | {failed} failed")
        try:
            base = telegram_api_base()
            requests.post(
                f"{base}/sendMessage",
                data={"chat_id": admin_chat_id, "text": "\n".join(lines)},
                timeout=20,
            )
            log.info("Sent batched summary to admin chat %s", admin_chat_id)
        except Exception as exc:
            log.warning("Could not send batched summary: %s", exc)

    cleanup_old_files(OUTPUT_DIR, args.cleanup_days)

    if manual_mode:
        log.info("Done. %d sent, %d failed.", sent, failed)
        if failed:
            sys.exit(1)  # lets the Telegram bot report it as a failure
    else:
        log.info("Done. %d sent, %d skipped (already done), %d failed. State saved to %s", sent, skipped, failed, args.state)


if __name__ == "__main__":
    main()