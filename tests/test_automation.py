"""Tests for fesco_bill_automation.py - everything runs against a fake browser and fake network."""
import base64
import json
import os
import subprocess
import sys
import textwrap
import time
import types
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
TINY_PNG = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mNk+M9QDwADhgGAWjR9awAAAABJRU5ErkJggg=="
)

BILL = {
    "reference_no": "12345678901234", "bill_month": "SEP 26", "name_address": "A. Customer, Sample Street",
    "units": "420", "due_date": "25 SEP 26", "payable_within_due": "12,500", "issue_date": "10 SEP 26",
    "present_reading": "5200",
}


# --------------------------------------------------------------------------- #
# Config parsing: per-person delivery choices
# --------------------------------------------------------------------------- #
def write_config(tmp_path, bills, **extra):
    path = tmp_path / "config.json"
    path.write_text(json.dumps({"default_whatsapp_region": "PK", "bills": bills, **extra}))
    return path


def test_plain_config_still_works(script_module, tmp_path):
    cfg = write_config(tmp_path, [{"reference_no": "111111", "recipients": {
        "emails": ["a@example.com"], "telegram_chat_ids": ["1", 2], "whatsapp_numbers": ["03001234567"]}}])
    r = script_module.load_bills(cfg)[0].recipients
    assert r.emails == ["a@example.com"] and r.telegram_chat_ids == ["1", "2"]
    assert r.whatsapp_numbers == ["923001234567"] and r.send_overrides == {}


def test_per_person_choices_parse(script_module, tmp_path):
    cfg = write_config(tmp_path, [{"reference_no": "111111", "recipients": {
        "emails": ["plain@example.com", {"address": "pdf@example.com", "send": ["pdf"]}],
        "telegram_chat_ids": [{"chat_id": "9", "send": "text"}, {"chat_id": "8", "send": ["image", "document"]}],
        "whatsapp_numbers": [{"number": "0321 1234567", "send": ["text", "pdf"]}],
    }}], delivery_defaults={"telegram": ["text"]})
    r = script_module.load_bills(cfg)[0].recipients
    assert r.send_for("email", "plain@example.com") is None
    assert r.send_for("email", "pdf@example.com") == ("pdf",)
    assert r.send_for("telegram", "9") == ("text",)
    assert r.send_for("telegram", "8") == ("png", "pdf")                 # aliases understood
    assert r.send_for("whatsapp", "923211234567") == ("text", "pdf")      # keyed by the normalised number
    assert r.send_for("telegram", "unknown") == ("text",)                 # falls back to delivery_defaults


def test_disabled_and_broken_entries(script_module, tmp_path):
    cfg = write_config(tmp_path, [{"reference_no": "111111", "recipients": {
        "emails": [{"address": "off@example.com", "enabled": False}, {"send": ["pdf"]}, 42.5, "ok@example.com",
                   {"address": "bad@example.com", "send": ["video"]}],
        "whatsapp_numbers": ["not-a-number", "03001234567"]}}])
    r = script_module.load_bills(cfg)[0].recipients
    assert r.emails == ["ok@example.com", "bad@example.com"]      # disabled / no address / junk skipped
    assert r.send_for("email", "bad@example.com") is None                 # unusable "send" -> default
    assert r.whatsapp_numbers == ["923001234567"]


def test_empty_config_is_an_error(script_module, tmp_path):
    with pytest.raises(ValueError):
        script_module.load_bills(write_config(tmp_path, []))


# --------------------------------------------------------------------------- #
# Senders
# --------------------------------------------------------------------------- #
@pytest.fixture
def http(script_module, monkeypatch):
    calls = []

    class Resp:
        def raise_for_status(self):
            pass

    def fake_post(url, **kw):
        calls.append((url, kw))
        return Resp()

    monkeypatch.setattr(script_module.requests, "post", fake_post)
    return calls


@pytest.fixture
def files(tmp_path):
    png, pdf = tmp_path / "b.png", tmp_path / "b.pdf"
    png.write_bytes(TINY_PNG)
    pdf.write_bytes(b"%PDF-1.4 test")
    return png, pdf


