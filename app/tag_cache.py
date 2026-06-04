import logging
import threading

logger = logging.getLogger(__name__)

_lock = threading.Lock()
_cache: list = []


def get() -> list:
    return _cache


def load_from_db() -> None:
    """Populate the in-memory cache from the DB. Called at startup before the scheduler runs."""
    from .database import SessionLocal
    from .models import KumaTag

    db = SessionLocal()
    try:
        tags = [{"id": t.id, "name": t.name, "color": t.color} for t in db.query(KumaTag).all()]
        global _cache
        with _lock:
            _cache = tags
        logger.info("Tag cache loaded from DB: %d entries", len(tags))
    except Exception as exc:
        logger.warning("Tag cache DB load failed: %s", exc)
    finally:
        db.close()


def refresh() -> None:
    """Fetch tags from Kuma, update in-memory cache, and persist to DB."""
    from .database import SessionLocal
    from .models import AppSettings, KumaTag
    from .kuma import get_tags

    db = SessionLocal()
    try:
        cfg = db.get(AppSettings, 1)
        if not cfg or not cfg.configured:
            return
        tags = get_tags(cfg.kuma_url, cfg.kuma_username, cfg.kuma_password)

        # Sync to DB: replace all rows
        db.query(KumaTag).delete(synchronize_session=False)
        for t in tags:
            db.add(KumaTag(id=t["id"], name=t["name"], color=t["color"]))
        db.commit()

        global _cache
        with _lock:
            _cache = [{"id": t["id"], "name": t["name"], "color": t["color"]} for t in tags]
        logger.debug("Tag cache refreshed from Kuma: %d entries", len(tags))
    except Exception as exc:
        logger.warning("Tag cache refresh failed: %s", exc)
    finally:
        db.close()
