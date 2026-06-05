import logging
import threading
from datetime import datetime, timedelta

logger = logging.getLogger(__name__)

TTL = timedelta(minutes=5)

_lock = threading.Lock()
_cache: list = []
_cache_at: datetime | None = None
_last_error: str | None = None


def get() -> list:
    return _cache


def status() -> dict:
    return {
        "last_run": _cache_at.isoformat() if _cache_at else None,
        "last_error": _last_error,
    }


def refresh() -> None:
    from .database import SessionLocal
    from .models import AppSettings
    from .kuma import get_notifications

    global _cache, _cache_at, _last_error

    db = SessionLocal()
    try:
        cfg = db.get(AppSettings, 1)
        if not cfg or not cfg.configured:
            return
        notifications = get_notifications(cfg.kuma_url, cfg.kuma_username, cfg.kuma_password)
        with _lock:
            _cache = notifications
            _cache_at = datetime.utcnow()
            _last_error = None
        logger.debug("Notification cache refreshed: %d entries", len(notifications))
    except Exception as exc:
        logger.warning("Notification cache refresh failed: %s", exc)
        with _lock:
            _cache_at = datetime.utcnow()
            _last_error = f"{type(exc).__name__}: {exc}"
    finally:
        db.close()
