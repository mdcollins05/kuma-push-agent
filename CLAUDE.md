# Kuma Push Agent — Claude Reference

## Project Overview

Dockerized Python/FastAPI web app that acts as a remote health-check agent for Uptime Kuma v2. Monitors URLs, pushes results via Uptime Kuma Push monitors, manages those monitors via Socket.IO API.

## Stack

- **FastAPI** + Uvicorn (port 3002)
- **SQLAlchemy 2.0** (sync) + **SQLite** at `/data/kuma_push_agent.db`
- **APScheduler** `BackgroundScheduler` + `ThreadPoolExecutor` — one job per monitor
- **httpx** (sync) for URL health checks and Kuma push heartbeats
- **uptime-kuma-api-v2** (sync Socket.IO) for Kuma monitor management
- **Jinja2** + Bootstrap 5 CDN for server-rendered UI
- **passlib[bcrypt]** for password hashing
- **itsdangerous** (via Starlette `SessionMiddleware`) for signed session cookies
- **uv** for dependency management

## File Map

```
app/
├── main.py          — FastAPI app factory, lifespan, middleware, exception handlers
├── config.py        — Settings (DATA_DIR, CONFIG_DIR, session_secret)
├── database.py      — SQLAlchemy engine + SessionLocal
├── models.py        — Monitor, AppSettings ORM models
├── schemas.py       — Pydantic MonitorCreate/Update/Response
├── dependencies.py  — get_db, require_auth, require_api_key, SetupRequired, LoginRequired
├── templates.py     — Jinja2Templates instance (path resolved relative to __file__)
├── kuma.py          — All uptime-kuma-api-v2 calls (create/pause/resume/delete/test)
├── checker.py       — run_check() — the per-monitor health check job
├── scheduler.py     — APScheduler singleton + add/remove/pause/resume job helpers
├── seed.py          — One-time YAML seed on first boot
└── routers/
    ├── auth.py      — /setup, /login, /logout
    ├── dashboard.py — GET /
    ├── monitors.py  — /monitors/* (CRUD + actions)
    ├── settings.py  — /settings, /settings/test, /settings/regenerate-key
    └── api.py       — /api/v1/* (JSON API, X-API-Key auth)
```

## Key Architecture Decisions

### uptime-kuma-api-v2 is blocking (sync Socket.IO)
Every call — `login()`, `add_monitor()`, `get_monitor()`, `pause_monitor()`, `delete_monitor()` — blocks the calling thread. **Never call from async context directly.** Always use `fastapi.concurrency.run_in_threadpool()` in route handlers, or call from within a threadpool APScheduler job.

### Short-lived Kuma connections
Use `with UptimeKumaApi(url) as api:` pattern in every function in `kuma.py`. Never create a long-lived singleton — sessions time out and there's no reconnect logic.

### push_token is not in add_monitor() response
After `api.add_monitor()`, you must call `api.get_monitor(kuma_id)` to retrieve `pushToken`. The return value of `add_monitor()` only contains `{"msg": "...", "monitorId": <int>}`.

### Push heartbeats are plain HTTP
The `/api/push/<token>?status=up&msg=OK&ping=<ms>` endpoint is a regular HTTP GET. No Socket.IO needed. Use `httpx.Client.get()` for this.

### Kuma sync is lazy / non-fatal
On startup and when creating monitors, Kuma sync may fail (Kuma not ready, wrong creds). `kuma_synced=False` monitors are retried in each check cycle. The app works fully without Kuma connectivity — it just won't push status.

### Session secret persists across restarts
`config.py:settings.session_secret` reads from `/data/.session_secret` (generates on first access). This means sessions survive container restarts.

### AppSettings is a singleton row (id=1)
The `app_settings` table always has exactly one row. Query with `db.get(AppSettings, 1)`. Created empty on first startup in `main.py` lifespan.

### First-run gate
`dependencies.py:require_auth()` raises `SetupRequired` if `AppSettings.ui_username is None`. `main.py` has an exception handler that redirects to `/setup`. After setup, it raises `LoginRequired` if there's no session, which redirects to `/login`.

### Checkbox form fields
HTML checkboxes only send a value when checked. The routers use `Form(True)` defaults but the checkbox value is the string `"true"`. FastAPI's bool coercion handles this — `verify_ssl: bool = Form(True)` receives either the string `"true"` (checked) or nothing (unchecked, defaults to `True`). If you add new checkboxes, test this carefully.

## Dev Workflow

```bash
# Local dev (without Docker) — needs Python 3.12+
uv sync
DATA_DIR=./data CONFIG_DIR=./config uv run uvicorn app.main:app --reload --port 3002

# Build and run in Docker
docker compose up --build

# Rebuild after dep changes
uv add <package>        # updates pyproject.toml
uv sync                 # regenerates uv.lock
docker compose build    # picks up new lock
```

**Templates are baked into the Docker image — there is no live reload in the container.** Every change to `app/` (templates, routers, models, etc.) requires a full rebuild: `docker compose up --build -d`.

## Branching and Release Workflow

All changes go through a branch + PR — never commit directly to `main`.

```bash
# Start a new feature or fix
git checkout -b feat/my-feature   # or fix/my-fix

# ... make changes, rebuild, test ...

git add <files>
git commit -m "feat: description"
git push -u origin feat/my-feature
# Open a PR on GitHub → merge into main
```

To publish a release after merging:
```bash
git checkout main && git pull
git tag v1.2.3
git push origin v1.2.3
```

This triggers the release workflow: tests run, then a multi-arch Docker image is built and pushed to `ghcr.io/mdcollins05/kuma-push-agent` with tags `1.2.3`, `1.2`, `1`, and `latest`.

## Testing

Tests run in a separate Docker image with dev dependencies (pytest) installed. No host Python install or running Kuma server needed.

```bash
docker compose --profile test run --rm test
```

- Tests use an in-memory SQLite DB isolated from production data
- FastAPI dependency overrides replace `get_db`, `require_auth`, and `require_api_key`
- Background jobs (APScheduler) start but use the real DB path — they won't affect test results since `AppSettings.configured=False` causes early returns
- Test files live in `tests/conftest.py` (fixtures) + `tests/test_api_monitors.py` + `tests/test_status_endpoints.py`

## Known Limitations / Watch Out For

1. **uptime-kuma-api-v2 PyPI name** — verify `pip show uptime-kuma-api-v2` works. The library is a community fork (`exaland/uptime-kuma-api-v2`). If the import fails, check the actual PyPI package name and update `pyproject.toml` and the import in `kuma.py`.

2. **pushToken key name** — `kuma.py:create_push_monitor()` checks both `pushToken` and `push_token` keys in the monitor response. If Kuma v2 uses a different field name, add it to the fallback chain.

3. **APScheduler job IDs** — jobs are named `monitor_<id>`. If you delete a monitor and create a new one, the new one may reuse the old ID. `replace_existing=True` on `add_job()` handles this correctly.

4. **SQLite JSON columns** — `expected_codes` is stored as JSON text. Always treat it as a Python list in code; never build raw SQL conditions on it.

5. **Form checkbox coercion** — see note above in Architecture Decisions.

6. **Kuma v2 compatibility** — the library may not support all Kuma v2 features. If you hit Socket.IO event format issues, check the `uptime-kuma-api-v2` GitHub for updates or open issues.
