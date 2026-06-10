import logging
import threading
from datetime import datetime, timezone

logger = logging.getLogger(__name__)

_lock = threading.Lock()
_cache: list = []
_last_run: datetime | None = None
_last_error: str | None = None


def get() -> list:
    return _cache


def status() -> dict:
    return {
        "last_run": _last_run.isoformat() if _last_run else None,
        "last_error": _last_error,
    }


def load_from_db() -> None:
    """Populate the in-memory cache from the DB. Called at startup before the scheduler runs."""
    from .database import SessionLocal
    from .models import AppSettings, KumaNotification

    db = SessionLocal()
    try:
        cfg = db.get(AppSettings, 1)
        global _cache
        if not cfg or not cfg.configured:
            with _lock:
                _cache = []
            logger.info("Notification cache skipped: Kuma not configured")
            return
        notifications = [{"id": n.id, "name": n.name} for n in db.query(KumaNotification).all()]
        with _lock:
            _cache = notifications
        logger.info("Notification cache loaded from DB: %d entries", len(notifications))
    except Exception as exc:
        logger.warning("Notification cache DB load failed: %s: %s", type(exc).__name__, exc, exc_info=True)
    finally:
        db.close()


def refresh(raise_on_error: bool = False) -> None:
    """Fetch notifications from Kuma, update in-memory cache, and persist to DB."""
    from .database import SessionLocal
    from .models import AppSettings, KumaNotification
    from .kuma import get_notifications

    global _cache, _last_run, _last_error

    db = SessionLocal()
    try:
        cfg = db.get(AppSettings, 1)
        if not cfg or not cfg.configured:
            db.query(KumaNotification).delete(synchronize_session=False)
            db.commit()
            with _lock:
                _cache = []
            return
        notifications = get_notifications(cfg.kuma_url, cfg.kuma_username, cfg.kuma_password)

        # Sync to DB: replace all rows
        db.query(KumaNotification).delete(synchronize_session=False)
        for n in notifications:
            db.add(KumaNotification(id=n["id"], name=n["name"]))
        db.commit()

        with _lock:
            _cache = [{"id": n["id"], "name": n["name"]} for n in notifications]
            _last_run = datetime.now(timezone.utc).replace(tzinfo=None)
            _last_error = None
        logger.debug("Notification cache refreshed from Kuma: %d entries", len(notifications))
    except Exception as exc:
        logger.warning("Notification cache refresh failed: %s: %s", type(exc).__name__, exc, exc_info=True)
        with _lock:
            _last_run = datetime.now(timezone.utc).replace(tzinfo=None)
            _last_error = f"{type(exc).__name__}: {exc}"
        if raise_on_error:
            raise
    finally:
        db.close()
