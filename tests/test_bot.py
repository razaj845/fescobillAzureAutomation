"""Tests for bot_listener.py: access, admin commands, limits, restrictions, activity log, queue, cancel."""
import json
import sys
import textwrap
import time
import types

import pytest


def user(uid, name=None):
    return types.SimpleNamespace(id=uid, first_name=name or f"User{uid}")


def wait_for(cond, timeout=10):
    end = time.time() + timeout
    while time.time() < end:
        if cond():
            return True
        time.sleep(0.05)
    return False


# --------------------------------------------------------------------------- #
# Access control
# --------------------------------------------------------------------------- #
def test_env_roles(bot_module, tg):
    m = bot_module
    assert m.has_access(1, "run") and m.has_access(1, "getbillbyref")        # admin: everything
    assert m.has_access(5, "run") and not m.has_access(5, "getbillbyref")    # run-only
    assert m.has_access(6, "getbillbyref") and not m.has_access(6, "run")    # lookup-only
    assert not m.has_any_access(99)


def test_run_requires_run_access(bot_module, tg, msg):
    m = bot_module
    m.trigger_external_script(msg(6, "/run"))
    assert tg.last_text() == "Unauthorized access."
    assert not m.waiting_jobs


def test_getbillbyref_requires_lookup_access(bot_module, tg, msg):
    m = bot_module
    m.ask_for_reference(msg(5, "/getbillbyref"))
    assert tg.last_text() == "Unauthorized access."


def test_stranger_help_shows_their_id(bot_module, tg, msg):
    bot_module.help_command(msg(99, "/start"))
    assert "Your Telegram ID is 99" in tg.last_text()


def test_help_lists_only_allowed_commands(bot_module, tg, msg):
    m = bot_module
    m.help_command(msg(5, "/help"))
    text = tg.last_text()
    assert "/run" in text and "/getbillbyref" not in text and "/adduser" not in text
    tg.clear()
    m.help_command(msg(1, "/help"))
    assert "/adduser" in tg.last_text() and "/health" in tg.last_text()


@pytest.mark.parametrize("handler", ["adduser_command", "removeuser_command", "users_command", "setlimit_command",
                                     "allowref_command", "denyref_command", "activity_command", "health_command"])
def test_admin_commands_denied_for_non_admins(bot_module, tg, msg, handler):
    getattr(bot_module, handler)(msg(6, "/x 123 run"))
    assert tg.last_text() == "Unauthorized access."


# --------------------------------------------------------------------------- #
# /adduser /removeuser /users
# --------------------------------------------------------------------------- #
def test_adduser_grants_access_and_notifies(bot_module, tg, msg):
    m = bot_module
    m.adduser_command(msg(1, "/adduser 777 getbillbyref Ali Khan"))
    assert m.has_access(777, "getbillbyref") and not m.has_access(777, "run")
    assert "can now use: getbillbyref" in tg.last_text(1)
    assert any(e[0] == "send" and e[1] == 777 for e in tg.events)            # the new person was told
    assert m.load_access()["users"]["777"]["name"] == "Ali Khan"


def test_adduser_all_and_merge(bot_module, tg, msg):
    m = bot_module
    m.adduser_command(msg(1, "/adduser 777 run"))
    m.adduser_command(msg(1, "/adduser 777 getbillbyref"))
    assert m.dynamic_commands(777) == {"run", "getbillbyref"}
    m.adduser_command(msg(1, "/adduser 888 all"))
    assert m.dynamic_commands(888) == {"run", "getbillbyref"}


@pytest.mark.parametrize("text", ["/adduser", "/adduser abc run", "/adduser 777", "/adduser 777 fly"])
def test_adduser_bad_input_shows_usage(bot_module, tg, msg, text):
    bot_module.adduser_command(msg(1, text))
    assert "Usage: /adduser" in tg.last_text()
    assert bot_module.load_access()["users"] == {}