@pytest.mark.parametrize("send,expected", [
    (("text", "png"), ["sendMessage", "sendPhoto"]),
    (("text",), ["sendMessage"]),
    (("png",), ["sendPhoto"]),
    (("pdf",), ["sendDocument"]),
    (("png", "pdf"), ["sendPhoto", "sendDocument"]),
    (("text", "png", "pdf"), ["sendMessage", "sendPhoto", "sendDocument"]),
])
def test_telegram_choices(script_module, http, files, send, expected):
    bill = script_module.BillRequest(reference_no="12345678901234")
    script_module.send_telegram("TOKEN", "1", BILL, bill, files[0], files[1], send)
    assert [c[0].rsplit("/", 1)[1] for c in http] == expected


def test_telegram_default_is_text_and_image(script_module, http, files):
    script_module.send_telegram("TOKEN", "1", BILL, script_module.BillRequest(reference_no="1"), files[0])
    assert [c[0].rsplit("/", 1)[1] for c in http] == ["sendMessage", "sendPhoto"]


def test_telegram_errors_when_nothing_could_be_sent(script_module, http):
    with pytest.raises(RuntimeError):
        script_module.send_telegram("T", "1", BILL, script_module.BillRequest(reference_no="1"), None, None, ("png",))


def test_telegram_base_url_comes_from_env(script_module, monkeypatch):
    monkeypatch.delenv("TELEGRAM_API_BASE", raising=False)
    monkeypatch.delenv("CLOUDFLARE_WORKER_URL", raising=False)
    assert script_module.telegram_api_base() == "https://api.telegram.org"
    monkeypatch.setenv("CLOUDFLARE_WORKER_URL", "https://worker.example.test/")
    assert script_module.telegram_api_base() == "https://worker.example.test"
    monkeypatch.setenv("TELEGRAM_API_BASE", "https://other.example.test")
    assert script_module.telegram_api_base() == "https://other.example.test"


@pytest.mark.parametrize("variant,pdf_attached,png_inline", [("all", True, True), ("pdf", True, False), ("png", False, True)])
def test_email_attachments_follow_choice(script_module, files, monkeypatch, variant, pdf_attached, png_inline):
    sent = []

    class SMTP:
        def __init__(self, *a, **k): pass
        def __enter__(self): return self
        def __exit__(self, *a): pass
        def starttls(self): pass
        def login(self, *a): pass
        def sendmail(self, f, to, msg): sent.append(msg)

    monkeypatch.setattr(script_module.smtplib, "SMTP", SMTP)
    bill = script_module.BillRequest(reference_no="12345678901234")
    cfg = {"smtp_host": "h", "smtp_port": 1, "sender_email": "a@b.c", "sender_password": "p"}
    html = script_module.render_email_minimal(BILL, bill, has_screenshot=png_inline)
    script_module.send_email(cfg, "to@example.com", BILL, bill, html,
                             files[1] if pdf_attached else None, files[0] if png_inline else None)
    assert ("application/pdf" in sent[0]) is pdf_attached and ("image/png" in sent[0]) is png_inline


