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
from .routers import api, auth, dashboard, monitors, settings as settings_router, jobs as jobs_router
from .scheduler import scheduler, add_check_job, start_kuma_queue_processor, start_notification_cache_refresher
from .seed import seed_from_yaml

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI):
    from .database import SessionLocal
    from .models import Monitor

    Base.metadata.create_all(bind=engine)

    # Add columns introduced after initial schema (idempotent — fails silently if column exists)
    with engine.connect() as conn:
        for ddl in [
            "ALTER TABLE monitors ADD COLUMN max_response_ms INTEGER",
            "ALTER TABLE monitors ADD COLUMN notification_ids TEXT",
            """CREATE TABLE IF NOT EXISTS kuma_jobs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                job_type TEXT NOT NULL,
                monitor_id INTEGER,
                monitor_name TEXT,
                payload TEXT NOT NULL DEFAULT '{}',
                status TEXT NOT NULL DEFAULT 'pending',
                error TEXT,
                retry_count INTEGER NOT NULL DEFAULT 0,
                next_retry_at DATETIME,
                created_at DATETIME
            )""",
            "ALTER TABLE kuma_jobs ADD COLUMN monitor_id INTEGER",
            "ALTER TABLE kuma_jobs ADD COLUMN retry_count INTEGER NOT NULL DEFAULT 0",
            "ALTER TABLE kuma_jobs ADD COLUMN next_retry_at DATETIME",
            "ALTER TABLE app_settings ADD COLUMN timezone TEXT DEFAULT 'UTC'",
        ]:
            try:
                conn.execute(__import__("sqlalchemy").text(ddl))
                conn.commit()
            except Exception:
                pass

    db = SessionLocal()
    try:
        if not db.get(AppSettings, 1):
            db.add(AppSettings(id=1))
            db.commit()

        seed_from_yaml(db, settings.seed_file)

        monitors_list = db.query(Monitor).filter_by(enabled=True).all()
        for monitor in monitors_list:
            add_check_job(monitor.id, monitor.interval, monitor.last_check_time)

    finally:
        db.close()

    start_kuma_queue_processor()
    start_notification_cache_refresher()
    scheduler.start()
    logger.info("Kuma Push Agent v%s started — %d monitor jobs scheduled", APP_VERSION, len(scheduler.get_jobs()))

    yield

    scheduler.shutdown(wait=False)
    logger.info("Kuma Push Agent stopped")


app = FastAPI(title="Kuma Push Agent", lifespan=lifespan)

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

app.include_router(auth.router)
app.include_router(dashboard.router)
app.include_router(monitors.router)
app.include_router(settings_router.router)
app.include_router(api.router)
app.include_router(jobs_router.router)