def test_adduser_refuses_admin(bot_module, tg, msg):
    bot_module.adduser_command(msg(1, "/adduser 2 run"))
    assert "already an admin" in tg.last_text()


def test_removeuser_one_command_then_everything(bot_module, tg, msg):
    m = bot_module
    m.adduser_command(msg(1, "/adduser 777 all"))
    m.removeuser_command(msg(1, "/removeuser 777 run"))
    assert m.dynamic_commands(777) == {"getbillbyref"}
    m.removeuser_command(msg(1, "/removeuser 777"))
    assert not m.has_any_access(777)
    assert "777" not in m.load_access()["users"]


def test_removeuser_warns_when_also_in_env(bot_module, tg, msg):
    bot_module.removeuser_command(msg(1, "/removeuser 5"))
    assert ".env" in tg.last_text() and "run" in tg.last_text()


def test_removed_last_command_keeps_limits(bot_module, tg, msg):
    m = bot_module
    m.adduser_command(msg(1, "/adduser 777 run"))
    m.setlimit_command(msg(1, "/setlimit 777 hour 3"))
    m.removeuser_command(msg(1, "/removeuser 777 run"))
    assert not m.has_any_access(777)
    assert m.effective_limits(777)["hour"] == 3        # settings survive, access does not


def test_users_lists_everyone(bot_module, tg, msg):
    m = bot_module
    m.adduser_command(msg(1, "/adduser 777 getbillbyref Ali"))
    m.setlimit_command(msg(1, "/setlimit 777 hour 5"))
    m.setlimit_command(msg(1, "/setlimit default day 30"))
    m.users_command(msg(1, "/users"))
    text = tg.last_text()
    assert "👑 1" in text and "777 Ali" in text and "5/hour" in text
    assert "5 - run" in text and "6 - getbillbyref" in text     # env-configured people shown too
    assert "30/day" in text


# --------------------------------------------------------------------------- #
# Lookup limits
# --------------------------------------------------------------------------- #
class Clock:
    def __init__(self):
        self.t = 1_000_000.0

    def __call__(self):
        return self.t


@pytest.fixture
def clock(bot_module, monkeypatch):
    c = Clock()
    monkeypatch.setattr(bot_module, "_now", c)
    return c


def test_hour_limit_blocks_and_recovers(bot_module, tg, msg, clock):
    m = bot_module
    m.setlimit_command(msg(1, "/setlimit 6 hour 3"))
    assert m.check_and_consume(6, 2, "a")[0]
    assert m.check_and_consume(6, 1, "b")[0]
    ok, text = m.check_and_consume(6, 1, "c")
    assert not ok and "3 of 3 used" in text
    clock.t += 1800          # 30 min later: still inside the hour
    assert not m.check_and_consume(6, 1, "d")[0]
    clock.t += 1801          # the first two lookups have aged out
    assert m.check_and_consume(6, 2, "e")[0]


def test_wait_message_tells_when_to_retry(bot_module, tg, msg, clock):
    m = bot_module
    m.setlimit_command(msg(1, "/setlimit 6 hour 1"))
    m.check_and_consume(6, 1, "a")
    clock.t += 600
    ok, text = m.check_and_consume(6, 1, "b")
    assert not ok and "50m" in text


def test_day_limit_independent_of_hour_limit(bot_module, tg, msg, clock):
    m = bot_module
    m.setlimit_command(msg(1, "/setlimit 6 hour 100"))
    m.setlimit_command(msg(1, "/setlimit 6 day 2"))
    assert m.check_and_consume(6, 1, "a")[0]
    clock.t += 7200
    assert m.check_and_consume(6, 1, "b")[0]
    clock.t += 7200
    ok, text = m.check_and_consume(6, 1, "c")
    assert not ok and "last day" in text
    clock.t += 86400
    assert m.check_and_consume(6, 1, "d")[0]


