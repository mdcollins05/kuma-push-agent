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
    from .models import AppSettings, KumaTag

    db = SessionLocal()
    try:
        cfg = db.get(AppSettings, 1)
        global _cache
        if not cfg or not cfg.configured:
            with _lock:
                _cache = []
            logger.info("Tag cache skipped: Kuma not configured")
            return
        tags = [{"id": t.id, "name": t.name, "color": t.color} for t in db.query(KumaTag).all()]
        with _lock:
            _cache = tags
        logger.info("Tag cache loaded from DB: %d entries", len(tags))
    except Exception as exc:
        logger.warning("Tag cache DB load failed: %s: %s", type(exc).__name__, exc, exc_info=True)
    finally:
        db.close()


def refresh(raise_on_error: bool = False) -> None:
    """Fetch tags from Kuma, update in-memory cache, and persist to DB."""
    from .database import SessionLocal
    from .models import AppSettings, KumaTag
    from .kuma import get_tags

    global _cache, _last_run, _last_error

    db = SessionLocal()
    try:
        cfg = db.get(AppSettings, 1)
        if not cfg or not cfg.configured:
            db.query(KumaTag).delete(synchronize_session=False)
            db.commit()
            with _lock:
                _cache = []
            return
        tags = get_tags(cfg.kuma_url, cfg.kuma_username, cfg.kuma_password)

        # Sync to DB: replace all rows
        db.query(KumaTag).delete(synchronize_session=False)
        for t in tags:
            db.add(KumaTag(id=t["id"], name=t["name"], color=t["color"]))
        db.commit()

        with _lock:
            _cache = [{"id": t["id"], "name": t["name"], "color": t["color"]} for t in tags]
            _last_run = datetime.now(timezone.utc).replace(tzinfo=None)
            _last_error = None
        logger.debug("Tag cache refreshed from Kuma: %d entries", len(tags))
    except Exception as exc:
        logger.warning("Tag cache refresh failed: %s: %s", type(exc).__name__, exc, exc_info=True)
        with _lock:
            _last_run = datetime.now(timezone.utc).replace(tzinfo=None)
            _last_error = f"{type(exc).__name__}: {exc}"
        if raise_on_error:
            raise
    finally:
        db.close()
