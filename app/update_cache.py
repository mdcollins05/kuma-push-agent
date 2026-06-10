import logging
import threading
from datetime import datetime, timedelta, timezone

import httpx

from .config import APP_VERSION

logger = logging.getLogger(__name__)

REPO = "mdcollins05/kuma-push-agent"
CHECK_INTERVAL_HOURS = 6

_lock = threading.Lock()
_latest_version: str | None = None
_update_available: bool = False
_last_run: str | None = None
_last_error: str | None = None


def next_run_time() -> datetime:
    """Return when the initial startup check should fire — now if overdue, future if recent."""
    from .database import SessionLocal
    from .models import AppSettings
    db = SessionLocal()
    try:
        cfg = db.get(AppSettings, 1)
        last = cfg.last_update_check if cfg else None
    finally:
        db.close()
    if last is None:
        return datetime.now(timezone.utc).replace(tzinfo=None)
    due = last + timedelta(hours=CHECK_INTERVAL_HOURS)
    return due if due > datetime.now(timezone.utc).replace(tzinfo=None) else datetime.now(timezone.utc).replace(tzinfo=None)


def get() -> dict:
    with _lock:
        return {"latest": _latest_version, "update_available": _update_available}


def status() -> dict:
    with _lock:
        return {"last_run": _last_run, "last_error": _last_error}


def refresh() -> None:
    if APP_VERSION == "dev":
        return

    global _latest_version, _update_available, _last_run, _last_error
    now = datetime.now(timezone.utc).replace(tzinfo=None)
    try:
        with httpx.Client(timeout=10.0) as client:
            resp = client.get(
                f"https://api.github.com/repos/{REPO}/releases/latest",
                headers={"Accept": "application/vnd.github+json"},
            )
            resp.raise_for_status()
            tag = resp.json().get("tag_name", "").lstrip("v")
        update_available = _is_newer(tag, APP_VERSION)
        with _lock:
            _latest_version = tag
            _update_available = update_available
            _last_run = now.isoformat()
            _last_error = None
        _persist_last_check(now)
        if update_available:
            logger.info("Update available: v%s (current: v%s)", tag, APP_VERSION)
    except Exception as exc:
        with _lock:
            _last_run = now.isoformat()
            _last_error = f"{type(exc).__name__}: {exc}"
        logger.warning("Update check failed: %s: %s", type(exc).__name__, exc)


def _persist_last_check(dt: datetime) -> None:
    from .database import SessionLocal
    from .models import AppSettings
    db = SessionLocal()
    try:
        cfg = db.get(AppSettings, 1)
        if cfg:
            cfg.last_update_check = dt
            db.commit()
    except Exception as exc:
        logger.warning("Could not persist update check timestamp: %s", exc)
    finally:
        db.close()


def _is_newer(latest: str, current: str) -> bool:
    def _parse(v: str) -> tuple[tuple, str | None]:
        parts = v.split("-", 1)
        try:
            base = tuple(int(x) for x in parts[0].split("."))
        except ValueError:
            base = (0,)
        pre = parts[1] if len(parts) > 1 else None
        return base, pre

    latest_base, latest_pre = _parse(latest)
    current_base, current_pre = _parse(current)

    if latest_base != current_base:
        return latest_base > current_base
    # Same base version: stable release is newer than any pre-release
    return latest_pre is None and current_pre is not None