def test_both_windows_enforced_together(bot_module, tg, msg, clock):
    m = bot_module
    m.setlimit_command(msg(1, "/setlimit 6 hour 2"))
    m.setlimit_command(msg(1, "/setlimit 6 day 3"))
    for token in "ab":
        assert m.check_and_consume(6, 1, token)[0]
    clock.t += 3601
    assert m.check_and_consume(6, 1, "c")[0]          # hour is free again, day now 3/3
    clock.t += 3601
    assert not m.check_and_consume(6, 1, "d")[0]      # day limit stops it


def test_default_limit_per_person_override_and_unlimited(bot_module, tg, msg, clock):
    m = bot_module
    m.setlimit_command(msg(1, "/setlimit default hour 1"))
    assert m.effective_limits(6)["hour"] == 1
    m.setlimit_command(msg(1, "/setlimit 6 hour 4"))            # personal limit beats the default
    assert m.effective_limits(6)["hour"] == 4
    m.setlimit_command(msg(1, "/setlimit 6 hour off"))          # explicitly unlimited
    assert m.effective_limits(6)["hour"] is None
    m.setlimit_command(msg(1, "/setlimit 6 hour default"))      # back to the default
    assert m.effective_limits(6)["hour"] == 1


def test_admins_are_never_limited(bot_module, tg, msg, clock):
    m = bot_module
    m.setlimit_command(msg(1, "/setlimit default hour 1"))
    for i in range(5):
        assert m.check_and_consume(1, 1, str(i))[0]
    m.setlimit_command(msg(1, "/setlimit 2 hour 1"))
    assert "never limited" in tg.last_text()


def test_request_larger_than_limit_is_refused(bot_module, tg, msg, clock):
    m = bot_module
    m.setlimit_command(msg(1, "/setlimit 6 hour 2"))
    ok, text = m.check_and_consume(6, 3, "a")
    assert not ok and "3 reference" in text and "2 per hour" in text


def test_limit_zero_blocks_everything(bot_module, tg, msg, clock):
    m = bot_module
    m.setlimit_command(msg(1, "/setlimit 6 day 0"))
    assert not m.check_and_consume(6, 1, "a")[0]


@pytest.mark.parametrize("text", ["/setlimit", "/setlimit 6 week 5", "/setlimit 6 hour many",
                                  "/setlimit abc hour 5", "/setlimit default hour default", "/setlimit 6 hour -3"])
def test_setlimit_bad_input(bot_module, tg, msg, text):
    bot_module.setlimit_command(msg(1, text))
    assert "Usage: /setlimit" in tg.last_text()


def test_usage_survives_restart(bot_module, tg, msg, clock):
    m = bot_module
    m.setlimit_command(msg(1, "/setlimit 6 hour 1"))
    m.check_and_consume(6, 1, "a")
    assert json.loads(m.USAGE_FILE.read_text())["6"][0][1] == 1     # persisted on disk
    assert not m.check_and_consume(6, 1, "b")[0]


def test_submit_lookup_enforces_limit_and_counts_per_reference(bot_module, tg, msg, clock):
    m = bot_module
    m.setlimit_command(msg(1, "/setlimit 6 hour 3"))
    tg.clear()
    assert m.submit_lookup(6, user(6), ["111111", "222222"])            # uses 2 of 3
    assert not m.submit_lookup(6, user(6), ["333333", "444444"])        # would be 4
    assert "Lookup limit reached" in tg.last_text(6)
    assert m.submit_lookup(6, user(6), ["555555"])                      # 3rd one fits
    assert len(m.waiting_jobs) == 2


def test_cancelling_a_queued_job_refunds_the_lookup(bot_module, tg, msg, clock):
    m = bot_module
    m.setlimit_command(msg(1, "/setlimit 6 hour 1"))
    job = m.submit_lookup(6, user(6), ["111111"])
    assert not m.check_and_consume(6, 1, "x")[0]
    m.cancel_jobs(6)
    assert job.cancelled and m.check_and_consume(6, 1, "y")[0]


