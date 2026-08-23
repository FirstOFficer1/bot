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
| `web`      | `python web_panel.py` | Flask + waitress       | Admin panel. `--port 8080` overrides the default 5000; `--dev` falls back to the Werkzeug dev server. **Must run as exactly one process** — see below. |
| `telegram` | `python bot.py`       | aiogram (async)        | **Legacy monolith** — a 70k+-line single file predating the `vkbot/` refactor. Avoid extending it; new work goes in `vkbot/`. |

## Tests

```bash
pip install -r requirements-dev.txt
playwright install chromium     # once, for the browser smoke check

pytest -q                                        # whole suite; DBs go to a temp dir
pytest tests/test_panel.py -q                    # one file
pytest tests/test_panel.py::test_code_is_single_use -q   # one test
pytest -q -k "week or parity"                    # by name
```

Deps live in the project venv, and on Windows a bare `python`/`pytest` resolves to the
system interpreter instead (`ModuleNotFoundError: flask`) — prefix with
`.venv/Scripts/python.exe -m` (POSIX: `.venv/bin/python -m`) when the venv isn't active.

`tests/conftest.py` redirects every DB to a temp directory **before** importing
`vkbot` — the paths are module-level constants in `vkbot/config.py`, so env has to
be set first; it also fills in `VK_TOKEN=""` (so `vk_names`/`notifier` never hit the
network) and a session-wide `db.init()` plus a per-test table wipe. Adding a table
means adding it to that wipe list, or tests leak rows into each other.

`pytest.ini` also sets `pythonpath = .` — without it only `python -m pytest` works
(the `-m` form puts the CWD on `sys.path`), and the bare `pytest` that CI and the
README use dies with `ModuleNotFoundError: No module named 'vkbot'`.

`pytest.ini`: `asyncio_mode = strict` — async tests need an explicit
`@pytest.mark.asyncio`; `DeprecationWarning` from `vkbot.*` and any `ResourceWarning`
are **errors**, so a deprecated call or a leaked sqlite handle fails the suite instead
of printing a warning. Two things make that work and are easy to undo by accident: the
catch-all `default` must stay **first** (the last matching filter wins, so a trailing
`default` silently disables every escalation above it), and `ResourceWarning` needs
`error::pytest.PytestUnraisableExceptionWarning` alongside it — the warning is raised
during GC, and pytest re-wraps it in its own class. Lint with `ruff check .` — the config (`ruff.toml`) selects only rules that catch real
defects (pyflakes, import/syntax errors, pylint errors), excludes the legacy `bot.py`,
and is what CI runs. Widen the rule set one rule at a time, together with the fixes it
demands.

The end-to-end check needs a live panel and a `.xlsx` to upload:

```bash
python tools/make_demo_schedule.py            # → demo_schedule.xlsx (gitignored)
python web_panel.py &                         # smoke drives a real server, not a test client
python tools/smoke_panel.py --shots shots/    # also: --base-url, --headed, --excel
```

It walks the whole admin path (OTP login → upload → preview → commit → schedule →
audit → logout) and exits non-zero on the first broken step.

`tools/check_live.py <code>` is the read-only counterpart for a **live** panel: it logs
in with a real one-time code, checks page layout at phone width, pair ordering on real
data and JS errors — but never writes. Never point `smoke_panel.py` at production: it
uploads a demo schedule and would overwrite the real one. The login code is single-use,
so a desktop and a mobile pass need two codes (`--mode desktop` / `--mode mobile`).

`tools/check_miniapp.py` checks the thing only a browser can see: it serves a fake
parent page on `vk.ru` and on `vk.com`, embeds the live panel in an iframe and waits
for `VKWebAppInit` over `postMessage`. That is exactly what VK does before deciding
to show "Приложение не инициализировано". No login needed, read-only, and it catches
CSP/framing breakage that unit tests cannot see because it lives in nginx.

## Setup

```bash
pip install -r requirements.txt          # runtime: VK bot + panel (this is what prod needs)
pip install -r requirements-dev.txt      # + pytest, pytest-asyncio, playwright
pip install -r requirements-legacy.txt   # + aiogram/openai, only for the legacy Telegram bot
```

