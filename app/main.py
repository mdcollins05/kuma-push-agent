import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.responses import RedirectResponse
from fastapi.staticfiles import StaticFiles
from starlette.middleware.sessions import SessionMiddleware

from .config import settings, APP_VERSION
from .database import engine
from .dependencies import LoginRequired, SetupRequired
from .models import AppSettings, Base
from .routers import api, auth, dashboard, monitors, settings as settings_router, tasks as tasks_router
from .scheduler import scheduler, add_check_job, start_group_cache_refresher, start_kuma_task_processor, start_notification_cache_refresher, start_tag_cache_refresher, start_update_checker
from .seed import seed_from_yaml

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)


# Substrings of OperationalError messages that mean a migration is already
# applied (or wasn't needed). Anything else means a real DB problem — locked
# DB, syntax error, permission issue, schema corruption — and must crash
# startup so we don't silently accept a half-broken schema.
_MIGRATION_BENIGN_ERRORS = (
    "duplicate column name",                       # ALTER TABLE ADD COLUMN twice
    "no such column",                              # DROP COLUMN / RENAME COLUMN of already-dropped col
    "no such table",                               # ALTER TABLE on already-renamed/missing table
    "already exists",                              # generic CREATE / RENAME conflict
    "already another table or index with this name",  # SQLite RENAME TO target already exists
)


def _migration_already_applied(exc: Exception) -> bool:
    """True when a DB exception matches a known "already done" pattern. False
    forces the caller to re-raise so startup fails loudly."""
    msg = str(exc).lower()
    return any(needle in msg for needle in _MIGRATION_BENIGN_ERRORS)


def _migrate_legacy_renames(target_engine) -> None:
    """Rename the v0.1 `kuma_jobs` table → `kuma_tasks` and its `job_type` column.

    Pre-checks the schema with the dialect inspector so we only run ALTER when
    actually needed and so half-migrated states (both old and new tables/columns
    present) crash startup loudly rather than letting rows get stranded.
    """
    import sqlalchemy as sa
    inspector = sa.inspect(target_engine)
    tables = set(inspector.get_table_names())

    if "kuma_jobs" in tables and "kuma_tasks" in tables:
        # Common case in DBs upgraded by the prior broad-except code: the rename
        # actually ran but the empty source table got left behind. Safe to drop
        # if it's empty; refuse otherwise so real data isn't silently abandoned.
        with target_engine.connect() as conn:
            stranded = conn.execute(sa.text("SELECT COUNT(*) FROM kuma_jobs")).scalar() or 0
        if stranded:
            raise RuntimeError(
                "Schema conflict: both 'kuma_jobs' and 'kuma_tasks' tables exist "
                f"and 'kuma_jobs' still has {stranded} row(s). Refusing to start so "
                "rows aren't silently abandoned. Resolve manually."
            )
        with target_engine.connect() as conn:
            conn.execute(sa.text("DROP TABLE kuma_jobs"))
            conn.commit()
        logger.info("Dropped empty legacy 'kuma_jobs' table left over from prior migration")
        tables = set(sa.inspect(target_engine).get_table_names())

    with target_engine.connect() as conn:
        if "kuma_jobs" in tables and "kuma_tasks" not in tables:
            conn.execute(sa.text("ALTER TABLE kuma_jobs RENAME TO kuma_tasks"))
            conn.commit()

        tables_after = set(sa.inspect(target_engine).get_table_names())
        if "kuma_tasks" not in tables_after:
            return  # fresh install — create_all will build the table

        cols = {c["name"] for c in sa.inspect(target_engine).get_columns("kuma_tasks")}
        if "job_type" in cols and "task_type" in cols:
            raise RuntimeError(
                "Schema conflict: kuma_tasks has both 'job_type' and 'task_type' columns. "
                "Resolve manually."
            )
        if "job_type" in cols and "task_type" not in cols:
            conn.execute(sa.text("ALTER TABLE kuma_tasks RENAME COLUMN job_type TO task_type"))
            conn.commit()