def test_mylimit_report(bot_module, tg, msg, clock):
    m = bot_module
    m.setlimit_command(msg(1, "/setlimit 6 hour 5"))
    m.setlimit_command(msg(1, "/setlimit 6 day 20"))
    m.check_and_consume(6, 2, "a")
    m.mylimit_command(msg(6, "/mylimit"))
    text = tg.last_text()
    assert "2 used of 5" in text and "2 used of 20" in text and "references: any" in text
    tg.clear()
    m.mylimit_command(msg(1, "/mylimit"))
    assert "admin" in tg.last_text()


# --------------------------------------------------------------------------- #
# Per-person allowed references
# --------------------------------------------------------------------------- #
def test_allowref_restricts_and_any_unrestricts(bot_module, tg, msg):
    m = bot_module
    assert m.refs_blocked(6, ["111111"]) == []
    m.allowref_command(msg(1, "/allowref 6 111111, 222222"))
    assert m.allowed_refs(6) == ["111111", "222222"]
    assert m.refs_blocked(6, ["111111", "999999"]) == ["999999"]
    m.allowref_command(msg(1, "/allowref 6 any"))
    assert m.allowed_refs(6) is None and m.refs_blocked(6, ["999999"]) == []


def test_denyref_and_empty_list_blocks_everything(bot_module, tg, msg):
    m = bot_module
    m.denyref_command(msg(1, "/denyref 6 111111"))
    assert "no restriction" in tg.last_text()
    m.allowref_command(msg(1, "/allowref 6 111111"))
    m.denyref_command(msg(1, "/denyref 6 111111"))
    assert m.allowed_refs(6) == [] and m.refs_blocked(6, ["111111"]) == ["111111"]


def test_allowref_validation(bot_module, tg, msg):
    m = bot_module
    m.allowref_command(msg(1, "/allowref 6 12ab"))
    assert "digits only" in tg.last_text()
    m.allowref_command(msg(1, "/allowref 1 111111"))
    assert "any reference" in tg.last_text()        # admins can't be restricted


def test_blocked_reference_is_rejected_everywhere(bot_module, tg, msg):
    m = bot_module
    m.allowref_command(msg(1, "/allowref 6 111111"))
    tg.clear()
    assert m.submit_lookup(6, user(6), ["999999"]) is None
    assert "not allowed to look up: 999999" in tg.last_text(6)
    assert not m.waiting_jobs

    m.save_command(msg(6, "/save shop 999999"))
    assert "not allowed" in tg.last_text(6)
    m.save_command(msg(6, "/save mine 111111"))
    assert "Saved" in tg.last_text(6)

    m.lastbill_command(msg(6, "/lastbill 999999"))
    assert "not allowed" in tg.last_text(6)
    m.history_command(msg(6, "/history 999999"))
    assert "not allowed" in tg.last_text(6)


def test_whole_request_rejected_if_any_ref_blocked(bot_module, tg, msg):
    m = bot_module
    m.allowref_command(msg(1, "/allowref 6 111111"))
    assert m.submit_lookup(6, user(6), ["111111", "999999"]) is None
    assert not m.waiting_jobs


def test_blocked_ref_does_not_use_up_quota(bot_module, tg, msg, clock):
    m = bot_module
    m.setlimit_command(msg(1, "/setlimit 6 hour 1"))
    m.allowref_command(msg(1, "/allowref 6 111111"))
    m.submit_lookup(6, user(6), ["999999"])
    assert m.submit_lookup(6, user(6), ["111111"])


def test_all_nickname_skips_blocked_refs_and_buttons_hide_them(bot_module, tg, msg):
    m = bot_module
    m.save_command(msg(6, "/save a 111111"))
    m.save_command(msg(6, "/save b 222222"))
    m.allowref_command(msg(1, "/allowref 6 111111"))
    assert m.resolve_tokens(6, "all") == (["111111"], [])
    kb = m.saved_buttons(6)
    labels = [b.text for row in kb.keyboard for b in row]
    assert "📄 a" in labels and "📄 b" not in labels


