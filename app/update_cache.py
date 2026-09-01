import logging
import threading
from datetime import datetime, timedelta, timezone

import httpx
from packaging.version import InvalidVersion, Version

from .config import APP_VERSION

logger = logging.getLogger(__name__)

REPO = "mdcollins05/kuma-push-agent"
CHECK_INTERVAL_HOURS = 6


def _is_dev_version(v: str) -> bool:
    """Skip update checks for unreleased builds: explicit 'dev', PEP 440 local-version
    builds (anything containing '+'), and the hatch-vcs '0.0.0+unknown' fallback."""
    return v == "dev" or "+" in v or v.startswith("0.0.0")

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


def load_from_db() -> None:
    """Restore the last-known latest version from the DB so the badge survives container restarts."""
    if _is_dev_version(APP_VERSION):
        return

    from .database import SessionLocal
    from .models import AppSettings

    db = SessionLocal()
    try:
        cfg = db.get(AppSettings, 1)
        latest = cfg.latest_version if cfg else None
        last_dt = cfg.last_update_check if cfg else None
    finally:
        db.close()

    global _latest_version, _update_available, _last_run
    with _lock:
        if latest:
            _latest_version = latest
            _update_available = _is_newer(latest, APP_VERSION)
        if last_dt is not None:
            _last_run = last_dt.isoformat()
    if latest:
        logger.info("Update cache loaded from DB: latest=v%s update_available=%s", latest, _update_available)


def refresh() -> None:
    if _is_dev_version(APP_VERSION):
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
        _persist_check(now, tag)
        if update_available:
            logger.info("Update available: v%s (current: v%s)", tag, APP_VERSION)
    except Exception as exc:
        with _lock:
            _last_run = now.isoformat()
            _last_error = f"{type(exc).__name__}: {exc}"
        logger.warning("Update check failed: %s: %s", type(exc).__name__, exc)


def _persist_check(dt: datetime, latest: str) -> None:
    from .database import SessionLocal
    from .models import AppSettings
    db = SessionLocal()
    try:
        cfg = db.get(AppSettings, 1)
        if cfg:
            cfg.last_update_check = dt
            cfg.latest_version = latest
            db.commit()
    except Exception as exc:
        logger.warning("Could not persist update check: %s", exc)
    finally:
        db.close()


def _is_newer(latest: str, current: str) -> bool:
    """Compare two versions under PEP 440.

    Both spellings of a pre-release tag must compare identically: the git tag is
    written "0.4.0-dev.1", but hatch-vcs normalises it to "0.4.0.dev1" before it
    reaches the runtime via importlib.metadata. Version() parses both to the same
    release, so a pre-release never reports an older stable as an upgrade.
    """
    try:
        return Version(latest) > Version(current)
    except InvalidVersion:
        return False
