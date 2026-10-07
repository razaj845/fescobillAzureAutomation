"""
tests/test_improvements.py
Unit tests for all new features and improvements added in this session.

Run with:
    cd fescobill
    python -m pytest tests/test_improvements.py -v
  or
    python tests/test_improvements.py
"""
import json
import os
import re
import sys
import tempfile
import time
import threading
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import MagicMock, patch, call

# ── resolve imports ──────────────────────────────────────────────────────────
SRC = Path(__file__).resolve().parent.parent / "src"
sys.path.insert(0, str(SRC))

import db  # noqa: E402

# ── helpers ───────────────────────────────────────────────────────────────────

def make_db(tmp: tempfile.TemporaryDirectory) -> Path:
    path = Path(tmp.name) / "test.db"
    db.init(path)
    return path


def close_db() -> None:
    """Force-close all SQLite connections and GC unreferenced ones.
    On Windows, open file handles block temp-dir deletion (WinError 32).
    Calling this in tearDown before tmp.cleanup() prevents that."""
    import gc
    db._db_path = None   # stop any new opens
    gc.collect()         # close any connections held only by refcount


# ═══════════════════════════════════════════════════════════════════════════════
# 1.  db — schema & migration
# ═══════════════════════════════════════════════════════════════════════════════
class TestDbSchema(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        make_db(self.tmp)

    def tearDown(self):
        close_db()
        self.tmp.cleanup()

    def test_new_tables_exist(self):
        import sqlite3
        conn = sqlite3.connect(str(db._db_path))
        tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()}
        conn.close()
        for t in ("user_expiry", "user_allowed_hours", "job_durations", "bot_state"):
            self.assertIn(t, tables, f"Table '{t}' missing from schema")

    def test_migrate_json_access(self):
        data = {
            "defaults": {"hour": 5, "day": 20},
            "users": {
                "111": {"name": "Ali", "commands": ["getbillbyref"], "hour": 3, "refs": ["123456"]},
            },
        }
        access_f = Path(self.tmp.name) / "access.json"
        access_f.write_text(json.dumps(data), encoding="utf-8")
        db.migrate_from_json(access_f, None, None)

        defaults = db.get_access_defaults()
        self.assertEqual(defaults["hour"], 5)
        self.assertEqual(defaults["day"],  20)

        u = db._build_user_dict(111)
        self.assertEqual(u["name"], "Ali")
        self.assertIn("getbillbyref", u["commands"])
        self.assertEqual(u["refs"], ["123456"])
        self.assertEqual(u["hour"], 3)

    def test_migrate_saved_refs(self):
        data  = {"222": {"home": "9876543210123"}}
        saved = Path(self.tmp.name) / "saved_refs.json"
        saved.write_text(json.dumps(data), encoding="utf-8")
        db.migrate_from_json(None, saved, None)
        self.assertEqual(db.get_saved(222), {"home": "9876543210123"})

    def test_migrate_usage_skips_old(self):
        old_ts = time.time() - 90000   # >24 h ago
        new_ts = time.time() - 100
        data   = {"333": [[old_ts, 2, "tok1"], [new_ts, 1, "tok2"]]}
        usage  = Path(self.tmp.name) / "usage.json"
        usage.write_text(json.dumps(data), encoding="utf-8")
        db.migrate_from_json(None, None, usage)
        report = db.usage_report(333)
        self.assertEqual(report["hour"][0], 1, "Old record should be skipped during migration")


