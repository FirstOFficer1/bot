# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

A VK (ВКонтакте) chatbot that helps university students: class schedules, notes,
reminders, deadlines, and push notifications before classes start. It ships with a
Flask web admin panel for managing schedules/users, and a legacy Telegram bot.

The codebase and all comments/UI strings are in **Russian** — match that language when
editing user-facing text and docstrings.

## Processes (see `Procfile`)

Three independent processes, each its own entry point:

| Process    | Command               | Stack                  | Notes |
|------------|-----------------------|------------------------|-------|
| `vk`       | `python vk_bot.py`    | vkbottle (async)       | The main, actively-developed bot. `vk_bot.py` just calls `vkbot.bot.main`; `python -m vkbot` is equivalent. |
| `web`      | `python web_panel.py` | Flask                  | Admin panel. `--port 8080` to override the default 5000. |
| `telegram` | `python bot.py`       | aiogram (async)        | **Legacy monolith** — a 70k+-line single file predating the `vkbot/` refactor. Avoid extending it; new work goes in `vkbot/`. |

There is no test suite, linter, or build step configured.

## Setup

```bash
pip install -r requirements.txt      # full env (includes Telegram/aiogram, Google, OpenAI deps)
pip install -r vk_requirements.txt   # minimal: just the VK bot + web panel
```

Required environment variables (`.env`, loaded via `python-dotenv`):
- `VK_TOKEN` — VK community token (bot won't start without it)
- `ADMIN_ID` — VK user_id of the owner / super-admin. Note: the bot (`vkbot/config.py`)
  reads **only** `ADMIN_ID`, while the panel (`web_panel.py`) accepts either `ADMIN_VK_ID`
  or `ADMIN_ID`. Set `ADMIN_ID` so both agree.
- `PANEL_SECRET` — Flask session secret; **web panel refuses to start if unset**
- `PANEL_BASE_URL` — public panel URL; if it starts with `https://`, secure/SameSite=None cookies are enabled (needed for the VK Mini App)
- `UPLOAD_API_TOKEN` — Bearer token for the schedule-upload REST API
- `VK_APP_ID` / `VK_APP_SECRET` — for VK Mini App launch-signature validation

## Architecture (`vkbot/` package)

The VK bot is a domain-sliced package. `vkbot/bot.py::build_bot()` wires everything:
`db.init()` → `store.load_all()` → `repo.reload()` → register handlers; `main()` then
adds the background workers and calls `bot.run_forever()`.

- **`config.py`** — env, DB paths, timings, `MSK` timezone, `now_msk()`. All times in the
  app are MSK-naive datetimes compared against strings stored in the DB.
- **`db.py`** — SQLite access via a `connect()` context manager (WAL, busy_timeout,
  auto-commit). `init()` is idempotent: it owns the full schema, indexes, and inline
  migrations (e.g. `ALTER TABLE` guarded by a `PRAGMA table_info` check). Add new tables/
  columns here, not in ad-hoc scripts.
- **`state.py`** — `store`, a singleton `StateStore` (in-memory dict + persisted to the
  `user_states` table). Drives multi-step conversational dialogs. `store.patch(uid, **kw)`
  merges state; `store.pop(uid)` ends a dialog.
- **`handlers/`** — one module per domain (notes, reminders, deadlines, subscriptions,
  schedule, feedback, panel_login, common). Each exposes
  `async def try_handle(bot, message, state, text, uid) -> bool` returning `True` when it
  consumes the message. `handlers/__init__.py::register` runs an **ordered `_PIPELINE`** —
  the first handler to return `True` wins, so **order matters** (`common.try_intro`/
  `try_category` run first, `common.fallback` last).
- **`models/`** — thin CRUD-per-domain modules over SQLite. No ORM.
- **`workers/`** — async background loops added in `main()` (`reminders`, `deadlines`,
  `classes`, `schedule_reloader`). Each is a `while True: await asyncio.sleep(POLL_SEC)`
  loop driven by the `*_POLL_SEC` constants in `config.py`.
- **`schedule/`** — `repo` is a thread-safe singleton caching courses/directions and the
  short-label↔full-name maps; `get_day()` queries the schedule DB. `loader.py` handles
  Excel import (preview/commit/rollback/versioning), delegating the actual parse to the
  root-level `import_excel.py`.
- **`sender.py`** vs **`notifier.py`** — two different send paths, don't conflate them:
  `sender.send()` is async (vkbottle, used inside the bot); `notifier` is synchronous
  `requests`-based `messages.send` used by the web panel and cron-style broadcasts. Both
  auto-disable a user's subscriptions on VK "user unavailable" codes (901/902/917).

### Two SQLite databases — note the filename quirk

- `notes.db` (`config.NOTES_DB`) — app data: states, notes, reminders, subscriptions,
  deadlines, prefs, panel users/codes/tokens, audit log, seen users.
- **`sсhedule.db`** (`config.SCHEDULE_DB`) — schedule rows. ⚠️ The `с` in the filename is a
  **Cyrillic U+0441**, not Latin `c`. This is deliberate (matches the production file) —
  never "fix" it. Always reference the path via `config.SCHEDULE_DB`.

DB files (`*.db`) are gitignored.

### Schedule hot-reload (cross-process)

The web panel and the bot are separate processes. After the panel commits a new schedule,
it `touch`es `config.SCHEDULE_RELOAD_MARKER` (`.schedule_reload`). The bot's
`schedule_reloader` worker polls that file's mtime and calls `repo.reload()` when it
changes. Use `vkbot.schedule.repo.signal_reload()` to trigger this, rather than reaching
into the bot's in-memory cache directly.

## Web panel (`web_panel.py`)

Single-file Flask app, role model = `owner` (env `ADMIN_ID`) / `admin` (`panel_users`
table) / `user` (any logged-in VK user, sees only `/me`). Auth is via a one-time code the
user requests from the bot with `/login` — no passwords. CSRF protection is on globally
(`CSRFProtect`); the `/api/schedule/*` JSON endpoints are explicitly `@csrf.exempt` and use
the `UPLOAD_API_TOKEN` Bearer header instead. Security headers (CSP etc.) are set in an
`after_request` hook. Notable routes: schedule upload/commit/rollback, `/admin/*` (users,
admins, conflicts, diff, audit, broadcast), `/calendar.ics`, `/me`.