# --------------------------------------------------------------------------- #
# Full runs through main() with a fake browser
# --------------------------------------------------------------------------- #
@pytest.fixture
def run_main(script_module, monkeypatch, tmp_path, files):
    """Returns run(argv, fetch=...) -> (exit_code, events). Fake playwright, SMTP and HTTP."""
    events = []
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("GMAIL_ADDRESS", "sender@example.com")
    monkeypatch.setenv("GMAIL_APP_PASSWORD", "app-password")
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "TOKEN")
    monkeypatch.setenv("GREEN_API_INSTANCE_ID", "1")
    monkeypatch.setenv("GREEN_API_TOKEN", "t")
    monkeypatch.setenv("ADMIN_ALERT_TELEGRAM_CHAT_ID", "999")
    monkeypatch.delenv("ADMIN_ALERT_EMAIL", raising=False)
    monkeypatch.delenv("BILLS_RETENTION_DAYS", raising=False)

    class Resp:
        def raise_for_status(self): pass

    def fake_post(url, **kw):
        data = kw.get("data") or {}
        who = data.get("chat_id") or data.get("chatId") or (kw.get("json") or {}).get("chatId")
        kind = url.split("/waInstance1/")[1].split("/")[0] if "waInstance" in url else url.rsplit("/", 1)[1]
        events.append(("http", kind, str(who), data.get("text", "")))
        return Resp()

    class SMTP:
        def __init__(self, *a, **k): pass
        def __enter__(self): return self
        def __exit__(self, *a): pass
        def starttls(self): pass
        def login(self, *a): pass
        def sendmail(self, f, to, msg): events.append(("email", to[0], "application/pdf" in msg, "image/png" in msg))

    class PW:
        def __enter__(self):
            browser = types.SimpleNamespace(new_context=lambda **k: object(), close=lambda: None)
            return types.SimpleNamespace(chromium=types.SimpleNamespace(launch=lambda **k: browser))
        def __exit__(self, *a): pass

    monkeypatch.setattr(script_module.requests, "post", fake_post)
    monkeypatch.setattr(script_module.smtplib, "SMTP", SMTP)
    monkeypatch.setattr(script_module, "sync_playwright", lambda: PW())
    png, pdf = files

    def run(argv, fetch=None):
        events.clear()

        def default_fetch(context, bill, out_dir, attempts, timeout):
            return dict(BILL, reference_no=bill.reference_no), pdf, png, []

        monkeypatch.setattr(script_module, "fetch_bill_with_retries", fetch or default_fetch)
        monkeypatch.setattr(sys, "argv", ["fesco"] + argv)
        exit_handlers = []
        monkeypatch.setattr(script_module.atexit, "register", lambda fn, *a, **k: exit_handlers.append(fn))
        code = 0
        try:
            script_module.main()
        except SystemExit as exc:
            code = exc.code or 0
        run.exit_handlers = list(exit_handlers)
        for handler in exit_handlers:       # what Python does when the process ends
            handler()
        return code, list(events)

    return run


def test_per_person_delivery_end_to_end(run_main, tmp_path):
    cfg = write_config(tmp_path, [{"reference_no": "12345678901234", "recipients": {
        "emails": ["plain@example.com", {"address": "pdf@example.com", "send": ["pdf"]}],
        "telegram_chat_ids": ["111", {"chat_id": "222", "send": ["pdf"]}],
        "whatsapp_numbers": ["03001234567", {"number": "03211234567", "send": ["text", "pdf", "png"]}]}}])
    code, events = run_main(["--config", str(cfg), "--force-resend"])
    assert code == 0
    assert ("email", "plain@example.com", True, True) in events
    assert ("email", "pdf@example.com", True, False) in events
    assert [e[1] for e in events if e[0] == "http" and e[2] == "111"] == ["sendMessage", "sendPhoto"]
    assert [e[1] for e in events if e[0] == "http" and e[2] == "222"] == ["sendDocument"]
    assert [e[1] for e in events if e[2] == "923001234567@c.us"] == ["sendMessage"]
    assert [e[1] for e in events if e[2] == "923211234567@c.us"] == ["sendFileByUpload", "sendFileByUpload"]


def test_whatsapp_flag_changes_default_only(run_main, tmp_path):
    cfg = write_config(tmp_path, [{"reference_no": "12345678901234", "recipients": {
        "whatsapp_numbers": ["03001234567", {"number": "03211234567", "send": ["text"]}]}}])
    _, events = run_main(["--config", str(cfg), "--force-resend", "--whatsapp-pdf", "--no-email", "--no-telegram"])
    assert [e[1] for e in events if e[2] == "923001234567@c.us"] == ["sendFileByUpload"]
    assert [e[1] for e in events if e[2] == "923211234567@c.us"] == ["sendMessage"]


