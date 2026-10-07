# FESCO Bill Automation + Telegram Bot

Automatically fetch electricity bills from the FESCO online bill portal (`bill.pitc.com.pk`), save them as PDF/PNG, and deliver them to the right people by **email, Telegram and WhatsApp**. A companion **Telegram bot** lets you (and people you trust) trigger a check from your phone, look up any reference number on demand, and manage who is allowed to do what - all without touching the PC.

> **Disclaimer:** This is an unofficial, personal-use tool. It is not affiliated with FESCO or PITC. It reads the public bill page the same way you would in a browser. Only use it for bills you are entitled to see, keep your request volume reasonable, and respect the website's terms. The selectors can break whenever the site changes (the tool tells you when that happens - see [Reliability](#reliability)).

All IDs, numbers, e-mail addresses and URLs in this document are **dummy examples**.

---

## Table of contents

1. [Features](#features)
2. [How it works](#how-it-works)
3. [Project layout](#project-layout)
4. [Requirements](#requirements)
5. [Installation](#installation)
6. [Configuration](#configuration)
   - [`.env` - secrets and settings](#env---secrets-and-settings)
   - [`config.json` - bills and recipients](#configjson---bills-and-recipients)
   - [Per-person delivery choices](#per-person-delivery-choices)
7. [Setting up the Telegram bot](#setting-up-the-telegram-bot)
8. [Running the automation script](#running-the-automation-script)
9. [Using the bot](#using-the-bot)
   - [Commands for everyone with access](#commands-for-everyone-with-access)
   - [Admin commands](#admin-commands)
   - [Example conversations](#example-conversations)
10. [Access control, limits and restrictions](#access-control-limits-and-restrictions)
11. [Reliability](#reliability)
12. [Files created at runtime](#files-created-at-runtime)
13. [Testing](#testing)
14. [Troubleshooting](#troubleshooting)
15. [Security and privacy](#security-and-privacy)
16. [FAQ](#faq)

---

## Features

**Bill automation (`fesco_bill_automation.py`)**

- Opens the FESCO bill page in a headless browser (Playwright), searches by reference number (or customer ID), scrapes the bill and saves a **PDF and PNG** copy.
- Delivers by **email (Gmail/SMTP)**, **Telegram** and **WhatsApp (Green API)**.
- **Per-person delivery choices** in `config.json`: each person can receive just the summary text, just the PDF, just the image, or any combination - and can be paused with one setting.
- Retries each bill automatically (fresh page each time), remembers what was already sent (**no duplicate bills**), and can resume after a failure.
- **Manual lookups** (`--refs`): fetch specific reference numbers on demand and send the result to _one_ Telegram chat only.
- Keeps a small **bill history** (month, units, amount, due date) for every successful fetch.

**Telegram bot (`bot_listener.py`)**

- `/run` checks all configured bills; `/getbillbyref` fetches specific bills; `/lastbill` and `/history` show saved data instantly.
- **Saved nicknames** per person (`/save shop 12345678901234`), tappable **buttons**, and a **Cancel** button on running jobs.
- One-at-a-time **job queue**: a second request waits for the first to finish.
- **`/status`**, **`/cancel`**, **`/help`**, and **`/mylimit`**.

**Access and admin**

- Per-command permissions (someone can have `/run` only, `/getbillbyref` only, or both).
- **Manage people from Telegram** (`/adduser`, `/removeuser`) - no file editing or restart.
- **Flexible rate limits** per person: lookups **per hour** and/or **per day**, with a default for everyone else.
- **Per-person reference restrictions**: limit someone to specific reference numbers only.
- **Activity log**: who used what, and when (rotating file, viewable with `/activity`).

**Reliability**

- **Runs in the background and never stops**: hidden Windows task with a watchdog, restart-on-failure, no battery/idle/time-limit stops (systemd example for Linux), plus a single-copy guard so two bots can never fight over Telegram.
- "Bot is online" message on every start, plus a `/health` report.
- **Lock file** so a scheduled run and a bot-started run can never overlap.
- **Site-layout-change alert** (one clear message instead of a flood of failures).
- Rotating log files and optional automatic **cleanup of old bill files**.
- Job timeout so a stuck browser can never block the queue.

---

## How it works

```
                     +--------------------+
   your phone  --->  |  Telegram          |
                     +---------+----------+
                               |  (optionally through your own Cloudflare Worker)
                     +---------v----------+
                     |  bot_listener.py   |   permissions, limits, queue, activity log
                     +---------+----------+
                               |  starts, one at a time
                     +---------v----------+      lock file
                     | fesco_bill_        | <--- shared with scheduled runs
                     | automation.py      |
                     +----+----------+----+
                          |          |
              headless browser     delivers to
              (bill.pitc.com.pk)   Email / Telegram / WhatsApp
                          |
                     bills/ (PDF + PNG), state.json, bill_history.json
```

- The **script** can run on its own (manually or on a schedule) using `config.json`.
- The **bot** starts the script for you and passes it the right options. Lookups made through the bot are delivered **only to the person who asked**.

---

## Project layout

```
.
├── fesco_bill_automation.py   # the bill fetcher / sender
├── bot_listener.py            # the Telegram bot
├── config.example.json        # copy to config.json
├── .env.example               # copy to .env
├── requirements.txt           # runtime dependencies
├── requirements-dev.txt       # + pytest
├── install_autostart.bat     # Windows: set up the "never stops" background task (recommended)
├── install_autostart_background.bat  # same, but also starts at boot with nobody logged in
├── bot_supervisor.ps1         # Windows: keeps the bot running (hidden) and restarts it
├── check_bot.bat              # Windows: "is the bot alive, and if not, why?"
├── remove_autostart.bat       # Windows: stop the bot and remove the task
├── start_bot.bat              # Windows: run the bot in a visible window (for testing)
├── .gitignore                 # keeps secrets/runtime data out of Git
└── tests/                     # offline automated tests (pytest)
```

---

## Requirements

- Python **3.10+** (3.12 tested)
- A machine that stays on (a spare PC, mini-PC or server) if you want the bot available all day
- Optional: a Gmail account with an **App Password**, a Telegram bot token, a [Green API](https://green-api.com) instance for WhatsApp

---

## Installation

```bash
git clone https://github.com/your-name/fesco-bill-bot.git
cd fesco-bill-bot

python -m venv .venv
# Windows:   .venv\Scripts\activate
# Linux/Mac: source .venv/bin/activate

pip install -r requirements.txt
playwright install chromium

cp .env.example .env            # Windows: copy .env.example .env
cp config.example.json config.json
```

Then edit `.env` and `config.json` (next section).

---

## Configuration

### `.env` - secrets and settings

`.env` holds secrets only. It is listed in `.gitignore` - **never commit it**.

| Variable                                                                            | Used by      | Meaning                                                                                                                                                      |
| ----------------------------------------------------------------------------------- | ------------ | ------------------------------------------------------------------------------------------------------------------------------------------------------------ |
| `GMAIL_ADDRESS`, `GMAIL_APP_PASSWORD`                                               | script       | Gmail sender. Use an [App Password](https://myaccount.google.com/apppasswords), not your normal password.                                                    |
| `SMTP_HOST`, `SMTP_PORT`                                                            | script       | Optional: another SMTP provider (defaults: `smtp.gmail.com`, `587`).                                                                                         |
| `TELEGRAM_BOT_TOKEN`                                                                | script + bot | Token from @BotFather. Blank = Telegram disabled in the script.                                                                                              |
| `CLOUDFLARE_WORKER_URL`                                                             | script + bot | Only if Telegram is blocked where you live: your Cloudflare Worker that proxies `api.telegram.org`. Leave out to talk to Telegram directly.                  |
| `GREEN_API_INSTANCE_ID`, `GREEN_API_TOKEN`, `GREEN_API_URL`                         | script       | WhatsApp via Green API (optional).                                                                                                                           |
| `ADMIN_ALERT_EMAIL`, `ADMIN_ALERT_TELEGRAM_CHAT_ID`                                 | script       | Where failure / "site layout changed" alerts go (optional).                                                                                                  |
| `ALLOWED_USER_ID`                                                                   | bot          | **Admins**: comma-separated Telegram user IDs with full access and no limits. The **first** ID is the primary admin (gets notices and the start-up message). |
| `RUN_USER_IDS`                                                                      | bot          | Extra people fixed in `.env` who may use `/run`.                                                                                                             |
| `GETBILLBYREF_USER_IDS`                                                             | bot          | Extra people fixed in `.env` who may use `/getbillbyref` and its helpers.                                                                                    |
| `PLAYWRIGHT_SCRIPT_PATH`                                                            | bot          | Full path to `fesco_bill_automation.py`.                                                                                                                     |
| `BILLS_RETENTION_DAYS`                                                              | script       | Delete saved bill PDFs/PNGs older than this many days (`0` = keep forever).                                                                                  |
| `BILLS_DIR`, `BILL_HISTORY_FILE`, `RUN_LOCK_FILE`                                   | bot          | Only if you moved these; they must match what the script uses.                                                                                               |
| `ACCESS_FILE`, `USAGE_FILE`, `SAVED_REFS_FILE`, `ACTIVITY_LOG_FILE`, `BOT_LOG_FILE` | bot          | Optional: change where the bot keeps its own files (default: next to `bot_listener.py`).                                                                     |

Example:

```ini
GMAIL_ADDRESS=sender@example.com
GMAIL_APP_PASSWORD=abcdabcdabcdabcd
TELEGRAM_BOT_TOKEN=123456789:AAExampleExampleExampleExampleExample
CLOUDFLARE_WORKER_URL=https://your-worker.example.workers.dev
ALLOWED_USER_ID=111111111,222222222
PLAYWRIGHT_SCRIPT_PATH=C:\Users\you\fesco-bill-bot\fesco_bill_automation.py
```

### `config.json` - bills and recipients

```json
{
  "default_whatsapp_region": "PK",
  "bills": [
    {
      "reference_no": "12345678901234",
      "label": "Home",
      "search_by": "refno",
      "recipients": {
        "emails": ["family@example.com"],
        "telegram_chat_ids": ["123456789"],
        "whatsapp_numbers": ["03001234567"]
      }
    },
    {
      "reference_no": "12345678901235",
      "label": "Shop",
      "recipients": { "emails": ["accounts@example.com"] }
    }
  ]
}
```

| Field                     | Meaning                                                                                                                                |
| ------------------------- | -------------------------------------------------------------------------------------------------------------------------------------- |
| `reference_no`            | The bill reference number (or customer ID if `search_by` is `appno`).                                                                  |
| `label`                   | A friendly name shown in messages and logs.                                                                                            |
| `search_by`               | `"refno"` (default) or `"appno"` (customer ID).                                                                                        |
| `ru_code`                 | `""` (urban, default) or `"R"` (rural) - matches the site's U/R dropdown.                                                              |
| `recipients`              | Who gets this bill - see below.                                                                                                        |
| `default_whatsapp_region` | Country code used to understand local numbers like `0300...` (e.g. `PK`). Numbers are converted to international format automatically. |
| `delivery_defaults`       | Optional, top level: change the default content per channel (see below).                                                               |

A Telegram chat ID is a number (find yours with [@userinfobot](https://t.me/userinfobot)). The person must have started a chat with your bot at least once.

### Per-person delivery choices

Every recipient is either a **plain string** (they get the default content) or an **object** that chooses exactly what that person receives.

```json
"recipients": {
  "emails": [
    "family1@example.com",
    { "address": "family2@example.com", "send": ["pdf"] },
    { "address": "paused@example.com", "enabled": false }
  ],
  "telegram_chat_ids": [
    "123456789",
    { "chat_id": "987654321", "send": ["text"] }
  ],
  "whatsapp_numbers": [
    "03001234567",
    { "number": "03211234567", "send": ["text", "pdf"] }
  ]
}
```

`send` is a list made of:

| Value  | What it means                                                 |
| ------ | ------------------------------------------------------------- |
| `text` | The bill summary message (month, units, amount, due date...). |
| `png`  | The bill image / screenshot.                                  |
| `pdf`  | The bill as a PDF.                                            |

Other things to know:

- **Defaults** (used when a person has no `send`): email = text + image + PDF, Telegram = text + image, WhatsApp = text (plus the PDF/image if you start the script with `--whatsapp-pdf` / `--whatsapp-png`).
- Change the default for everyone on a channel with a top-level block, for example `"delivery_defaults": { "telegram": ["text"] }`.
- `"enabled": false` pauses one person without deleting them.
- "Telegram only" simply means listing the person only under `telegram_chat_ids`.
- Mistakes are forgiving: an unknown option (like `"video"`) is ignored with a warning in the log and the default is used; a missing file is skipped (on WhatsApp the text is sent instead so the person still gets something).
- Aliases are accepted: `image`/`photo` = `png`, `document` = `pdf`.

---

## Setting up the Telegram bot

1. **Create the bot:** message [@BotFather](https://t.me/BotFather), send `/newbot`, and copy the token into `TELEGRAM_BOT_TOKEN`.
2. **Find your user ID:** message [@userinfobot](https://t.me/userinfobot) and put your number in `ALLOWED_USER_ID`. You are now an admin.
3. **Start a chat with your bot** (press _Start_), so it is allowed to message you.
4. **Set the script path:** `PLAYWRIGHT_SCRIPT_PATH` must be the full path to `fesco_bill_automation.py`. (If you see `unrecognized arguments: --refs`, the bot is running an _old copy_ of the script - this path points to the wrong file.)
5. **Run it:** `python bot_listener.py` to test. You should get a _"bot is online"_ message in Telegram. For everyday use on Windows, run `install_autostart.bat` instead so it keeps running by itself (see [Reliability](#reliability)).

### Optional: Cloudflare Worker (if Telegram is blocked in your country)

If your network cannot reach `api.telegram.org`, you can proxy it through a free Cloudflare Worker you control. A minimal example worker:

```js
export default {
  async fetch(request) {
    const url = new URL(request.url);
    url.hostname = "api.telegram.org";
    return fetch(new Request(url, request));
  },
};
```

Deploy it, then set `CLOUDFLARE_WORKER_URL=https://your-worker.example.workers.dev` in `.env`. Both the bot and the script then use it. **Keep your worker URL private** (anyone who knows it can send requests through it to Telegram - they still need a valid bot token).

---

## Running the automation script

```bash
python fesco_bill_automation.py                      # normal run for everything in config.json
python fesco_bill_automation.py --headed             # show the browser (debugging)
python fesco_bill_automation.py --no-email --no-whatsapp
python fesco_bill_automation.py --force-resend       # send again even if already delivered
python fesco_bill_automation.py --retry-failed-only  # resume: skip bills that already succeeded

# Manual lookup: fetch specific references, send ONLY to one Telegram chat
python fesco_bill_automation.py --refs 12345678901234,12345678901235 --reply-chat-id 123456789
```

| Option                                                       | Meaning                                                                                        |
| ------------------------------------------------------------ | ---------------------------------------------------------------------------------------------- |
| `--config PATH`                                              | Config file (default `config.json`).                                                           |
| `--state PATH`                                               | Progress file (default `state.json`).                                                          |
| `--headed`                                                   | Visible browser window.                                                                        |
| `--no-email` / `--no-telegram` / `--no-whatsapp`             | Skip a channel.                                                                                |
| `--whatsapp-text-only` / `--whatsapp-pdf` / `--whatsapp-png` | Default WhatsApp content for people without their own `send`.                                  |
| `--max-attempts N`                                           | Retries per bill (default 3).                                                                  |
| `--loader-timeout S`                                         | Seconds to let the site's loading animation finish (default 10).                               |
| `--force-resend`                                             | Resend even if this reference + month was already delivered.                                   |
| `--retry-failed-only`                                        | Skip bills that already succeeded in `state.json`.                                             |
| `--refs A,B,C`                                               | Check only these references (manual lookup mode).                                              |
| `--reply-chat-id ID`                                         | In `--refs` mode: the Telegram chat that receives the bills (and nobody else).                 |
| `--history PATH`                                             | Bill history file (default `bill_history.json`).                                               |
| `--lock-file PATH` / `--lock-wait S`                         | Run lock and how long to wait for another run (defaults: `fesco_bill_automation.lock`, 900 s). |
| `--cleanup-days N`                                           | Delete saved PDFs/PNGs older than N days (default from `BILLS_RETENTION_DAYS`, `0` = never).   |

**Manual lookup mode (`--refs`) is deliberately isolated:** it ignores the recipients in `config.json` (nobody except the requester is ever messaged), does **not** change `state.json` (so a later scheduled run still notifies the real recipients), does not send admin failure alerts for typos, and exits with code `1` if a bill could not be fetched or delivered. The PDF/PNG are still saved in `bills/` and the bill history is still recorded.

### Scheduling

**Windows (Task Scheduler):** create a task that runs monthly (or on the days new bills appear):

```
schtasks /Create /TN "FESCO Bills" /TR "C:\path\.venv\Scripts\python.exe C:\path\fesco_bill_automation.py" /SC MONTHLY /D 12 /ST 09:00
```

**Linux (cron):**

```
0 9 12 * *  cd /home/you/fesco-bill-bot && .venv/bin/python fesco_bill_automation.py
```

It is safe to schedule runs even though the bot can also start the script - the **lock file** makes sure they never overlap.

---

## Using the bot

Send `/help` to the bot at any time - it lists exactly the commands _you_ may use.

### Commands for everyone with access

| Command                              | Needs          | What it does                                                                                                                                |
| ------------------------------------ | -------------- | ------------------------------------------------------------------------------------------------------------------------------------------- |
| `/run`                               | `run` access   | Check **all** bills in `config.json` and deliver them to their configured recipients.                                                       |
| `/getbillbyref`                      | `getbillbyref` | Ask for reference number(s) and fetch them. The bill is sent **only to you**.                                                               |
| `/getbillbyref shop, 12345678901234` | `getbillbyref` | Same, in one message (numbers and/or your saved nicknames; `all` = every saved one).                                                        |
| `/lastbill <ref or nickname>`        | `getbillbyref` | Show the last saved bill image instantly (no website visit).                                                                                |
| `/history <ref or nickname>`         | `getbillbyref` | Last 6 bills: month, units, amount, due date.                                                                                               |
| `/save <nickname> <ref>`             | `getbillbyref` | Save a nickname, e.g. `/save shop 12345678901234`. Max 20 per person.                                                                       |
| `/saved` / `/unsave <nickname>`      | `getbillbyref` | List / remove your nicknames.                                                                                                               |
| `/mylimit`                           | `getbillbyref` | Your lookup limits and how much you have used.                                                                                              |
| `/status`                            | any access     | What is running, who is waiting, the last result. (Other people's jobs are shown anonymously unless you are an admin.)                      |
| `/cancel`                            | any access     | Stop your running script (admins: anyone's) and remove your waiting jobs. While the bot is asking for numbers, it just aborts the question. |
| `/help`                              | any access     | List your commands. Strangers are shown their own Telegram ID so they can ask an admin for access.                                          |

Running and waiting jobs also carry a **🛑 Cancel** button, and `/getbillbyref` shows **buttons for your saved nicknames** (plus _All saved_).

### Admin commands

Admins are the IDs in `ALLOWED_USER_ID`.

| Command                                                      | What it does                                                                                           |
| ------------------------------------------------------------ | ------------------------------------------------------------------------------------------------------ |
| `/users`                                                     | Everyone with access: commands, limits, number of allowed references.                                  |
| `/adduser <id> <run\|getbillbyref\|all> [name]`              | Give someone access (they get a message telling them).                                                 |
| `/removeuser <id> [run\|getbillbyref]`                       | Remove one command, or (no command) the person completely.                                             |
| `/setlimit <id\|default> <hour\|day> <number\|off\|default>` | Set lookup limits - see below.                                                                         |
| `/allowref <id> <ref,ref...\|any>`                           | Restrict a person to specific references (or `any` to lift it).                                        |
| `/denyref <id> <ref,ref...>`                                 | Take references away from a person's allowed list.                                                     |
| `/activity [n]`                                              | The last _n_ (default 15, max 50) activity-log lines.                                                  |
| `/health`                                                    | Uptime, script found?, queue, run lock, saved bills & disk space, last successful fetch, people count. |

### Example conversations

**A lookup**

```
You:  /getbillbyref
Bot:  Send the reference number.
      For more than one, separate with commas, e.g.
      12345678901234, 12345678901235
      [📄 shop] [📄 home] [📚 All saved] [✖️ Cancel]
You:  12345678901234
Bot:  Got 1 reference number(s): 12345678901234
Bot:  ▶️ Starting: bill check for 12345678901234   [🛑 Cancel]
Bot:  (the bill summary and image)
Bot:  Script finished successfully!
```

**Adding a trusted person with limits and a restriction**

```
Admin: /adduser 123456789 getbillbyref Sam
Bot:   ✅ 123456789 (Sam) can now use: getbillbyref
Admin: /setlimit 123456789 hour 3
Admin: /setlimit 123456789 day 10
Admin: /allowref 123456789 12345678901234
Bot:   ✅ 123456789 may now look up only: 12345678901234
```

**Hitting a limit**

```
Sam:  /getbillbyref 12345678901234
Bot:  ⛔ Lookup limit reached: 3 of 3 used in the last hour.
      You can look up 1 more in about 41m 5s.
```

---

## Access control, limits and restrictions

### Who can do what

There are two places people can be configured:

| Where                                                               | Changes need      | Good for                                |
| ------------------------------------------------------------------- | ----------------- | --------------------------------------- |
| `.env` (`ALLOWED_USER_ID`, `RUN_USER_IDS`, `GETBILLBYREF_USER_IDS`) | restart           | You (admin) and a few permanent people. |
| `access.json`, edited by the bot (`/adduser`, `/removeuser`, ...)   | nothing - instant | Everyone else.                          |

The two are combined. **Admins** (only from `.env`) can use every command and are never limited or restricted. Everyone else can use exactly the commands they were given. A person can have `run`, `getbillbyref`, or both.

### Lookup limits (per hour / per day, per person)

- Limits apply to **lookups**, counted **per reference number** (asking for 3 references = 3 lookups).
- Windows are **rolling**: "per hour" means _the last 60 minutes_, "per day" means _the last 24 hours_. They can be combined; both must have room.
- Resolution order: a person's own limit → otherwise the **default** limit → otherwise unlimited.

```
/setlimit 123456789 hour 5        # this person: 5 lookups per hour
/setlimit 123456789 day 20        # ...and 20 per day
/setlimit 123456789 hour off      # explicitly unlimited per hour
/setlimit 123456789 hour default  # go back to the default
/setlimit default day 30          # everybody without their own limit: 30 per day
/setlimit default hour off        # remove the default hour limit
```

- A request bigger than the limit itself (e.g. 4 references when the limit is 3/hour) is refused with an explanation, and the refusal tells people **when they can try again**.
- If a waiting job is cancelled before it ever starts, the lookups are **given back**.
- Usage is saved in `usage.json`, so restarting the bot does not reset anyone's count.
- `/run`, `/lastbill` and `/history` are not rate-limited (the last two never contact the website).

### Restricting someone to specific references

```
/allowref 123456789 12345678901234,12345678901235   # only these two (turns the restriction ON)
/denyref  123456789 12345678901235                  # remove one
/allowref 123456789 any                             # lift the restriction
```

A restricted person is blocked from `/getbillbyref`, `/lastbill`, `/history`, saving nicknames for other references, and their buttons only show allowed references. A rejected request does **not** use up any of their limit. An empty allowed-list means "nothing allowed yet".

### Activity log

Every command, every refusal (`DENIED`, `RATE_LIMITED`, `BLOCKED_REF`), every lookup, every admin action and every job start/finish is written to `activity.log` (rotating, 1 MB x 5 files):

```
2026-10-03 16:04:40 | lookup | user=123456789 (Sam) | refs=12345678901234
2026-10-03 16:04:41 | RATE_LIMITED | user=123456789 (Sam) | refs=12345678901235
2026-10-03 16:05:02 | ADMIN setlimit | user=111111111 (Admin) | target=123456789 | window=hour | value=5
```

View the latest lines in Telegram with `/activity`.

---

## Reliability

| Feature                               | How it works                                                                                                                                                                                                                                                              |
| ------------------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| **Start automatically, stay running** | Windows: run `install_autostart.bat` once - see [Windows: keep the bot running no matter what](#windows-keep-the-bot-running-no-matter-what).                                                                                                                             |
| **One copy only**                     | The bot takes a lock owned by its own process when it starts. A second copy exits immediately (exit code 4) instead of fighting the first over Telegram. The lock vanishes by itself if the bot crashes or is killed, so it can never get "stuck".                        |
| **Same Python everywhere**            | The bot starts the script with the very same Python it runs on (not whatever `python` happens to be on `PATH`), so virtual environments and Task Scheduler's different environment cannot break it.                                                                       |
| **Self-healing polling**              | The bot's network loop also restarts itself with an increasing delay if anything unexpected happens, and uncaught errors are written to `bot.log`.                                                                                                                        |
| **"Bot is online" message**           | After every start the primary admin gets a Telegram message - including any problems found (e.g. script path missing). If Telegram is unreachable it retries 3 times.                                                                                                     |
| **`/health`**                         | One message with uptime, script status, queue, lock, disk space and last successful fetch.                                                                                                                                                                                |
| **No overlapping runs**               | A lock file (`fesco_bill_automation.lock`) is shared by scheduled, bot and manual runs. A second run waits (default 15 minutes) then gives up with exit code 3. A lock left by a crashed or killed run (dead process, or older than 3 hours) is taken over automatically. |
| **Site-change alert**                 | If the form or bill fields can no longer be found, the script sends **one** alert ("site layout may have changed") to the admin instead of one failure per bill. Ordinary failures (e.g. a wrong reference number) still produce a normal per-bill alert.                 |
| **Rotating logs**                     | `fesco_bill_automation.log`, `bot.log` and `activity.log` rotate at ~1 MB and keep 5 old copies.                                                                                                                                                                          |
| **Old file cleanup**                  | Set `BILLS_RETENTION_DAYS` (or `--cleanup-days`) to delete saved PDFs/PNGs older than N days. Off by default.                                                                                                                                                             |
| **Job timeout**                       | A bot-started script that runs longer than 1 hour is stopped, so one stuck browser cannot block the queue.                                                                                                                                                                |
| **Safe cancel**                       | `/cancel` stops the script **and** the browser it opened.                                                                                                                                                                                                                 |
| **Network hiccups**                   | Status messages are best-effort: a failed "Starting..." message never prevents the job from running.                                                                                                                                                                      |

#### Windows: keep the bot running no matter what

Windows Task Scheduler's **default settings are not meant for always-on programs** - they stop a task on battery power, after a time limit, or when the PC goes idle, and a bot started in a console window dies as soon as that window is closed. The included installer fixes all of that.

**Set it up (once):**

1. Make sure `.env` is filled in and `python bot_listener.py` works when you run it by hand.
2. Delete any scheduled task you created manually for the bot.
3. Double-click **`install_autostart.bat`**. (On a laptop use `install_autostart.bat -KeepAwake` from a command prompt, or tick the power options below.)
4. Wait a minute - you should get the _"bot is online"_ message in Telegram. Check any time with **`check_bot.bat`**.

**What it sets up, and why:**

| Setting                                                                                        | Why it matters                                                                                              |
| ---------------------------------------------------------------------------------------------- | ----------------------------------------------------------------------------------------------------------- |
| Runs `bot_supervisor.ps1` in a **hidden** window, started **in the project folder**            | Nothing to close by accident; relative paths (`bills/`, `.env`) work.                                       |
| Supervisor **restarts the bot** whenever it exits (backs off up to 5 min if it keeps crashing) | Covers crashes of any kind.                                                                                 |
| Triggers: **at logon** + a **watchdog every 5 minutes**                                        | If anything ever kills it, it is back within 5 minutes. The watchdog does nothing while the bot is healthy. |
| _Stop if running on battery_ = **off**, _Start on battery_ = **allowed**                       | The #1 reason laptop tasks silently stop.                                                                   |
| _Stop the task if it runs longer than_ = **never**                                             | Windows' default is 3 days.                                                                                 |
| _If already running_ = **do not start a new one** + the bot's single-copy lock                 | No duplicates.                                                                                              |
| _Restart on failure_ = every minute, up to 999 times                                           | Extra safety net from Windows itself.                                                                       |
| _Run as soon as possible after a missed start_                                                 | Catches up after the PC was off.                                                                            |

**Want it to run even when nobody is logged in** (after a reboot / power cut)? Right-click **`install_autostart_background.bat`** -> _Run as administrator_. It asks for your Windows password once (Windows stores it for the task) and adds an _at boot_ trigger. Run it again if you ever change your Windows password.

**Sleep is the one thing a task cannot override.** If the PC goes to sleep, nothing runs. While plugged in, turn sleep off:

```
powercfg /change standby-timeout-ac 0
powercfg /change hibernate-timeout-ac 0
```

(or run the installer with `-KeepAwake`). On a laptop that you close, also set _Control Panel -> Power Options -> "Choose what closing the lid does"_ to **Do nothing** when plugged in.

**Handy commands**

| What                          | How                                                                                                               |
| ----------------------------- | ----------------------------------------------------------------------------------------------------------------- |
| Is it alive? Why did it stop? | `check_bot.bat` (task state, last result code, running processes, the last log lines, power settings)             |
| Stop it completely            | `remove_autostart.bat` (stops the processes and removes the task; run `install_autostart.bat` to set up again)    |
| Restart it                    | In Task Scheduler right-click _FESCO Bill Bot_ -> _End_, then _Run_                                               |
| Pause for a while             | Task Scheduler -> right-click the task -> _Disable_ (End alone is not enough - the watchdog would start it again) |

Logs: `bot_supervisor.log` (starts/exits with times and exit codes), `bot.log` (the bot itself), `bot_console_err.log` (anything printed to the error stream, e.g. a crash on start-up).

> Note: the PowerShell scripts were written and reviewed carefully, but they can only be fully verified on a real Windows machine. If anything in `install_autostart.bat` shows a red error, send me the message.

**Linux (systemd) example** for running the bot as a service:

```ini
# /etc/systemd/system/fesco-bot.service
[Unit]
Description=FESCO bill Telegram bot
After=network-online.target

[Service]
WorkingDirectory=/home/you/fesco-bill-bot
ExecStart=/home/you/fesco-bill-bot/.venv/bin/python bot_listener.py
Restart=always
RestartSec=10
User=you

[Install]
WantedBy=multi-user.target
```

Then `sudo systemctl enable --now fesco-bot`. (The bot starts the script with the same Python it is running on, so using the virtual environment's Python in `ExecStart` is all that is needed.)

---

## Files created at runtime

All of these are in `.gitignore`.

| File / folder                                                   | Created by           | Contains                                                                                            |
| --------------------------------------------------------------- | -------------------- | --------------------------------------------------------------------------------------------------- |
| `bills/`                                                        | script               | `<reference>.pdf` and `<reference>.png` - the latest saved copy of each bill.                       |
| `state.json`                                                    | script               | What has been delivered (so nothing is sent twice) and recent attempts.                             |
| `bill_history.json`                                             | script               | Per reference: month, units, amount, due date (last 24 months). Read by `/history`.                 |
| `fesco_bill_automation.log*`                                    | script               | Script log (rotating).                                                                              |
| `fesco_bill_automation.lock`                                    | script               | Present only while a run is in progress.                                                            |
| `access.json`                                                   | bot                  | People added from Telegram, their limits and allowed references, and the default limit.             |
| `usage.json`                                                    | bot                  | Recent lookups per person (for the rate limits).                                                    |
| `saved_refs.json`                                               | bot                  | Each person's saved nicknames.                                                                      |
| `activity.log*`                                                 | bot                  | The activity log (rotating).                                                                        |
| `bot.log*`                                                      | bot                  | Bot log (rotating).                                                                                 |
| `bot.lock`                                                      | bot                  | The "only one copy" lock (the file itself is harmless; the lock lives in the running process).      |
| `bot_supervisor.log*`, `bot_console.log`, `bot_console_err.log` | `bot_supervisor.ps1` | Start/exit history of the bot with exit codes, and its console output / errors from the last start. |

Note: the bot reads `bills/` and `bill_history.json` from the folder it is **started in**, which must be the same folder the script runs from. If you moved them, set `BILLS_DIR` / `BILL_HISTORY_FILE` in `.env`.

---

## Testing

The test-suite is fully **offline** - it uses a fake browser, a fake Telegram and a fake mail server, so it never touches the real FESCO website or your accounts.

```bash
pip install -r requirements-dev.txt
pytest
```

It covers config parsing and per-person delivery, every sender, full runs through `main()`, manual-lookup isolation, the lock file (including two real processes), cleanup, site-layout alerts, access control, all admin commands, rate-limit windows, reference restrictions, the activity log, the job queue, cancelling real subprocesses, timeouts and start-up/health behaviour.

---

## Troubleshooting

| Symptom                                         | Likely cause and fix                                                                                                                                                                                                                                                                                                                                                                                                                                                                          |
| ----------------------------------------------- | --------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `unrecognized arguments: --refs ...`            | The bot is running an **old copy** of the script. Check `PLAYWRIGHT_SCRIPT_PATH` in `.env`, replace that file, and restart the bot. Verify with `python fesco_bill_automation.py -h` (it should list `--refs`).                                                                                                                                                                                                                                                                               |
| **The bot runs for a few minutes, then stops**  | Run `check_bot.bat` - it shows why. The usual causes: the task was created with Windows' default settings (_stop on battery_, a time limit, or a closed console window) - use `install_autostart.bat`, which sets all of these correctly; or the PC went to sleep - see [keep the bot running](#windows-keep-the-bot-running-no-matter-what). Last result code `0x41306` means Windows itself stopped the task; the lines in `bot_supervisor.log` and `bot.log` show whether the bot crashed. |
| `bot_supervisor.log` shows _exited with code 4_ | Another copy of the bot is already running (that is fine - the second copy exits on purpose). Use `check_bot.bat` to see which one.                                                                                                                                                                                                                                                                                                                                                           |
| Telegram error _409 Conflict_ in `bot.log`      | Two copies of the bot are polling the same token (for example one on another PC). Stop the other one.                                                                                                                                                                                                                                                                                                                                                                                         |
| Bot says _Unauthorized access._                 | The person is not in `ALLOWED_USER_ID` / `RUN_USER_IDS` / `GETBILLBYREF_USER_IDS` and has not been added with `/adduser`. They can send `/help` to see their Telegram ID.                                                                                                                                                                                                                                                                                                                     |
| No messages arrive from the bot                 | Telegram may be blocked - set up the Cloudflare Worker and `CLOUDFLARE_WORKER_URL`. Also make sure you pressed _Start_ in the bot chat.                                                                                                                                                                                                                                                                                                                                                       |
| _Another run is still in progress_              | A scheduled or manual run holds the lock. It clears itself when that run ends; if a run crashed, the next run removes the stale lock. `/health` shows whether the lock is present.                                                                                                                                                                                                                                                                                                            |
| _Site layout may have changed_ alert            | The FESCO page was redesigned. The selectors in `fesco_bill_automation.py` (the `#searchTextBox`, `#btnSearch`... ids, `SCRAPE_JS`, `wait_for_bill_fields`) need updating.                                                                                                                                                                                                                                                                                                                    |
| _No bill rendered for reference ..._            | Wrong reference number, or the site returned an error. Check the number and try again later.                                                                                                                                                                                                                                                                                                                                                                                                  |
| `/lastbill` says _No saved bill image_          | That reference has not been fetched yet, or the bot is started from a different folder than the script (see `BILLS_DIR`).                                                                                                                                                                                                                                                                                                                                                                     |
| Emails fail to send                             | Use a Gmail **App Password** (needs 2-Step Verification), not your normal password.                                                                                                                                                                                                                                                                                                                                                                                                           |
| WhatsApp number skipped                         | The number could not be parsed. Use international format (`923001234567`) or set `default_whatsapp_region`.                                                                                                                                                                                                                                                                                                                                                                                   |
| Limit seems wrong                               | Limits are rolling windows counted **per reference**. See `/mylimit` for the person's current usage.                                                                                                                                                                                                                                                                                                                                                                                          |

---

## Security and privacy

- **Never commit** `.env`, `config.json`, `access.json`, `usage.json`, `saved_refs.json`, `state.json`, `bill_history.json`, `bills/` or any logs - they contain tokens, names, addresses, reference numbers and bills. The included `.gitignore` already excludes them.
- Bills contain personal data (name, address, consumption). Lookups made through the bot go **only** to the person who asked; the admin receives a short notice (who, which reference, success/failure) but **never the bill itself**. Turn the notice off with `ADMIN_NOTICE_FOR_GETBILLBYREF = False` in `bot_listener.py`.
- Reference numbers are validated as digits only before they are passed to the script, so chat input can never inject command-line options.
- Anyone with `getbillbyref` access can look up **any** reference number they know (that is how the FESCO portal works) unless you restrict them with `/allowref`. `/history` and `/lastbill` expose data for references that have been fetched before - restrict people if that matters.
- Keep your bot token and (if you use one) your Cloudflare Worker URL private. If a token ever leaks, revoke it with @BotFather (`/revoke`).

---

## FAQ

**Can two people use `/getbillbyref` at the same time?** Yes - requests are queued and run one after another. Each person is told when they are queued, can cancel with the button, and receives their own bill.

**Does `/getbillbyref` change what my family receives?** No. It never uses the recipients in `config.json` and never changes `state.json`, so the next scheduled run behaves exactly as if the lookup had not happened.

**What if two bills in `config.json` share a reference number?** A manual lookup fetches it once. A normal run processes each entry with its own recipients.

**Where can I change the default content someone receives?** In `config.json`: per person with `send`, or for a whole channel with `delivery_defaults`. See [Per-person delivery choices](#per-person-delivery-choices).

**How do I give someone access for only a few lookups per day?** `/adduser <id> getbillbyref`, then `/setlimit <id> day 5`. Add `/allowref <id> <their reference>` to also restrict _which_ bills they can see.

**Can I limit `/run` too?** Not currently: `/run` is a permission only (give it to as few people as needed).

---

_Made for personal/family use. Add your own license file before publishing._