def test_mylimit_shows_allowed_refs(bot_module, tg, msg):
    m = bot_module
    m.allowref_command(msg(1, "/allowref 6 111111"))
    m.mylimit_command(msg(6, "/mylimit"))
    assert "references: 111111" in tg.last_text(6)


# --------------------------------------------------------------------------- #
# Activity log
# --------------------------------------------------------------------------- #
def test_activity_log_records_commands_denials_and_admin_actions(bot_module, tg, msg, tmp_path):
    m = bot_module
    m.trigger_external_script(msg(6, "/run"))                    # denied
    m.adduser_command(msg(1, "/adduser 777 run Ali"))
    m.submit_lookup(6, user(6, "Sam"), ["123456"])
    log = (tmp_path / "activity.log").read_text(encoding="utf-8")
    assert "DENIED | user=6 (User6) | cmd=/run" in log
    assert "command | user=1 (User1) | cmd=/adduser" in log
    assert "ADMIN adduser | user=1 (User1) | target=777 | commands=run" in log
    assert "lookup | user=6 (Sam) | refs=123456" in log


def test_activity_command_shows_recent_lines(bot_module, tg, msg):
    m = bot_module
    m.activity_command(msg(1, "/activity"))
    assert "command" in tg.last_text(1)            # the /activity call itself was just logged
    m.activity_command(msg(1, "/activity 1"))
    assert tg.last_text(1).count("\n") == 0


def test_activity_log_rotates(bot_module, tmp_path):
    m = bot_module
    m.configure_activity_log(tmp_path / "rot.log")
    handler = m.activity_logger.handlers[0]
    assert handler.maxBytes == m.LOG_MAX_BYTES and handler.backupCount == m.LOG_BACKUP_COUNT


def test_activity_values_cannot_forge_extra_lines(bot_module, tg, tmp_path):
    m = bot_module
    m.activity("lookup", user(6), refs="1\n2026-01-01 | ADMIN adduser | target=666")
    lines = (tmp_path / "activity.log").read_text(encoding="utf-8").splitlines()
    assert len(lines) == 1


# --------------------------------------------------------------------------- #
# /getbillbyref flow
# --------------------------------------------------------------------------- #
def test_inline_lookup_builds_safe_command(bot_module, tg, msg):
    m = bot_module
    m.ask_for_reference(msg(6, "/getbillbyref 12345678901234, 12345678901235"))
    job = m.waiting_jobs[0]
    assert job.cmd[2:] == ["--refs", "12345678901234,12345678901235", "--reply-chat-id", "6",
                           "--force-resend", "--no-email"]


def test_lookup_rejects_garbage_and_option_injection(bot_module, tg, msg):
    m = bot_module
    for text in ["/getbillbyref --headed", "/getbillbyref 12", "/getbillbyref abc; rm -rf", "/getbillbyref 123456,evil"]:
        m.ask_for_reference(msg(6, text))
    assert not m.waiting_jobs


def test_prompt_then_reply(bot_module, tg, msg):
    m = bot_module
    m.ask_for_reference(msg(6, "/getbillbyref"))
    assert ("next_step",) in tg.events
    m.receive_references(msg(6, "111111 , 222222"))
    assert m.waiting_jobs[0].cmd[3] == "111111,222222"


def test_prompt_handles_cancel_other_commands_and_bad_input(bot_module, tg, msg):
    m = bot_module
    m.receive_references(msg(6, "/cancel"))
    assert "Request cancelled" in tg.last_text(6)
    m.receive_references(msg(6, "/status"))
    assert ("redispatch", "/status") in tg.events
    tg.clear()
    m.receive_references(msg(6, "nonsense"))
    assert "Invalid input" in tg.last_text(6) and ("next_step",) in tg.events
    assert not m.waiting_jobs