def test_normal_run_updates_state_and_history_and_skips_duplicates(run_main, tmp_path):
    cfg = write_config(tmp_path, [{"reference_no": "12345678901234", "recipients": {"telegram_chat_ids": ["111"]}}])
    code, events = run_main(["--config", str(cfg), "--no-email"])
    assert code == 0 and any(e[2] == "111" for e in events)
    state = json.loads((tmp_path / "state.json").read_text())
    assert "12345678901234|SEP 26" in state["bills"]["12345678901234"]["sent_keys"]
    history = json.loads((tmp_path / "bill_history.json").read_text())
    assert history["12345678901234"][0]["units"] == "420"
    _, events = run_main(["--config", str(cfg), "--no-email"])           # second run: already delivered
    assert not [e for e in events if e[0] == "http"]


def test_manual_lookup_sends_only_to_requester_and_leaves_state_alone(run_main, tmp_path):
    cfg = write_config(tmp_path, [{"reference_no": "12345678901234", "label": "Home", "recipients": {
        "emails": ["family@example.com"], "telegram_chat_ids": ["111"], "whatsapp_numbers": ["03001234567"]}}])
    code, events = run_main(["--config", str(cfg), "--refs", "12345678901234,99999999",
                             "--reply-chat-id", "555", "--force-resend", "--no-email"])
    assert code == 0
    recipients = {e[2] for e in events if e[0] == "http"}
    assert recipients == {"555"}                                          # nobody else, ever
    assert not any(e[0] == "email" for e in events)
    assert sum(1 for e in events if e[1] == "sendMessage") == 2          # one summary per reference
    assert not (tmp_path / "state.json").exists()                         # state untouched
    assert (tmp_path / "bill_history.json").exists()                      # history still recorded


def test_manual_lookup_without_config_entries_and_duplicate_refs(run_main, tmp_path):
    cfg = write_config(tmp_path, [{"reference_no": "12345678901234", "recipients": {}},
                                  {"reference_no": "12345678901234", "label": "Shop", "recipients": {}}])
    _, events = run_main(["--config", str(cfg), "--refs", "12345678901234,12345678901234",
                          "--reply-chat-id", "555", "--no-email"])
    assert sum(1 for e in events if e[1] == "sendMessage") == 1          # fetched and sent once


def test_manual_lookup_reports_failure_with_exit_code_1(run_main, tmp_path):
    cfg = write_config(tmp_path, [{"reference_no": "12345678901234", "recipients": {}}])

    def broken(*a, **k):
        raise RuntimeError("No bill rendered for reference")

    code, events = run_main(["--config", str(cfg), "--refs", "123456", "--reply-chat-id", "555"], fetch=broken)
    assert code == 1
    assert not [e for e in events if e[0] == "http"]                      # no admin alert for a manual typo


def test_manual_lookup_fails_if_requester_cannot_be_reached(run_main, tmp_path, script_module, monkeypatch):
    cfg = write_config(tmp_path, [{"reference_no": "12345678901234", "recipients": {}}])
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "")
    code, _ = run_main(["--config", str(cfg), "--refs", "123456", "--reply-chat-id", "555"])
    assert code == 1


def test_manual_lookup_without_reply_chat_id_fails_clearly(run_main, tmp_path):
    cfg = write_config(tmp_path, [{"reference_no": "12345678901234", "recipients": {}}])
    code, _ = run_main(["--config", str(cfg), "--refs", "123456"])
    assert code == 1


# --------------------------------------------------------------------------- #
# Reliability: site-layout alert, lock, cleanup, logs
# --------------------------------------------------------------------------- #
def test_layout_change_sends_one_combined_alert(run_main, tmp_path, script_module):
    cfg = write_config(tmp_path, [
        {"reference_no": "111111", "recipients": {}}, {"reference_no": "222222", "recipients": {}}])

    def changed(*a, **k):
        raise script_module.SiteLayoutError("search form not found")

    code, events = run_main(["--config", str(cfg), "--no-email"], fetch=changed)
    alerts = [e for e in events if e[0] == "http" and e[2] == "999"]
    assert len(alerts) == 1 and "layout may have changed" in alerts[0][3]
    assert "111111, 222222" in alerts[0][3]


