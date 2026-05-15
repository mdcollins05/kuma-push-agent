import logging
import threading
from datetime import datetime, timedelta

logger = logging.getLogger(__name__)

TTL = timedelta(minutes=5)

_lock = threading.Lock()
_cache: list = []
_cache_at: datetime | None = None


def get() -> list:
    return _cache


def refresh() -> None:
    from .database import SessionLocal
    from .models import AppSettings
    from .kuma import get_notifications

    db = SessionLocal()
    try:
        cfg = db.get(AppSettings, 1)
        if not cfg or not cfg.configured:
            return
        notifications = get_notifications(cfg.kuma_url, cfg.kuma_username, cfg.kuma_password)
        global _cache, _cache_at
        with _lock:
            _cache = notifications
            _cache_at = datetime.utcnow()
        logger.debug("Notification cache refreshed: %d entries", len(notifications))
    except Exception as exc:
        logger.warning("Notification cache refresh failed: %s", exc)
    finally:
        db.close()
