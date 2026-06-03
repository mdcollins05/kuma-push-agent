import logging
import threading

logger = logging.getLogger(__name__)

_lock = threading.Lock()
_cache: list = []


def get() -> list:
    return _cache


def refresh() -> None:
    from .database import SessionLocal
    from .models import AppSettings
    from .kuma import get_tags

    db = SessionLocal()
    try:
        cfg = db.get(AppSettings, 1)
        if not cfg or not cfg.configured:
            return
        tags = get_tags(cfg.kuma_url, cfg.kuma_username, cfg.kuma_password)
        global _cache
        with _lock:
            _cache = tags
        logger.debug("Tag cache refreshed: %d entries", len(tags))
    except Exception as exc:
        logger.warning("Tag cache refresh failed: %s", exc)
    finally:
        db.close()