def _migrate_monitor_config(target_engine) -> None:
    """v0.3.0 migration: fold per-type HTTP fields into `config` JSON, drop legacy columns.

    Idempotent — skips rows whose `config` is already populated, and skips the whole
    block when no legacy columns remain in the table.
    """
    import json as _json
    sa = __import__("sqlalchemy")
    with target_engine.connect() as conn:
        cols = {row[1] for row in conn.execute(sa.text("PRAGMA table_info(monitors)")).fetchall()}
        legacy_cols = {"url", "expected_codes", "keyword", "max_response_ms", "verify_ssl"}
        present_legacy = legacy_cols & cols
        if not present_legacy:
            return
        select_cols = ["id", "config"] + sorted(present_legacy)
        rows = conn.execute(sa.text(f"SELECT {', '.join(select_cols)} FROM monitors")).fetchall()
        for row in rows:
            row_map = dict(zip(select_cols, row))
            existing = row_map.get("config")
            if existing:
                try:
                    parsed = _json.loads(existing) if isinstance(existing, str) else existing
                except (TypeError, ValueError):
                    parsed = None
                # Only skip rows whose config already matches the new contract
                # (dict with a discriminator). Anything else — empty dict, list,
                # string, malformed JSON — falls through and is rebuilt from
                # the legacy columns before they're dropped.
                if isinstance(parsed, dict) and parsed.get("type"):
                    continue
            expected = row_map.get("expected_codes")
            if isinstance(expected, str):
                try:
                    expected = _json.loads(expected)
                except Exception:
                    expected = [200]
            new_config = {
                "type": "http",
                "url": row_map.get("url") or "",
                "method": "GET",
                "headers": {},
                "body": None,
                "expected_codes": expected or [200],
                "keyword": row_map.get("keyword"),
                "max_response_ms": row_map.get("max_response_ms"),
                "verify_ssl": bool(row_map.get("verify_ssl")) if row_map.get("verify_ssl") is not None else True,
            }
            conn.execute(
                sa.text("UPDATE monitors SET config = :config WHERE id = :id"),
                {"config": _json.dumps(new_config), "id": row_map["id"]},
            )
        conn.commit()
        from sqlalchemy.exc import OperationalError
        for ddl in [
            "ALTER TABLE monitors DROP COLUMN url",
            "ALTER TABLE monitors DROP COLUMN expected_codes",
            "ALTER TABLE monitors DROP COLUMN keyword",
            "ALTER TABLE monitors DROP COLUMN max_response_ms",
            "ALTER TABLE monitors DROP COLUMN verify_ssl",
        ]:
            try:
                conn.execute(sa.text(ddl))
                conn.commit()
            except OperationalError as exc:
                if _migration_already_applied(exc):
                    continue  # column already dropped
                msg = str(exc).lower()
                if 'near "drop": syntax error' in msg:
                    # SQLite < 3.35 doesn't support DROP COLUMN. Leaving the
                    # legacy columns behind is harmless — `config` is the live
                    # source of truth.
                    logger.warning("SQLite lacks DROP COLUMN support (%s): %s", ddl, exc)
                    continue
                logger.error("Legacy column drop migration failed: %s — %s", ddl, exc)
                raise


@asynccontextmanager
async def lifespan(app: FastAPI):
    from .database import SessionLocal
    from .models import Monitor

    _migrate_legacy_renames(engine)

    # Rename legacy task type strings — must not be silently swallowed on real errors
    # because _run() no longer handles the old names and would fail those tasks permanently.
    from sqlalchemy.exc import OperationalError
    with engine.connect() as conn:
        for ddl in [
            "UPDATE kuma_tasks SET task_type = 'update_monitor' WHERE task_type = 'update'",
            "UPDATE kuma_tasks SET task_type = 'pause_monitor' WHERE task_type = 'pause'",
            "UPDATE kuma_tasks SET task_type = 'resume_monitor' WHERE task_type = 'resume'",
            "UPDATE kuma_tasks SET task_type = 'delete_monitor' WHERE task_type = 'delete'",
        ]:
            try:
                conn.execute(__import__("sqlalchemy").text(ddl))
                conn.commit()
            except OperationalError as exc:
                if "no such table" in str(exc).lower():
                    pass  # fresh install — table created by create_all below
                else:
                    logger.error("Task type migration failed: %s — %s", ddl, exc)
                    raise

    Base.metadata.create_all(bind=engine)

    # Add columns introduced after initial schema (idempotent — skips ALTERs that
    # have already been applied, but real DDL errors still surface).
    with engine.connect() as conn:
        for ddl in [
            "ALTER TABLE monitors ADD COLUMN max_response_ms INTEGER",
            "ALTER TABLE monitors ADD COLUMN notification_ids TEXT",
            "ALTER TABLE kuma_tasks ADD COLUMN monitor_id INTEGER",
            "ALTER TABLE kuma_tasks ADD COLUMN retry_count INTEGER NOT NULL DEFAULT 0",
            "ALTER TABLE kuma_tasks ADD COLUMN next_retry_at DATETIME",
            "ALTER TABLE app_settings ADD COLUMN timezone TEXT DEFAULT 'UTC'",
            "ALTER TABLE app_settings ADD COLUMN last_update_check DATETIME",
            "ALTER TABLE app_settings ADD COLUMN latest_version TEXT",
            "ALTER TABLE monitors ADD COLUMN tag_ids TEXT",
            "ALTER TABLE monitors ADD COLUMN kuma_group_id INTEGER",
            "ALTER TABLE monitors ADD COLUMN config TEXT",
            "ALTER TABLE monitors ADD COLUMN kuma_missing BOOLEAN DEFAULT 0",
            """CREATE TABLE IF NOT EXISTS kuma_tags (
                id INTEGER PRIMARY KEY,
                name TEXT NOT NULL,
                color TEXT NOT NULL
            )""",
            """CREATE TABLE IF NOT EXISTS kuma_notifications (
                id INTEGER PRIMARY KEY,
                name TEXT NOT NULL
            )""",
            """CREATE TABLE IF NOT EXISTS kuma_groups (
                id INTEGER PRIMARY KEY,
                name TEXT NOT NULL
            )""",
        ]:
            try:
                conn.execute(__import__("sqlalchemy").text(ddl))
                conn.commit()
            except OperationalError as exc:
                if _migration_already_applied(exc):
                    continue
                logger.error("Schema migration failed: %s — %s", ddl, exc)
                raise

    _migrate_monitor_config(engine)

    db = SessionLocal()
    try:
        if not db.get(AppSettings, 1):
            db.add(AppSettings(id=1))
            db.commit()

        # Reset failed delete tasks — if the Kuma monitor is already gone, they'll
        # resolve immediately under the updated "does not exist" success logic.
        from .models import KumaTask
        db.query(KumaTask).filter_by(task_type="delete_monitor", status="failed").update(
            {"status": "pending", "retry_count": 0, "next_retry_at": None, "error": None},
            synchronize_session=False,
        )
        db.commit()

        seed_from_yaml(db, settings.seed_file)

        monitors_list = db.query(Monitor).filter_by(enabled=True).all()
        for monitor in monitors_list:
            add_check_job(monitor.id, monitor.interval, monitor.last_check_time)

    finally:
        db.close()

    from .notification_cache import load_from_db as _load_notifications_from_db
    _load_notifications_from_db()

    from .tag_cache import load_from_db as _load_tags_from_db
    _load_tags_from_db()

    from .group_cache import load_from_db as _load_groups_from_db
    _load_groups_from_db()

    from .update_cache import load_from_db as _load_update_from_db
    _load_update_from_db()

    start_kuma_task_processor()
    start_notification_cache_refresher()
    start_tag_cache_refresher()
    start_group_cache_refresher()
    start_update_checker()
    scheduler.start()
    logger.info("Kuma Push Agent v%s started — %d monitor tasks scheduled", APP_VERSION, len(scheduler.get_jobs()))

    yield

    scheduler.shutdown(wait=False)
    logger.info("Kuma Push Agent stopped")