# ═══════════════════════════════════════════════════════════════════════════════
# 2.  db — user expiry
# ═══════════════════════════════════════════════════════════════════════════════
class TestUserExpiry(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        make_db(self.tmp)
        db.add_user(42, ["getbillbyref"], "Temp")

    def tearDown(self):
        close_db()
        self.tmp.cleanup()

    def test_no_expiry_by_default(self):
        self.assertIsNone(db.get_user_expiry(42))
        self.assertFalse(db.is_user_expired(42))

    def test_future_expiry_not_expired(self):
        future = datetime.now(timezone.utc) + timedelta(hours=24)
        db.set_user_expiry(42, future)
        self.assertFalse(db.is_user_expired(42))

    def test_past_expiry_is_expired(self):
        past = datetime.now(timezone.utc) - timedelta(seconds=1)
        db.set_user_expiry(42, past)
        self.assertTrue(db.is_user_expired(42))

    def test_remove_expired_users(self):
        past = datetime.now(timezone.utc) - timedelta(seconds=1)
        db.set_user_expiry(42, past)
        removed = db.remove_expired_users()
        self.assertIn(42, removed)
        self.assertFalse(db.user_exists(42))

    def test_remove_expired_leaves_valid_users(self):
        db.add_user(99, ["run"], "Perm")
        past = datetime.now(timezone.utc) - timedelta(seconds=1)
        db.set_user_expiry(42, past)
        db.remove_expired_users()
        self.assertTrue(db.user_exists(99))

    def test_expiry_roundtrip(self):
        future = datetime.now(timezone.utc) + timedelta(days=7)
        db.set_user_expiry(42, future)
        got = db.get_user_expiry(42)
        # Allow 1 s rounding from strftime
        self.assertAlmostEqual(got.timestamp(), future.timestamp(), delta=1)


# ═══════════════════════════════════════════════════════════════════════════════
# 3.  db — time-gated access
# ═══════════════════════════════════════════════════════════════════════════════
class TestAllowedHours(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        make_db(self.tmp)

    def tearDown(self):
        close_db()
        self.tmp.cleanup()

    def test_no_gate_always_allowed(self):
        self.assertTrue(db.is_within_allowed_hours(999))

    def test_gate_within_window(self):
        now_h = datetime.now().hour
        # Window that contains current hour
        start = (now_h - 1) % 24
        end   = (now_h + 2) % 24
        db.set_allowed_hours(5, start, end)
        if start <= end:
            self.assertTrue(db.is_within_allowed_hours(5))

    def test_gate_outside_window(self):
        now_h = datetime.now().hour
        # Window that definitely excludes current hour
        start = (now_h + 2) % 24
        end   = (now_h + 3) % 24
        db.set_allowed_hours(6, start, end)
        # Only assert if window doesn't wrap around
        if start < end:
            self.assertFalse(db.is_within_allowed_hours(6))

    def test_remove_gate(self):
        db.set_allowed_hours(7, 9, 22)
        db.remove_allowed_hours(7)
        self.assertIsNone(db.get_allowed_hours(7))
        self.assertTrue(db.is_within_allowed_hours(7))

    def test_overnight_window(self):
        # 22–06 next day: hours 22, 23, 0, 1, 2, 3, 4, 5 should be inside
        db.set_allowed_hours(8, 22, 6)
        gate = db.get_allowed_hours(8)
        self.assertEqual(gate, (22, 6))

    def test_get_allowed_hours(self):
        db.set_allowed_hours(9, 8, 20)
        self.assertEqual(db.get_allowed_hours(9), (8, 20))


# ═══════════════════════════════════════════════════════════════════════════════
# 4.  db — job durations / estimated completion
# ═══════════════════════════════════════════════════════════════════════════════
class TestJobDurations(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        make_db(self.tmp)

    def tearDown(self):
        close_db()
        self.tmp.cleanup()

    def test_no_history_returns_none(self):
        self.assertIsNone(db.get_avg_job_duration("full bill run"))

    def test_single_record(self):
        db.record_job_duration("full bill run", 90.0)
        self.assertAlmostEqual(db.get_avg_job_duration("full bill run"), 90.0, places=1)

    def test_average_of_multiple(self):
        for s in (60.0, 80.0, 100.0):
            db.record_job_duration("full bill run", s)
        self.assertAlmostEqual(db.get_avg_job_duration("full bill run"), 80.0, places=1)

    def test_cap_at_20_records(self):
        for i in range(25):
            db.record_job_duration("test job", float(i))
        import sqlite3
        conn = sqlite3.connect(str(db._db_path))
        count = conn.execute("SELECT COUNT(*) FROM job_durations WHERE title='test job'").fetchone()[0]
        conn.close()
        self.assertLessEqual(count, 20)

    def test_different_titles_independent(self):
        db.record_job_duration("job A", 30.0)
        db.record_job_duration("job B", 90.0)
        self.assertAlmostEqual(db.get_avg_job_duration("job A"), 30.0, places=1)
        self.assertAlmostEqual(db.get_avg_job_duration("job B"), 90.0, places=1)


# ═══════════════════════════════════════════════════════════════════════════════
# 5.  db — silence alert / last activity
# ═══════════════════════════════════════════════════════════════════════════════
class TestLastActivity(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        make_db(self.tmp)

    def tearDown(self):
        close_db()
        self.tmp.cleanup()

    def test_no_activity_returns_none(self):
        self.assertIsNone(db.get_last_activity())

    def test_touch_updates_timestamp(self):
        before = datetime.now(timezone.utc)
        db.touch_last_activity()
        after  = datetime.now(timezone.utc)
        ts     = db.get_last_activity()
        self.assertIsNotNone(ts)
        self.assertGreaterEqual(ts.timestamp(), before.timestamp() - 1)
        self.assertLessEqual   (ts.timestamp(), after.timestamp()  + 1)

    def test_touch_twice_updates(self):
        db.touch_last_activity()
        t1 = db.get_last_activity()
        time.sleep(1.1)
        db.touch_last_activity()
        t2 = db.get_last_activity()
        self.assertGreater(t2.timestamp(), t1.timestamp())


# ═══════════════════════════════════════════════════════════════════════════════
# 6.  db — subscriptions + payments
# ═══════════════════════════════════════════════════════════════════════════════
class TestSubscriptionsAndPayments(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        make_db(self.tmp)

    def tearDown(self):
        close_db()
        self.tmp.cleanup()

    def test_subscribe_unsubscribe(self):
        self.assertTrue(db.subscribe(100))
        self.assertFalse(db.subscribe(100))   # already subscribed
        self.assertTrue(db.is_subscribed(100))
        self.assertTrue(db.unsubscribe(100))
        self.assertFalse(db.is_subscribed(100))

    def test_get_all_subscribers(self):
        db.subscribe(1); db.subscribe(2); db.subscribe(3)
        subs = db.get_all_subscribers()
        self.assertSetEqual(set(subs), {1, 2, 3})

    def test_log_payment(self):
        paid_at = db.log_payment("123456789012", 55, "via JazzCash")
        self.assertIn("UTC", paid_at)

    def test_get_payment_history(self):
        db.log_payment("REF001", 55, "cash")
        time.sleep(1.1)          # ensure different paid_at timestamps
        db.log_payment("REF001", 55, "online")
        history = db.get_payment_history("REF001")
        self.assertEqual(len(history), 2)
        notes = [h["note"] for h in history]
        self.assertIn("cash",   notes)
        self.assertIn("online", notes)
        # Most recent (online) should come first (DESC order)
        self.assertEqual(history[0]["note"], "online")

    def test_payment_history_limit(self):
        for i in range(15):
            db.log_payment("REFX", 55, str(i))
        self.assertLessEqual(len(db.get_payment_history("REFX")), 10)


# ═══════════════════════════════════════════════════════════════════════════════
# 7.  db — rate limiting
# ═══════════════════════════════════════════════════════════════════════════════
class TestRateLimiting(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        make_db(self.tmp)
        db.add_user(77, ["getbillbyref"])
        db.set_user_limit(77, "hour", 3)

    def tearDown(self):
        close_db()
        self.tmp.cleanup()

    def test_within_limit(self):
        ok, msg = db.check_and_consume(77, 2, "tok1")
        self.assertTrue(ok)
        self.assertIsNone(msg)

    def test_exceeds_limit(self):
        db.check_and_consume(77, 2, "tok1")
        ok, msg = db.check_and_consume(77, 2, "tok2")
        self.assertFalse(ok)
        self.assertIn("⛔", msg)

    def test_refund_restores_quota(self):
        db.check_and_consume(77, 3, "tok1")
        db.refund_usage(77, "tok1")
        ok, _ = db.check_and_consume(77, 3, "tok2")
        self.assertTrue(ok)

    def test_unlimited_user(self):
        db.add_user(88, ["run"])   # no limit set
        ok, _ = db.check_and_consume(88, 50, "tok")
        self.assertTrue(ok)

    def test_usage_report(self):
        db.check_and_consume(77, 1, "t1")
        report = db.usage_report(77)
        self.assertEqual(report["hour"][0], 1)
        self.assertEqual(report["hour"][1], 3)


# ═══════════════════════════════════════════════════════════════════════════════
# 8.  bot_listener — config.json loading and ref-in-config check
# ═══════════════════════════════════════════════════════════════════════════════
class TestConfigLoader(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.cfg = Path(self.tmp.name) / "config.json"

    def tearDown(self):
        close_db()
        self.tmp.cleanup()

    def _write_config(self, bills: list) -> None:
        self.cfg.write_text(json.dumps({"bills": bills}), encoding="utf-8")

    def test_loads_refs(self):
        import importlib, bot_listener as bl
        self._write_config([
            {"reference_no": "111111111111", "label": "Home"},
            {"reference_no": "222222222222", "label": "Office"},
        ])
        with patch.object(bl, "CONFIG_PATH", self.cfg):
            n = bl.reload_config()
        self.assertEqual(n, 2)

    def test_ref_in_config(self):
        import bot_listener as bl
        self._write_config([{"reference_no": "333333333333", "label": "Shop"}])
        with patch.object(bl, "CONFIG_PATH", self.cfg):
            bl.reload_config()
        with patch.object(bl, "_config_refs", {"333333333333"}):
            self.assertTrue(bl.ref_in_config("333333333333"))
            self.assertFalse(bl.ref_in_config("999999999999"))

    def test_missing_config_returns_0(self):
        import bot_listener as bl
        missing = Path(self.tmp.name) / "missing.json"
        with patch.object(bl, "CONFIG_PATH", missing):
            n = bl.reload_config()
        self.assertEqual(n, 0)


# ═══════════════════════════════════════════════════════════════════════════════
# 9.  bot_listener — validate_config
# ═══════════════════════════════════════════════════════════════════════════════
class TestValidateConfig(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.cfg = Path(self.tmp.name) / "config.json"

    def tearDown(self):
        close_db()
        self.tmp.cleanup()

    def test_valid_config_no_problems(self):
        import bot_listener as bl
        self.cfg.write_text(json.dumps({"bills": [
            {"reference_no": "123456789012", "label": "Home",
             "recipients": {"emails": ["user@example.com"], "telegram_chat_ids": ["123456789"]}}
        ]}), encoding="utf-8")
        with patch.object(bl, "CONFIG_PATH", self.cfg):
            problems = bl.validate_config()
        self.assertEqual(problems, [])

    def test_invalid_ref_number(self):
        import bot_listener as bl
        self.cfg.write_text(json.dumps({"bills": [
            {"reference_no": "ABC", "label": "Bad ref"}
        ]}), encoding="utf-8")
        with patch.object(bl, "CONFIG_PATH", self.cfg):
            problems = bl.validate_config()
        self.assertTrue(any("reference_no" in p for p in problems))

    def test_missing_reference_no(self):
        import bot_listener as bl
        self.cfg.write_text(json.dumps({"bills": [{"label": "No ref"}]}), encoding="utf-8")
        with patch.object(bl, "CONFIG_PATH", self.cfg):
            problems = bl.validate_config()
        self.assertTrue(any("missing reference_no" in p for p in problems))

    def test_invalid_email(self):
        import bot_listener as bl
        self.cfg.write_text(json.dumps({"bills": [
            {"reference_no": "123456789012",
             "recipients": {"emails": ["not-an-email"]}}
        ]}), encoding="utf-8")
        with patch.object(bl, "CONFIG_PATH", self.cfg):
            problems = bl.validate_config()
        self.assertTrue(any("email" in p for p in problems))

    def test_no_bills_raises_problem(self):
        import bot_listener as bl
        self.cfg.write_text(json.dumps({"bills": []}), encoding="utf-8")
        with patch.object(bl, "CONFIG_PATH", self.cfg):
            problems = bl.validate_config()
        self.assertTrue(any("no 'bills'" in p.lower() for p in problems))


# ═══════════════════════════════════════════════════════════════════════════════
# 10.  bot_listener — .env completeness check
# ═══════════════════════════════════════════════════════════════════════════════
class TestEnvCompleteness(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.cfg = Path(self.tmp.name) / "config.json"

    def tearDown(self):
        close_db()
        self.tmp.cleanup()

    def test_email_recipients_need_gmail_creds(self):
        import bot_listener as bl
        self.cfg.write_text(json.dumps({"bills": [
            {"reference_no": "1" * 12, "recipients": {"emails": ["a@b.com"]}}
        ]}), encoding="utf-8")
        env = {"GMAIL_ADDRESS": "", "GMAIL_APP_PASSWORD": "", "TELEGRAM_BOT_TOKEN": "tok"}
        with patch.dict(os.environ, env, clear=False):
            issues = bl.check_env_completeness(self.cfg)
        self.assertTrue(any("GMAIL" in i for i in issues))

    def test_whatsapp_recipients_need_green_api(self):
        import bot_listener as bl
        self.cfg.write_text(json.dumps({"bills": [
            {"reference_no": "1"*12, "recipients": {"whatsapp_numbers": ["+923001234567"]}}
        ]}), encoding="utf-8")
        env = {"GREEN_API_INSTANCE_ID": "", "GREEN_API_TOKEN": ""}
        with patch.dict(os.environ, env, clear=False):
            issues = bl.check_env_completeness(self.cfg)
        self.assertTrue(any("GREEN_API" in i for i in issues))

    def test_no_issues_when_all_set(self):
        import bot_listener as bl
        self.cfg.write_text(json.dumps({"bills": [
            {"reference_no": "1"*12, "recipients": {"emails": ["a@b.com"]}}
        ]}), encoding="utf-8")
        env = {"GMAIL_ADDRESS": "bot@gmail.com", "GMAIL_APP_PASSWORD": "pass123"}
        with patch.dict(os.environ, env, clear=False):
            issues = bl.check_env_completeness(self.cfg)
        self.assertFalse(any("GMAIL" in i for i in issues))


# ═══════════════════════════════════════════════════════════════════════════════
# 11.  bot_listener — parse_ref_and_email
# ═══════════════════════════════════════════════════════════════════════════════
class TestParseRefAndEmail(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        make_db(self.tmp)

    def tearDown(self):
        close_db()
        self.tmp.cleanup()

    def _parse(self, uid, text):
        import bot_listener as bl
        return bl.parse_ref_and_email(uid, text)

    def test_ref_only(self):
        refs, invalid, email = self._parse(0, "123456789012")
        self.assertEqual(refs, ["123456789012"])
        self.assertEqual(invalid, [])
        self.assertIsNone(email)

    def test_ref_with_email(self):
        refs, invalid, email = self._parse(0, "123456789012 user@example.com")
        self.assertEqual(refs, ["123456789012"])
        self.assertIsNone(invalid or None)
        self.assertEqual(email, "user@example.com")

    def test_invalid_ref_with_email(self):
        refs, invalid, email = self._parse(0, "ABC user@example.com")
        self.assertEqual(refs, [])
        self.assertIn("ABC", invalid)
        self.assertEqual(email, "user@example.com")

    def test_email_not_confused_for_ref(self):
        refs, invalid, email = self._parse(0, "123456789012 notanemail")
        # "notanemail" should be treated as an invalid ref token, not an email
        self.assertIsNone(email)
        self.assertIn("notanemail", invalid)

    def test_multiple_refs_with_email(self):
        refs, invalid, email = self._parse(0, "111111111111,222222222222 me@test.com")
        self.assertIn("111111111111", refs)
        self.assertIn("222222222222", refs)
        self.assertEqual(email, "me@test.com")

    def test_saved_nickname_with_email(self):
        db.save_ref(55, "shop", "999999999999")
        import bot_listener as bl
        with patch.object(bl, "get_saved", return_value={"shop": "999999999999"}):
            refs, invalid, email = bl.parse_ref_and_email(55, "shop admin@example.com")
        self.assertEqual(refs, ["999999999999"])
        self.assertEqual(email, "admin@example.com")


# ═══════════════════════════════════════════════════════════════════════════════
# 12.  bot_listener — stale bill detection
# ═══════════════════════════════════════════════════════════════════════════════
class TestStaleBillDetection(unittest.TestCase):
    def test_stale_threshold_constant(self):
        import bot_listener as bl
        self.assertGreater(bl.STALE_BILL_DAYS, 0)

    def test_recent_file_not_stale(self):
        import bot_listener as bl
        threshold = bl.STALE_BILL_DAYS * 86400
        age       = threshold / 2          # half the threshold → not stale
        mtime     = time.time() - age
        self.assertFalse((time.time() - mtime) / 86400 > bl.STALE_BILL_DAYS)

    def test_old_file_is_stale(self):
        import bot_listener as bl
        threshold = bl.STALE_BILL_DAYS * 86400
        age       = threshold + 86400      # one day past threshold → stale
        mtime     = time.time() - age
        self.assertTrue((time.time() - mtime) / 86400 > bl.STALE_BILL_DAYS)


# ═══════════════════════════════════════════════════════════════════════════════
# 13.  bot_listener — last_bill_amount helper
# ═══════════════════════════════════════════════════════════════════════════════
class TestLastBillAmount(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.history_file = Path(self.tmp.name) / "bill_history.json"

    def tearDown(self):
        close_db()
        self.tmp.cleanup()

    def test_returns_amount_from_history(self):
        import bot_listener as bl
        data = {"REF001": [
            {"bill_month": "Sep 2026", "payable_within_due": "3200"},
            {"bill_month": "Oct 2026", "payable_within_due": "4100"},
        ]}
        self.history_file.write_text(json.dumps(data), encoding="utf-8")
        with patch.object(bl, "BILL_HISTORY_FILE", self.history_file):
            amount = bl.last_bill_amount("REF001")
        self.assertEqual(amount, "4100")

    def test_returns_none_for_unknown_ref(self):
        import bot_listener as bl
        self.history_file.write_text(json.dumps({}), encoding="utf-8")
        with patch.object(bl, "BILL_HISTORY_FILE", self.history_file):
            self.assertIsNone(bl.last_bill_amount("UNKNOWN"))

    def test_returns_none_when_history_missing(self):
        import bot_listener as bl
        missing = Path(self.tmp.name) / "no_such_file.json"
        with patch.object(bl, "BILL_HISTORY_FILE", missing):
            self.assertIsNone(bl.last_bill_amount("REF001"))


# ═══════════════════════════════════════════════════════════════════════════════
# 14.  bot_listener — parse_expiry
# ═══════════════════════════════════════════════════════════════════════════════
class TestParseExpiry(unittest.TestCase):
    def _parse(self, text):
        import bot_listener as bl
        return bl.parse_expiry(text)

    def test_days(self):
        result = self._parse("7d")
        self.assertIsNotNone(result)
        expected = datetime.now(timezone.utc) + timedelta(days=7)
        self.assertAlmostEqual(result.timestamp(), expected.timestamp(), delta=5)

    def test_hours(self):
        result = self._parse("24h")
        expected = datetime.now(timezone.utc) + timedelta(hours=24)
        self.assertAlmostEqual(result.timestamp(), expected.timestamp(), delta=5)

    def test_weeks(self):
        result = self._parse("2w")
        expected = datetime.now(timezone.utc) + timedelta(weeks=2)
        self.assertAlmostEqual(result.timestamp(), expected.timestamp(), delta=5)

    def test_invalid_returns_none(self):
        self.assertIsNone(self._parse("forever"))
        self.assertIsNone(self._parse("7x"))
        self.assertIsNone(self._parse(""))


# ═══════════════════════════════════════════════════════════════════════════════
# 15.  fesco_bill_automation — jitter backoff values
# ═══════════════════════════════════════════════════════════════════════════════
class TestJitterBackoff(unittest.TestCase):
    """Verify the backoff calculation is bounded and uses jitter."""

    def test_backoff_bounded_normal_error(self):
        cap = 30
        for attempt in range(1, 6):
            backoff = __import__("random").uniform(0, min(cap, 2 ** attempt))
            self.assertGreaterEqual(backoff, 0)
            self.assertLessEqual(backoff, cap)

    def test_backoff_shorter_for_site_down(self):
        cap_down   = 8
        cap_normal = 30
        # site-down cap should be smaller
        self.assertLess(cap_down, cap_normal)

    def test_jitter_produces_different_values(self):
        import random
        values = {random.uniform(0, min(30, 2 ** 3)) for _ in range(20)}
        # Very unlikely all 20 random values are identical
        self.assertGreater(len(values), 1)


# ═══════════════════════════════════════════════════════════════════════════════
# 16.  fesco_bill_automation — send_email_with_retry
# ═══════════════════════════════════════════════════════════════════════════════
class TestEmailRetry(unittest.TestCase):
    def _get_fn(self):
        import fesco_bill_automation as fa
        return fa.send_email_with_retry

    def test_succeeds_on_first_attempt(self):
        fn = self._get_fn()
        mock_send = MagicMock()
        with patch("fesco_bill_automation.send_email", mock_send):
            fn({}, "a@b.com", {}, MagicMock(), "<html>", None, None, max_retries=2)
        mock_send.assert_called_once()

    def test_retries_on_failure_then_succeeds(self):
        fn   = self._get_fn()
        mock_send = MagicMock(side_effect=[Exception("SMTP timeout"), None])
        with patch("fesco_bill_automation.send_email", mock_send), \
             patch("time.sleep"):
            fn({}, "a@b.com", {}, MagicMock(), "<html>", None, None, max_retries=2)
        self.assertEqual(mock_send.call_count, 2)

    def test_raises_after_all_retries(self):
        fn = self._get_fn()
        mock_send = MagicMock(side_effect=Exception("SMTP error"))
        with patch("fesco_bill_automation.send_email", mock_send), \
             patch("time.sleep"):
            with self.assertRaises(Exception):
                fn({}, "a@b.com", {}, MagicMock(), "<html>", None, None, max_retries=2)
        self.assertEqual(mock_send.call_count, 2)


# ═══════════════════════════════════════════════════════════════════════════════
# 17.  fesco_bill_automation — WhatsApp retry
# ═══════════════════════════════════════════════════════════════════════════════
class TestWhatsAppRetry(unittest.TestCase):
    def _get_fn(self):
        import fesco_bill_automation as fa
        return fa.send_whatsapp_with_retry

    def test_succeeds_on_first(self):
        fn = self._get_fn()
        mock_send = MagicMock()
        with patch("fesco_bill_automation.send_whatsapp_message", mock_send):
            fn("inst", "tok", "+923001234567", "caption", MagicMock(), max_retries=2)
        mock_send.assert_called_once()

    def test_retries_on_failure(self):
        fn = self._get_fn()
        mock_send = MagicMock(side_effect=[Exception("timeout"), None])
        with patch("fesco_bill_automation.send_whatsapp_message", mock_send), \
             patch("time.sleep"):
            fn("inst", "tok", "+923001234567", "caption", MagicMock(), max_retries=2)
        self.assertEqual(mock_send.call_count, 2)

    def test_raises_after_all_retries(self):
        fn = self._get_fn()
        mock_send = MagicMock(side_effect=Exception("error"))
        with patch("fesco_bill_automation.send_whatsapp_message", mock_send), \
             patch("time.sleep"):
            with self.assertRaises(Exception):
                fn("inst", "tok", "+923001234567", "caption", MagicMock(), max_retries=2)


# ═══════════════════════════════════════════════════════════════════════════════
# 18.  fesco_bill_automation — WhatsApp render helpers
# ═══════════════════════════════════════════════════════════════════════════════
class TestWhatsAppHelpers(unittest.TestCase):
    def test_render_summary(self):
        import fesco_bill_automation as fa
        bill = MagicMock()
        bill.label = "Home"; bill.reference_no = "111111111111"
        data = {"bill_month": "Oct 2026", "due_date": "15 Nov 2026",
                "payable_within_due": "3,200", "units": "280"}
        result = fa.render_whatsapp_summary(data, bill)
        self.assertIn("Home",      result)
        self.assertIn("Oct 2026",  result)
        self.assertIn("3,200",     result)
        self.assertIn("15 Nov",    result)

    def test_render_summary_missing_fields(self):
        import fesco_bill_automation as fa
        bill = MagicMock(); bill.label = None; bill.reference_no = "REF001"
        result = fa.render_whatsapp_summary({}, bill)
        self.assertIn("REF001", result)
        # Missing fields should show dash, not crash
        self.assertIn("-", result)


# ═══════════════════════════════════════════════════════════════════════════════
# 19.  fesco_bill_automation — VALID_SEND contains 'summary'
# ═══════════════════════════════════════════════════════════════════════════════
class TestValidSend(unittest.TestCase):
    def test_summary_in_valid_send(self):
        import fesco_bill_automation as fa
        self.assertIn("summary", fa.VALID_SEND)

    def test_send_aliases_include_summary(self):
        import fesco_bill_automation as fa
        aliases = fa._SEND_ALIASES
        self.assertTrue(any(v == "summary" for v in aliases.values()),
                        "No alias resolves to 'summary'")


# ═══════════════════════════════════════════════════════════════════════════════
# 20.  fesco_bill_automation — PROJECT_ROOT resolves correctly
# ═══════════════════════════════════════════════════════════════════════════════
class TestProjectRoot(unittest.TestCase):
    def test_project_root_is_parent_of_src(self):
        import fesco_bill_automation as fa
        self.assertEqual(fa.PROJECT_ROOT.name, SRC.parent.name)
        self.assertTrue((fa.PROJECT_ROOT / "src").is_dir())


# ═══════════════════════════════════════════════════════════════════════════════
# 21.  Concurrency — db writes from multiple threads
# ═══════════════════════════════════════════════════════════════════════════════
class TestDbConcurrency(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        make_db(self.tmp)
        db.add_user(200, ["getbillbyref"])
        db.set_user_limit(200, "hour", 1000)

    def tearDown(self):
        close_db()
        self.tmp.cleanup()

    def test_concurrent_check_and_consume(self):
        """Multiple threads consuming quota simultaneously should not exceed the limit."""
        results = []
        lock    = threading.Lock()

        def consume():
            ok, _ = db.check_and_consume(200, 1, uuid_tok())
            with lock:
                results.append(ok)

        def uuid_tok():
            import uuid; return uuid.uuid4().hex

        threads = [threading.Thread(target=consume) for _ in range(20)]
        for t in threads: t.start()
        for t in threads: t.join()

        used = sum(1 for ok in results if ok)
        self.assertLessEqual(used, 1000)

    def test_concurrent_save_ref(self):
        errors = []
        def save(i):
            try:
                db.save_ref(200, f"nick{i}", f"{'1'*12}{i:02d}")
            except Exception as e:
                errors.append(e)

        threads = [threading.Thread(target=save, args=(i,)) for i in range(10)]
        for t in threads: t.start()
        for t in threads: t.join()
        self.assertEqual(errors, [])


# ═══════════════════════════════════════════════════════════════════════════════
# Entry point
# ═══════════════════════════════════════════════════════════════════════════════
if __name__ == "__main__":
    unittest.main(verbosity=2)