def test_prompt_keeps_waiting_when_a_stranger_replies(bot_module, tg, msg):
    m = bot_module
    m.receive_references(msg(99, "111111"))
    assert tg.last_text() == "Unauthorized access." and ("next_step",) in tg.events
    assert not m.waiting_jobs


def test_buttons_start_lookups(bot_module, tg, msg, call):
    m = bot_module
    m.save_command(msg(6, "/save shop 12345678901234"))
    m.save_command(msg(6, "/save home 12345678901235"))
    tg.clear()
    m.prompt_button(call(6, "ref:all"))
    assert m.waiting_jobs[0].cmd[3] == "12345678901234,12345678901235"
    m.prompt_button(call(6, "cancel_prompt"))
    assert ("answer", "Cancelled.") in tg.events
    m.prompt_button(call(99, "ref:111111"))
    assert ("answer", "Unauthorized access.") in tg.events


def test_run_command_queues_original_flags(bot_module, tg, msg):
    m = bot_module
    m.trigger_external_script(msg(5, "/run"))
    assert tg.texts(5)[0] == "Starting your Playwright script with correct flags..."
    assert m.waiting_jobs[0].cmd[2:] == ["--force-resend", "--no-email"]


# --------------------------------------------------------------------------- #
# Nicknames
# --------------------------------------------------------------------------- #
def test_nickname_lifecycle_and_validation(bot_module, tg, msg):
    m = bot_module
    m.save_command(msg(6, "/save Shop 12345678901234"))
    assert m.get_saved(6) == {"shop": "12345678901234"}
    for bad in ["/save all 123456", "/save 1abc 123456", "/save shop abc", "/save shop"]:
        m.save_command(msg(6, bad))
    assert m.get_saved(6) == {"shop": "12345678901234"}
    assert m.resolve_tokens(6, "SHOP, 999999, nope") == (["12345678901234", "999999"], ["nope"])
    assert m.get_saved(7) == {}                                      # nicknames are per person
    m.unsave_command(msg(6, "/unsave shop"))
    assert m.get_saved(6) == {}


def test_nickname_limit(bot_module, tg, msg):
    m = bot_module
    for i in range(m.MAX_SAVED_PER_USER):
        m.save_command(msg(6, f"/save n{i} 1234{i:02d}"))
    m.save_command(msg(6, "/save extra 999999"))
    assert "at most" in tg.last_text(6) and len(m.get_saved(6)) == m.MAX_SAVED_PER_USER


def test_lastbill_and_history(bot_module, tg, msg, tmp_path):
    m = bot_module
    (tmp_path / "bills").mkdir()
    (tmp_path / "bills" / "111111.png").write_bytes(b"png")
    m.lastbill_command(msg(6, "/lastbill 111111"))
    assert any(e[0] == "photo" and e[1] == 6 for e in tg.events)
    m.lastbill_command(msg(6, "/lastbill 222222"))
    assert "No saved bill image" in tg.last_text(6)
    m.history_command(msg(6, "/history 111111"))
    assert "No history" in tg.last_text(6)
    m.BILL_HISTORY_FILE.write_text(json.dumps({"111111": [
        {"bill_month": "AUG 26", "units": "300", "payable_within_due": "9,000", "due_date": "25 AUG 26"},
        {"bill_month": "SEP 26", "units": "420", "payable_within_due": "12,500", "due_date": "25 SEP 26"}]}))
    m.history_command(msg(6, "/history 111111"))
    text = tg.last_text(6)
    assert text.index("SEP 26") < text.index("AUG 26") and "12,500" in text


