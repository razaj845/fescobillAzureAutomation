"""Shared fixtures. Nothing here touches the network, Telegram, or the real FESCO site."""
import importlib.util
import os
import sys
import tempfile
import threading
import types
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent

# The modules read .env-style settings and create log files when imported, so point
# everything at a throw-away folder BEFORE importing them.
_SESSION_DIR = Path(tempfile.mkdtemp(prefix="fesco-tests-"))
os.chdir(_SESSION_DIR)
os.environ.update(
    TELEGRAM_BOT_TOKEN="123456:TEST",
    CLOUDFLARE_WORKER_URL="https://worker.example.test",
    ALLOWED_USER_ID="1,2",
    RUN_USER_IDS="5",
    GETBILLBYREF_USER_IDS="6",
    PLAYWRIGHT_SCRIPT_PATH=str(ROOT / "fesco_bill_automation.py"),
    ACCESS_FILE=str(_SESSION_DIR / "access.json"),
    USAGE_FILE=str(_SESSION_DIR / "usage.json"),
    SAVED_REFS_FILE=str(_SESSION_DIR / "saved_refs.json"),
    ACTIVITY_LOG_FILE=str(_SESSION_DIR / "activity.log"),
    BOT_LOG_FILE=str(_SESSION_DIR / "bot.log"),
)


def _load(name, filename):
    spec = importlib.util.spec_from_file_location(name, ROOT / filename)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="session")
def bot_module():
    return _load("bot_listener", "bot_listener.py")


@pytest.fixture(scope="session")
def script_module():
    return _load("fesco_bill_automation", "fesco_bill_automation.py")


# --------------------------------------------------------------------------- #
# Bot fixtures
# --------------------------------------------------------------------------- #
class FakeTelegram:
    """Records everything the bot tries to send and hands back message-like objects."""

    def __init__(self):
        self.events = []
        self._counter = 100

    def _msg(self, chat_id):
        self._counter += 1
        return types.SimpleNamespace(chat=types.SimpleNamespace(id=chat_id), message_id=self._counter)

    def send_message(self, chat_id, text, **kw):
        self.events.append(("send", chat_id, text, bool(kw.get("reply_markup"))))
        return self._msg(chat_id)

    def reply_to(self, message, text, **kw):
        self.events.append(("reply", message.chat.id, text, bool(kw.get("reply_markup"))))
        return self._msg(message.chat.id)

    def edit_message_reply_markup(self, *a, **k):
        self.events.append(("edit_markup",))

    def answer_callback_query(self, call_id, text=None, **k):
        self.events.append(("answer", text))

    def clear_step_handler_by_chat_id(self, chat_id):
        self.events.append(("clear_step", chat_id))

    def register_next_step_handler(self, *a, **k):
        self.events.append(("next_step",))

    def send_photo(self, chat_id, photo, caption=None, **k):
        self.events.append(("photo", chat_id, caption))

    def process_new_messages(self, msgs):
        self.events.append(("redispatch", msgs[0].text))

    # helpers for assertions
    def texts(self, chat_id=None):
        return [e[2] for e in self.events if e[0] in ("send", "reply") and (chat_id is None or e[1] == chat_id)]

    def last_text(self, chat_id=None):
        return self.texts(chat_id)[-1]

    def clear(self):
        self.events.clear()


@pytest.fixture
def tg(bot_module, monkeypatch, tmp_path):
    """Fresh bot state per test: temp files, no admins/users beyond ids 1,2 (admin), 5 (run), 6 (lookup)."""
    m = bot_module
    fake = FakeTelegram()
    for name in ("send_message", "reply_to", "edit_message_reply_markup", "answer_callback_query",
                 "clear_step_handler_by_chat_id", "register_next_step_handler", "send_photo",
                 "process_new_messages"):
        monkeypatch.setattr(m.bot, name, getattr(fake, name))

    monkeypatch.setattr(m, "ACCESS_FILE", tmp_path / "access.json")
    monkeypatch.setattr(m, "USAGE_FILE", tmp_path / "usage.json")
    monkeypatch.setattr(m, "SAVED_REFS_FILE", tmp_path / "saved_refs.json")
    monkeypatch.setattr(m, "BILL_HISTORY_FILE", tmp_path / "bill_history.json")
    monkeypatch.setattr(m, "BILLS_DIR", tmp_path / "bills")
    monkeypatch.setattr(m, "RUN_LOCK_FILE", tmp_path / "run.lock")
    monkeypatch.setattr(m, "ACTIVITY_LOG_FILE", tmp_path / "activity.log")
    monkeypatch.setattr(m, "ALLOWED_USER_IDS", [1, 2])
    monkeypatch.setattr(m, "PRIMARY_ADMIN_CHAT_ID", 1)
    monkeypatch.setattr(m, "COMMAND_ACCESS", {"run": {5}, "getbillbyref": {6}})
    monkeypatch.setattr(m, "_now", __import__("time").time)
    m.configure_activity_log(tmp_path / "activity.log")

    # empty job state
    with m.state_lock:
        m.waiting_jobs.clear()
        m.current_job = None
        m.last_finished = None
    while not m.job_queue.empty():
        m.job_queue.get_nowait()
    return fake


def make_message(uid, text, name=None, chat_id=None):
    return types.SimpleNamespace(
        from_user=types.SimpleNamespace(id=uid, first_name=name or f"User{uid}"),
        chat=types.SimpleNamespace(id=chat_id or uid),
        text=text,
        message_id=1,
    )


def make_call(uid, data, chat_id=None):
    return types.SimpleNamespace(
        id="cb",
        data=data,
        from_user=types.SimpleNamespace(id=uid, first_name=f"User{uid}"),
        message=types.SimpleNamespace(chat=types.SimpleNamespace(id=chat_id or uid), message_id=9),
    )


@pytest.fixture
def msg():
    return make_message


@pytest.fixture
def call():
    return make_call


@pytest.fixture(scope="session")
def worker_thread(bot_module):
    """The real job worker (one shared daemon thread, as in production)."""
    t = threading.Thread(target=bot_module.worker, daemon=True, name="test-worker")
    t.start()
    return t