def test_ordinary_failure_alerts_per_bill(run_main, tmp_path):
    cfg = write_config(tmp_path, [{"reference_no": "111111", "recipients": {}}, {"reference_no": "222222", "recipients": {}}])

    def broken(*a, **k):
        raise RuntimeError("No bill rendered")

    _, events = run_main(["--config", str(cfg), "--no-email"], fetch=broken)
    alerts = [e for e in events if e[0] == "http" and e[2] == "999"]
    assert len(alerts) == 2 and all("Failed to fetch bill" in a[3] for a in alerts)
    state = json.loads((tmp_path / "state.json").read_text())
    assert state["bills"]["111111"]["consecutive_failures"] == 1


def test_form_timeout_is_classified_as_layout_change(script_module, files):
    """The real fetch code must turn a missing form element into SiteLayoutError."""
    PWTimeout = script_module.PWTimeout

    class Page:
        def goto(self, *a, **k): pass
        def check(self, *a, **k): raise PWTimeout("selector not found")
        def close(self): pass

    context = types.SimpleNamespace(new_context=None, new_page=lambda: Page())
    bill = script_module.BillRequest(reference_no="123456")
    with pytest.raises(script_module.SiteLayoutError):
        script_module.fetch_bill_with_retries(context, bill, files[0].parent, 1, 1000)


def test_lock_basics(script_module, tmp_path):
    lock_path = tmp_path / "x.lock"
    a = script_module.RunLock(lock_path)
    assert a.acquire() and lock_path.exists()
    b = script_module.RunLock(lock_path, wait_seconds=0)
    assert not b.acquire()
    a.release()
    assert not lock_path.exists() and b.acquire()
    b.release()


def test_stale_lock_from_dead_process_is_taken_over(script_module, tmp_path):
    lock_path = tmp_path / "x.lock"
    dead = subprocess.Popen([sys.executable, "-c", "pass"])
    dead.wait()
    lock_path.write_text(json.dumps({"pid": dead.pid}))
    lock = script_module.RunLock(lock_path)
    assert lock.acquire()
    assert json.loads(lock_path.read_text())["pid"] == os.getpid()
    lock.release()


def test_very_old_lock_is_stale_and_corrupt_lock_recovers(script_module, tmp_path):
    lock_path = tmp_path / "x.lock"
    lock_path.write_text(json.dumps({"pid": os.getpid() + 0}))
    old = time.time() - 10 * 3600
    os.utime(lock_path, (old, old))
    assert script_module.RunLock(lock_path, stale_after=3600).acquire()
    lock_path.unlink()
    lock_path.write_text("garbage{")
    os.utime(lock_path, (old, old))
    assert script_module.RunLock(lock_path).acquire()


def test_waiting_for_a_lock_that_is_released(script_module, tmp_path):
    lock_path = tmp_path / "x.lock"
    holder = subprocess.Popen([sys.executable, "-c", textwrap.dedent(f"""
        import json, os, time
        open({str(lock_path)!r}, "w").write(json.dumps({{"pid": os.getpid()}}))
        time.sleep(1.5)
        os.remove({str(lock_path)!r})
    """)])
    try:
        time.sleep(0.4)
        started = time.time()
        waiter = script_module.RunLock(lock_path, wait_seconds=15)
        assert waiter.acquire()
        assert 0.5 < time.time() - started < 10
        waiter.release()
    finally:
        holder.wait()


def test_two_real_script_processes_never_overlap(tmp_path):
    """Second process must give up with exit code 3 while the first one holds the lock."""
    lock_path = tmp_path / "run.lock"
    holder = subprocess.Popen([sys.executable, "-c", textwrap.dedent(f"""
        import json, os, time
        open({str(lock_path)!r}, "w").write(json.dumps({{"pid": os.getpid()}}))
        time.sleep(30)
    """)])
    try:
        time.sleep(0.5)
        result = subprocess.run(
            [sys.executable, str(ROOT / "fesco_bill_automation.py"), "--lock-file", str(lock_path),
             "--lock-wait", "1", "--config", str(tmp_path / "missing.json")],
            cwd=tmp_path, capture_output=True, text=True, timeout=60,
        )
        assert result.returncode == 3
        assert "Another run is still in progress" in result.stdout + result.stderr
    finally:
        holder.kill()
        holder.wait()