# --------------------------------------------------------------------------- #
# Queue, cancel and the real worker (runs real subprocesses)
# --------------------------------------------------------------------------- #
@pytest.fixture
def scripts(tmp_path):
    (tmp_path / "ok.py").write_text("print('hello from script')\n")
    (tmp_path / "fail.py").write_text("import sys\nprint('2026-10-03 ERROR bad ref')\nsys.exit(1)\n")
    (tmp_path / "boom.py").write_text("raise RuntimeError('kaboom')\n")
    (tmp_path / "sleep.py").write_text("import time\nprint('started', flush=True)\ntime.sleep(60)\n")
    return {k: str(tmp_path / f"{k}.py") for k in ("ok", "fail", "boom", "sleep")}


def make_job(m, cmd, uid=6, chat=6, start=None, admin=None):
    return m.Job(chat, uid, f"User{uid}", [sys.executable, cmd], "test job", start_msg=start, admin_text=admin)


def test_successful_job_reports_output_and_notifies_admin(bot_module, tg, worker_thread, scripts):
    m = bot_module
    m.enqueue(make_job(m, scripts["ok"], admin=lambda s, o: f"admin copy: {s}", start="▶️ go"))
    assert wait_for(lambda: any("hello from script" in t for t in tg.texts(6)))
    assert any("Script finished successfully!" in t for t in tg.texts(6))
    assert wait_for(lambda: any("admin copy" in t for t in tg.texts(1)))


def test_failed_job_shows_stdout_error_when_stderr_empty(bot_module, tg, worker_thread, scripts):
    m = bot_module
    m.enqueue(make_job(m, scripts["fail"]))
    assert wait_for(lambda: any("Script failed with error" in t and "bad ref" in t for t in tg.texts(6)))


def test_crashing_script_shows_traceback(bot_module, tg, worker_thread, scripts):
    m = bot_module
    m.enqueue(make_job(m, scripts["boom"]))
    assert wait_for(lambda: any("kaboom" in t for t in tg.texts(6)))


def test_second_job_waits_for_first_and_cancel_works(bot_module, tg, worker_thread, scripts):
    m = bot_module
    first = make_job(m, scripts["sleep"], start="▶️ first")
    m.enqueue(first)
    assert wait_for(lambda: m.current_job is first and first.proc is not None)
    second = make_job(m, scripts["ok"])
    m.enqueue(second)
    assert any("1 job(s) ahead" in t for t in tg.texts(6))
    assert second in m.waiting_jobs and not any("hello" in t for t in tg.texts(6))

    running, removed = m.cancel_jobs(6)                    # cancels the running job AND the waiting one
    assert running is first and removed == 1
    assert wait_for(lambda: any("Script was cancelled" in t for t in tg.texts(6)))
    assert wait_for(lambda: m.current_job is None)
    assert first.proc.poll() is not None                   # the process really died
    assert not any("hello" in t for t in tg.texts(6))      # the cancelled second job never ran


def test_only_owner_or_admin_can_cancel(bot_module, tg, worker_thread, scripts):
    m = bot_module
    job = make_job(m, scripts["sleep"], uid=6)
    m.enqueue(job)
    assert wait_for(lambda: m.current_job is job and job.proc is not None)
    assert m.cancel_jobs(5) == (None, 0)                   # a different user can't
    assert m.cancel_jobs(5, job_id=job.job_id) == (None, 0)
    running, _ = m.cancel_jobs(1, job_id=job.job_id)       # an admin can
    assert running is job
    assert wait_for(lambda: m.current_job is None)


def test_cancel_command_and_button_messages(bot_module, tg, msg, call, worker_thread, scripts):
    m = bot_module
    m.cancel_command(msg(6, "/cancel"))
    assert tg.last_text(6) == "Nothing to cancel."
    m.cancel_command(msg(99, "/cancel"))
    assert tg.last_text(99) == "Unauthorized access."
    m.cancel_button(call(6, "cx:424242"))
    assert ("answer", "That job has already finished.") in tg.events