app = FastAPI(
    title="Kuma Push Agent",
    description=(
        "Remote health-check agent for Uptime Kuma v2.\n\n"
        "Monitors URLs and pushes results via Uptime Kuma Push monitors.\n\n"
        "## Authentication\n"
        "All endpoints except `POST /api/v1/setup` require an `X-API-Key` header. "
        "Find or regenerate your key in **Settings → API Key**."
    ),
    version=APP_VERSION,
    openapi_tags=[
        {"name": "Setup", "description": "One-time application bootstrap. No authentication required."},
        {"name": "Settings", "description": "Configure application and Uptime Kuma connection."},
        {"name": "Tags", "description": "View and create Uptime Kuma tags."},
        {"name": "Notifications", "description": "View Uptime Kuma notification channels."},
        {"name": "Groups", "description": "View Uptime Kuma group monitors."},
        {"name": "Monitors", "description": "Create, read, update, and delete health-check monitors."},
        {"name": "System", "description": "Application metadata — version, update availability."},
    ],
    swagger_ui_parameters={"persistAuthorization": True},
    lifespan=lifespan,
)


def _custom_openapi():
    if app.openapi_schema:
        return app.openapi_schema
    from fastapi.openapi.utils import get_openapi
    schema = get_openapi(
        title=app.title,
        version=app.version,
        description=app.description,
        routes=app.routes,
        tags=app.openapi_tags,
    )
    schema.setdefault("components", {})["securitySchemes"] = {
        "ApiKeyHeader": {"type": "apiKey", "in": "header", "name": "X-API-Key"}
    }
    _no_auth = {"/api/v1/setup"}
    for path, path_item in schema["paths"].items():
        if path not in _no_auth:
            for op in path_item.values():
                op["security"] = [{"ApiKeyHeader": []}]
    app.openapi_schema = schema
    return schema


app.openapi = _custom_openapi

app.add_middleware(
    SessionMiddleware,
    secret_key=settings.session_secret,
    session_cookie="kuma_push_agent_session",
    max_age=86400 * 30,
    https_only=False,
)


@app.exception_handler(SetupRequired)
async def setup_required_handler(request: Request, exc: SetupRequired):
    return RedirectResponse("/setup", status_code=302)


@app.exception_handler(LoginRequired)
async def login_required_handler(request: Request, exc: LoginRequired):
    return RedirectResponse(f"/login?next={exc.next_url}", status_code=302)


app.mount("/static", StaticFiles(directory="app/static"), name="static")

app.include_router(auth.router, include_in_schema=False)
app.include_router(dashboard.router, include_in_schema=False)
app.include_router(monitors.router, include_in_schema=False)
app.include_router(settings_router.router, include_in_schema=False)
app.include_router(api.public_router)
app.include_router(api.router)
app.include_router(tasks_router.router)