`vk_requirements.txt` is kept as a `-r requirements.txt` shim so old deploy scripts
don't break.

Required environment variables (`.env`, loaded via `python-dotenv`):
- `VK_TOKEN` — VK community token (bot won't start without it)
- `ADMIN_ID` — VK user_id of the owner / super-admin. Note: the bot (`vkbot/config.py`)
  reads **only** `ADMIN_ID`, while the panel (`web_panel.py`) accepts either `ADMIN_VK_ID`
  or `ADMIN_ID`. Set `ADMIN_ID` so both agree.
- `PANEL_SECRET` — Flask session secret; **web panel refuses to start if unset**
- `PANEL_BASE_URL` — public panel URL; if it starts with `https://`, secure/SameSite=None cookies and HSTS are enabled (needed for the VK Mini App)
- `PANEL_TRUSTED_PROXIES` — how many real proxies sit in front of the app (nginx → `1`,
  direct → `0`). Gates `ProxyFix`: at `0` the app ignores `X-Forwarded-For`/`X-Real-IP`,
  otherwise those headers could be spoofed to bypass the per-IP `/login/code` limit
- `PANEL_MAX_UPLOAD_MB` — request size cap (default 16); keep nginx's
  `client_max_body_size` in sync
- `PANEL_HOST` / `PANEL_PORT` — bind address (defaults `127.0.0.1:5000`); the CLI
  `--port` wins over `PANEL_PORT`. Keep the host loopback-only when nginx fronts it
- `DATA_DIR` / `NOTES_DB` / `SCHEDULE_DB` / `SCHEDULE_VERSIONS_DIR` /
  `SCHEDULE_RELOAD_MARKER` — optional path overrides, used by the tests and handy for
  putting data on a volume. Bot and panel must agree on all of them, or the panel
  writes a schedule the bot never sees
- `UPLOAD_API_TOKEN` — Bearer token for the schedule-upload REST API
- `VK_APP_ID` / `VK_APP_SECRET` — for VK Mini App launch-signature validation

## Architecture (`vkbot/` package)

The VK bot is a domain-sliced package. `vkbot/bot.py::build_bot()` wires everything:
`db.init()` → `store.load_all()` → `repo.reload()` → register handlers; `main()` then
adds the background workers and calls `bot.run_forever()`.

- **`config.py`** — env, DB paths, timings, `MSK` timezone, `now_msk()`. All times in the
  app are MSK-naive datetimes compared against strings stored in the DB. DB paths are
  absolute (rooted at the project dir), overridable via env — never relative to cwd,
  or a service started elsewhere silently opens a second empty database.
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
- **`models/`** — thin CRUD-per-domain modules over SQLite. No ORM. Deletes take the
  owner's `uid` and scope on it (`WHERE id=? AND user_id=?`) — don't add a delete that
  trusts a bare row id.
- **`workers/`** — async background loops added in `main()` (`reminders`, `deadlines`,
  `classes`, `schedule_reloader`). Each is a `while True: await asyncio.sleep(POLL_SEC)`
  loop driven by the `*_POLL_SEC` constants in `config.py`. Two rules every worker
  follows: the whole tick body sits in a `try/except Exception` (an unguarded error
  kills the coroutine for good — the process keeps answering messages while
  notifications silently stop), and a successful tick ends with
  `heartbeats.mark(...)`, which is what `/healthz` reads. A new worker needs a name
  in `models/heartbeats.py::ALL` and a limit in `web_panel._worker_limits()`, or it
  is invisible to monitoring.
- **`schedule/`** — `repo` is a thread-safe singleton caching courses/directions and the
  short-label↔full-name maps; `get_day()` queries the schedule DB. `loader.py` handles
  Excel import (preview/commit/rollback/versioning), delegating the actual parse to the
  root-level `import_excel.py`.
  ⚠️ `__init__.py` exports the **instance** as `repo`, which shadows the `repo`
  *module* of the same name. Inside the package import from the module explicitly
  (`from .repo import repo, signal_reload`) — `from . import repo` hands you the
  instance and blows up on `signal_reload()`. That bug silently broke every schedule
  commit at the last step.
- **`sender.py`** vs **`notifier.py`** — two different send paths, don't conflate them:
  `sender.send()` is async (vkbottle, used inside the bot); `notifier` is synchronous
  `requests`-based `messages.send` used by the web panel and cron-style broadcasts. Both
  auto-disable a user's subscriptions on VK "user unavailable" codes (901/902/917).

### Week parity

Parity lives in the schedule table's **`week` column** (`''` = every week, `'чёт'`,
`'нечет'`), written by `import_excel.py` from the `*`/`**` markers in the Excel cell.
Anything that filters classes by week must use that column — `repo.get_day()` and
`workers/classes.py` both do. The old `[чёт] Subject` name prefix is a legacy
Telegram-bot convention the current importer never produces; filtering on it silently
matches nothing, which is how class notifications ended up firing on both weeks.
(`workers/classes.py` still strips that prefix on display and excludes the other
week's legacy rows, for databases carried over from the Telegram era.)

Which parity *today* is comes from `schedule/week.py::current_week_type()`, derived
from the **ISO week number** (`нечет` when the ISO week is even) — not from a semester
start date. If the university's чёт/нечет is inverted relative to ours, flip it there
and nowhere else; every caller goes through that one function.

### Class-notification dedup

`workers/classes.py` records each push in `sent_class_notifications` keyed on
`(user_id, class_key, class_date, class_time)`. `class_key` comes from
`models/sent_notifs.py::class_key()` — a normalized `course|direction|subject|room`
fingerprint, deliberately **not** the schedule row's `rowid`: a re-import rewrites the
table and hands out fresh rowids, so the old key re-sent every push when a schedule
was uploaded mid-day. Anything that changes how classes are identified must keep this
key stable across re-imports.

### Housekeeping

`workers/deadlines.py::_housekeeping()` runs hourly (`HOUSEKEEPING_EVERY_SEC`) and
prunes login codes (`PANEL_CODES_CLEANUP_DAYS`) and the audit log (`AUDIT_KEEP_DAYS`);
`/healthz` prunes the in-memory rate-limit counters, which live in the panel process
and nowhere else. Retention functions here have a habit of being written and never
called — if you add one, wire it into one of those two places in the same commit.

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

### sqlite3 connections

`with sqlite3.connect(...) as conn:` manages the **transaction only** — it does not
close the connection. The panel's `_notes_conn()`/`_sched_conn()` return a `_Conn`
subclass whose `__exit__` also closes; elsewhere use `contextlib.closing` or
`vkbot.db.connect()`. A leaked handle isn't just a slow leak: on Windows it blocks
deleting the file, which broke the upload preview outright. A trailing `conn.close()`
as the last line of a `try:` doesn't count — the panel's `except Exception: pass`
blocks swallow the error and skip it; close in `finally`. The suite fails on a new
leak (see the `pytest.ini` note above).

## Web panel (`web_panel.py`)

Single-file Flask app, role model = `owner` / `admin` (`panel_users` table) / `user`
(any logged-in VK user, sees only `/me`).

**Ownership has two tiers and the distinction is the whole security argument.** Env
owners (`ADMIN_ID` / `ADMIN_VK_ID` / `ADMIN_VK_IDS`) are an anchor the web can never
remove — only editing `.env` and restarting does. Panel owners (`panel_users.role =
'owner'`) are granted and revoked in the UI. So a hijacked session cannot demote the
real owner; the worst it does is add a co-owner, which is audited, announced to every
owner over VK, and undone with one button. `_is_owner()` is the union of both sets;
never compare against `OWNER_VK_IDS` directly — that skips panel owners.

Owner-level routes (`/admin/owner/grant`, `/admin/owner/revoke`) require **step-up**:
a fresh one-time code from the bot on top of the session, so a stolen cookie is not
enough. Rails enforced there: env owners are never revocable, the last owner cannot be
revoked, revoking ownership demotes to `admin` rather than removing access, and the
panel refuses to start with no owner at all (`_assert_owner_exists`). Auth is via a one-time code the
user requests from the bot with `/login` — no passwords. CSRF protection is on globally
(`CSRFProtect`); the `/api/schedule/*` JSON endpoints are explicitly `@csrf.exempt` and use
the `UPLOAD_API_TOKEN` Bearer header instead. Security headers (CSP etc.) are set in an
`after_request` hook. Notable routes: schedule upload/commit/rollback, `/admin/*` (users,
admins, conflicts, diff, audit, broadcast), `/calendar.ics`, `/me`. `/logout` is POST-only
(a GET logout is CSRF-able). Any `next=` form parameter goes through `_safe_next()`, which
accepts local paths only.

It also calls `vkbot.db.init()` at import: the panel may be started before the bot has
ever run, and it needs `panel_login_codes` / `panel_remember_tokens` for its own login.

**`/api/schedule/*` must mirror the UI routes.** Both change the same schedule, so a
change to one path belongs in the other: write an `audit.log` entry (the API is
otherwise a way to swap the schedule leaving no trace in `/admin/audit`), call
`_ics_cache_clear()`, call `_sync_legacy_schedule_db()`, and return a generic error
message — `str(e)` leaks paths and SQL to the caller.

**Legacy `s.db`.** The panel re-imports every schedule change into `s.db`, the old
Telegram bot's database, so the two don't drift; `_sync_legacy_schedule_db()` is the
single place that does it, and every mutating path (panel/API × upload/rollback) calls
it. The path follows `DATA_DIR` (override: `LEGACY_SCHEDULE_DB`) — the legacy `bot.py`
itself still opens a bare relative `"s.db"`, so it only agrees when started from the
project root.

**Single-process requirement.** `_PENDING_UPLOADS`, `_LAST_BROADCAST` and `_ICS_CACHE`
are in-memory module state. A second worker breaks schedule uploads ("unknown or
expired token") and scrambles broadcast progress. Move that state into the DB before
scaling out.

### Consent gate (152-ФЗ)

`_require_consent()` is a `before_request` that redirects any logged-in user to
`/consent` until they have accepted the current consent text. The version lives in
`vkbot/models/consents.py::VERSION` — bump it when the wording changes and everyone
re-accepts, because otherwise people count as having agreed to a document they never
saw. Only `_CONSENT_FREE_ENDPOINTS` pass through: login/logout, the legal pages, the
consent screen itself, `/healthz`, static, and `me_delete_all` — refusing and wiping
yourself must not require signing anything.

The bot has the same gate: `handlers/consent.py::try_handle` is **first** in
`_PIPELINE` and swallows everything — including the greeting — until the user taps
«Принимаю». That is where it matters most, because notes, reminders and subscriptions
are created in the chat, not on the site. Storage is shared, so consenting on either
surface opens both. A handler placed above it in `_PIPELINE` becomes reachable without
consent; a test pins the ordering.

Three consequences worth knowing before you debug something confusing:

- **A new test that logs in and expects 200 will get a 302 to `/consent`.** Call
  `consents.accept(uid, source="test")` in the fixture, the way every panel test file
  already does. The gate itself is covered by `tests/test_consent.py`.
- **A test that drives `_PIPELINE` needs the same call**, or every message comes back
  as the consent offer instead of reaching the handler under test.
- **Seamless VK launch does not imply consent.** VK hands us `vk_user_id`, not
  agreement to our processing, so the Mini App shows the screen once too. This does
  not conflict with VK rule 1.1.2 (that one is about redundant *authentication*);
  1.1.4 requires exactly such an acceptance.

`/consent` renders without login (the bot links to it before the user has told us
anything) but shows the accept form only to a logged-in user.

`user_data.export_all()` (`/me/export`) and `user_data.purge()` walk the same
`_USER_TABLES`, so what gets exported and what gets deleted cannot drift apart — a
test asserts the two sets are equal. Add a table with a `user_id`/`vk_id` column and
`tests/test_delete_my_data.py` fails until it is listed there.

### VK Mini App

The panel runs inside VK as a Mini App, and the platform rules
(https://dev.vk.com/ru/mini-apps-rules) are enforceable requirements, not advice —
moderation rejects on them. What the code does for each:

- **1.2.2 / 1.1.2 — seamless auth.** `vk_launch_user_id()` authenticates from signed
  launch params: signature, our `vk_app_id`, and `vk_ts` freshness must all hold.
  Asking a VK user for a code, email or VK ID is a rule violation, so `/login`
  redirects inside when the launch is signed. The OTP path stays for plain browsers.
  Checked on **every** request, not just the first — browsers block iframe cookies.
- **1.1.4 / 2.4.1** — links to terms, privacy and support live in the in-app footer
  (`.app-foot`), not only on the login page a VK user never sees.
- **2.2.1** — `VKWebAppInit` must reach VK, or the container shows a blank frame and
  then "Приложение не инициализировано". Both templates send it **inline, before any
  external resource**, and the bridge itself is vendored at `static/vk-bridge.min.js`
  (v3.0.2, from `unpkg.com/@vkontakte/vk-bridge`) and loaded `defer` — as a
  render-blocking `<script>` in `<head>` a slow CDN alone was enough to miss VK's
  timeout. Keep both properties when touching the head.
- **1.2.6** — `_rate_limit()` caps requests per IP (`PANEL_RATE_LIMIT_RPM`), skipping
  `/healthz` and `/static/`.
- **3.2.2** — `viewport-fit=cover` plus `env(safe-area-inset-*)` padding.

**CSP must stay a single header.** Browsers enforce every CSP header they receive,
so an `add_header Content-Security-Policy` in nginx does not replace the app's — both
apply, and a frame has to be allowed by each. The server's copy listed only `vk.com`
and blocked the Mini App once VK started serving from `vk.ru`. Set the policy in
`_security_headers()` and nowhere else.

`tests/test_vk_rules.py` pins all of the above.

### Health checks

`/healthz` is public (a monitor calls it) and returns 200 only when `notes.db` opens
and every worker's heartbeat is younger than three poll intervals; otherwise 503 with
per-worker state. It answers the failure systemd cannot see: the bot process alive,
one worker dead. Keep it free of user data, paths and versions — it is exposed to the
internet.

### CI

`.github/workflows/ci.yml` runs three jobs on every push and PR: `ruff check .`,
`pytest -q` on Python 3.10 and 3.12, and the browser smoke against a panel it starts
itself (screenshots are uploaded as an artifact, the panel log is dumped on failure).
The smoke job needs no secrets — `VK_TOKEN` is empty and every DB goes to a temp dir.

## Other docs (Russian, for humans)

- `README.md` — quick start, panel login, first schedule upload, deploy commands.
- `docs/LAUNCH.md` — pre-launch checklist and the standing list of known limitations;
  update it when you fix or add one.
- `deploy/` — mirrors the live machine, so treat it as the source of truth rather
  than a template: the project lives at **`/root/vkbot`** with its venv in `venv/`,
  the panel runs under **gunicorn on 127.0.0.1:8080** (`-w 1` — the in-memory state
  above forbids a second worker) and `python web_panel.py`/waitress is only the local
  and CI path. That directory is hardcoded in `vkbot.service`, `vkpanel.service`,
  `vkbot-backup.service` and `backup.sh`'s `PROJECT_DIR` — moving the project means
  editing all four. `nginx-panel.conf` needs `panel-ratelimit.conf` in `conf.d/`
  beside it (the `limit_req_zone` only works in the http context, and nginx refuses
  to start without it). The backup pair `vkbot-backup.service`/`.timer` drives
  `backup.sh` (online SQLite copies + `schedule_versions.tar.gz`, integrity-checked,
  14-day rotation). Changing `PANEL_MAX_UPLOAD_MB` means changing
  `client_max_body_size` too — nginx smaller than the app turns a readable error into
  a bare 413.
- Every timestamp — domain and bookkeeping alike — is written through
  `config.now_msk()`, so the server's own timezone doesn't matter. A guard test
  (`tests/test_timestamps.py`) fails the suite if a bare `datetime.now()` reappears
  in `vkbot/`, `web_panel.py` or `import_excel.py`.