def test_status_hides_other_peoples_jobs(bot_module, tg, msg, worker_thread, scripts):
    m = bot_module
    job = make_job(m, scripts["sleep"], uid=6)
    m.enqueue(job)
    assert wait_for(lambda: m.current_job is job and job.proc is not None)
    m.status_command(msg(5, "/status"))
    assert "another user's job" in tg.last_text(5)
    m.status_command(msg(1, "/status"))
    assert "test job" in tg.last_text(1)
    m.cancel_jobs(6)
    assert wait_for(lambda: m.current_job is None)


def test_job_timeout_stops_script(bot_module, tg, worker_thread, scripts, monkeypatch):
    m = bot_module
    monkeypatch.setattr(m, "RUN_TIMEOUT_SECONDS", 1)
    m.enqueue(make_job(m, scripts["sleep"]))
    assert wait_for(lambda: any("timed out" in t for t in tg.texts(6)))


def test_network_error_sending_start_message_does_not_block_job(bot_module, tg, worker_thread, scripts, monkeypatch):
    m = bot_module
    original = m.bot.send_message

    def flaky(chat_id, text, **kw):
        if "Starting" in text:
            raise OSError("network down")
        return original(chat_id, text, **kw)

    monkeypatch.setattr(m.bot, "send_message", flaky)
    m.enqueue(make_job(m, scripts["ok"], start="▶️ Starting: x"))
    assert wait_for(lambda: any("hello from script" in t for t in tg.texts(6)))


# --------------------------------------------------------------------------- #
# Health / start-up
# --------------------------------------------------------------------------- #
def test_health_report(bot_module, tg, msg):
    m = bot_module
    m.health_command(msg(1, "/health"))
    text = tg.last_text(1)
    for part in ("Uptime", "Script: OK", "Queue: idle", "Script lock file: free", "Saved bills", "Default limit"):
        assert part in text


def test_health_shows_lock_and_script_problems(bot_module, tg, msg, monkeypatch):
    m = bot_module
    m.RUN_LOCK_FILE.write_text("{}")
    monkeypatch.setattr(m, "PLAYWRIGHT_SCRIPT_PATH", "/nonexistent/script.py")
    m.health_command(msg(1, "/health"))
    assert "present" in tg.last_text(1) and "Script not found" in tg.last_text(1)


def test_startup_notice_goes_to_primary_admin(bot_module, tg, monkeypatch):
    m = bot_module
    monkeypatch.setattr(m.time, "sleep", lambda s: None)
    m.announce_startup()
    assert "bot is online" in tg.last_text(1)
    tg.clear()
    monkeypatch.setattr(m, "PLAYWRIGHT_SCRIPT_PATH", None)
    m.announce_startup()
    assert "Problems found" in tg.last_text(1) and "PLAYWRIGHT_SCRIPT_PATH" in tg.last_text(1)


def test_startup_notice_retries_then_gives_up(bot_module, tg, monkeypatch):
    m = bot_module
    calls = []
    monkeypatch.setattr(m.time, "sleep", lambda s: None)
    monkeypatch.setattr(m.bot, "send_message", lambda *a, **k: calls.append(1) or (_ for _ in ()).throw(OSError("down")))
    m.announce_startup()
    assert len(calls) == 3


def test_polling_loop_survives_crashes(bot_module, tg, monkeypatch):
    m = bot_module
    attempts = []

    def fake_polling(**kw):
        attempts.append(1)
        if len(attempts) < 3:
            raise ConnectionError("boom")
        raise KeyboardInterrupt

    monkeypatch.setattr(m.bot, "polling", fake_polling)
    monkeypatch.setattr(m.time, "sleep", lambda s: None)
    with pytest.raises(KeyboardInterrupt):
        m.run_forever()
    assert len(attempts) == 3


def test_format_script_output(bot_module):
    f = bot_module.format_script_output
    assert f("", True) == "No output."
    ok = f("2026-10-03 10:00:00 [INFO] Done. 1 sent\nSkipping WhatsApp number x\nattempt 1/3\n", True)
    assert ok == "Done. 1 sent"
    assert len(f("x" * 5000, False)) == 3000
