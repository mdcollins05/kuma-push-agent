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
        logger.warning("Notification cache DB load failed: %s", exc)
    finally:
        db.close()


def refresh(raise_on_error: bool = False) -> None:
    """Fetch notifications from Kuma, update in-memory cache, and persist to DB."""
    from .database import SessionLocal
    from .models import AppSettings, KumaNotification
    from .kuma import get_notifications

    db = SessionLocal()
    try:
        cfg = db.get(AppSettings, 1)
        if not cfg or not cfg.configured:
            db.query(KumaNotification).delete(synchronize_session=False)
            db.commit()
            global _cache
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
        logger.debug("Notification cache refreshed from Kuma: %d entries", len(notifications))
    except Exception as exc:
        logger.warning("Notification cache refresh failed: %s", exc)
        if raise_on_error:
            raise
    finally:
        db.close()
