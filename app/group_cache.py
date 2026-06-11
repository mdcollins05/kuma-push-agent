import logging
import threading
from datetime import datetime

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
    from .models import AppSettings, KumaGroup

    db = SessionLocal()
    try:
        cfg = db.get(AppSettings, 1)
        global _cache
        if not cfg or not cfg.configured:
            with _lock:
                _cache = []
            logger.info("Group cache skipped: Kuma not configured")
            return
        groups = [{"kuma_id": g.id, "name": g.name} for g in db.query(KumaGroup).all()]
        with _lock:
            _cache = groups
        logger.info("Group cache loaded from DB: %d entries", len(groups))
    except Exception as exc:
        logger.warning("Group cache DB load failed: %s: %s", type(exc).__name__, exc, exc_info=True)
    finally:
        db.close()


def refresh(raise_on_error: bool = False) -> None:
    """Fetch groups from Kuma, update in-memory cache, and persist to DB."""
    from .database import SessionLocal
    from .models import AppSettings, KumaGroup
    from .kuma import get_groups

    global _cache, _last_run, _last_error

    db = SessionLocal()
    try:
        cfg = db.get(AppSettings, 1)
        if not cfg or not cfg.configured:
            db.query(KumaGroup).delete(synchronize_session=False)
            db.commit()
            with _lock:
                _cache = []
            return
        groups = get_groups(cfg.kuma_url, cfg.kuma_username, cfg.kuma_password)

        # Sync to DB: replace all rows
        db.query(KumaGroup).delete(synchronize_session=False)
        for g in groups:
            db.add(KumaGroup(id=g["kuma_id"], name=g["name"]))
        db.commit()

        with _lock:
            _cache = [{"kuma_id": g["kuma_id"], "name": g["name"]} for g in groups]
            _last_run = datetime.utcnow()
            _last_error = None
        logger.debug("Group cache refreshed from Kuma: %d entries", len(groups))
    except Exception as exc:
        logger.warning("Group cache refresh failed: %s: %s", type(exc).__name__, exc, exc_info=True)
        with _lock:
            _last_run = datetime.utcnow()
            _last_error = f"{type(exc).__name__}: {exc}"
        if raise_on_error:
            raise
    finally:
        db.close()