def test_run_registers_lock_release_and_leaves_no_lock_behind(run_main, tmp_path):
    cfg = write_config(tmp_path, [{"reference_no": "12345678901234", "recipients": {}}])
    lock = tmp_path / "custom.lock"
    run_main(["--config", str(cfg), "--lock-file", str(lock), "--no-email", "--no-telegram"])
    assert len(run_main.exit_handlers) == 1 and not lock.exists()


def test_own_pid_in_unheld_lock_is_stale_pid_reuse(script_module, tmp_path):
    lock_path = tmp_path / "x.lock"
    lock_path.write_text(json.dumps({"pid": os.getpid()}))      # left by a crashed run whose PID we now have
    lock = script_module.RunLock(lock_path)
    assert lock.acquire()
    assert not script_module.RunLock(lock_path).acquire()       # but a lock we really hold is respected
    lock.release()


def test_pid_alive(script_module):
    assert script_module.pid_alive(os.getpid())
    gone = subprocess.Popen([sys.executable, "-c", "pass"])
    gone.wait()
    assert not script_module.pid_alive(gone.pid) and not script_module.pid_alive(0)


def test_cleanup_only_removes_old_bill_files(script_module, tmp_path):
    folder = tmp_path / "bills"
    folder.mkdir()
    old, new, other = folder / "old.pdf", folder / "new.png", folder / "notes.txt"
    for f in (old, new, other):
        f.write_text("x")
    ancient = time.time() - 400 * 86400
    for f in (old, other):
        os.utime(f, (ancient, ancient))
    assert script_module.cleanup_old_files(folder, 0) == 0               # disabled
    assert script_module.cleanup_old_files(folder, 180) == 1
    assert not old.exists() and new.exists() and other.exists()
    assert script_module.cleanup_old_files(tmp_path / "missing", 30) == 0


def test_cleanup_runs_from_cli_flag(run_main, tmp_path):
    cfg = write_config(tmp_path, [{"reference_no": "12345678901234", "recipients": {}}])
    (tmp_path / "bills").mkdir()
    stale = tmp_path / "bills" / "999999.pdf"
    stale.write_text("x")
    ancient = time.time() - 400 * 86400
    os.utime(stale, (ancient, ancient))
    run_main(["--config", str(cfg), "--no-email", "--no-telegram", "--cleanup-days", "180"])
    assert not stale.exists()


def test_log_file_rotates(tmp_path):
    """Checked in a fresh interpreter: pytest pre-installs its own log handlers, which would hide ours."""
    code = textwrap.dedent(f"""
        import sys, logging, logging.handlers
        sys.path.insert(0, {str(ROOT)!r})
        import fesco_bill_automation as m
        h = [h for h in logging.getLogger().handlers if isinstance(h, logging.handlers.RotatingFileHandler)]
        print(len(h), h[0].maxBytes, h[0].backupCount)
    """)
    out = subprocess.run([sys.executable, "-c", code], cwd=tmp_path, capture_output=True, text=True, timeout=60)
    assert out.stdout.split() == ["1", "1000000", "5"], out.stderr
    assert (tmp_path / "fesco_bill_automation.log").exists()


def test_history_keeps_one_entry_per_month(script_module, tmp_path):
    path = tmp_path / "h.json"
    bill = script_module.BillRequest(reference_no="111111")
    script_module.record_history(path, bill, {"bill_month": "AUG 26", "units": "300"})
    script_module.record_history(path, bill, {"bill_month": "SEP 26", "units": "420"})
    script_module.record_history(path, bill, {"bill_month": "SEP 26", "units": "425"})
    entries = json.loads(path.read_text())["111111"]
    assert [(e["bill_month"], e["units"]) for e in entries] == [("AUG 26", "300"), ("SEP 26", "425")]


def test_history_survives_corrupt_file(script_module, tmp_path):
    path = tmp_path / "h.json"
    path.write_text("not json")
    script_module.record_history(path, script_module.BillRequest(reference_no="1"), {"bill_month": "AUG 26"})
    assert json.loads(path.read_text())["1"][0]["bill_month"] == "AUG 26"
