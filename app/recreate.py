"""Shared logic for the 'Recreate Kuma monitor' action.

When the user clicks Recreate, the work splits into two steps that both
happen on a worker thread so the async route handler never blocks:

1. Verify the apparent kuma_missing state via a *read-only* Kuma API call
   (Socket.IO `getMonitor`). This is the only way to confirm the monitor
   is gone without writing — the push heartbeat endpoint mutates Kuma
   state and so isn't safe to use as a probe.

2. If verified missing → drop the cached kuma_monitor_id / push_token,
   cancel queued tasks targeting the dead ID, and fire the check job
   immediately so the sync_monitor task appears in the banner right away.
   If verified still alive → just clear kuma_missing (the flag was
   stale, no need to recreate). If the verify call itself fails →
   leave state untouched so the user can retry.

Callers must invoke verify_then_reset() via fastapi.concurrency.run_in_threadpool()
because it performs blocking Socket.IO + DB work.
"""
import logging

from .kuma_queue import cancel_monitor_tasks
from .scheduler import add_check_job

logger = logging.getLogger(__name__)


# Outcomes returned by verify_then_reset().
OUTCOME_VERIFIED = "verified"       # Kuma still has the monitor — only cleared kuma_missing.
OUTCOME_RECREATED = "recreated"     # Kuma confirms missing (or no way to verify) → full reset done.
OUTCOME_UNREACHABLE = "unreachable" # Verify call failed (network/auth) — no state change.


def verify_then_reset(monitor_id: int) -> str:
    """Worker-side entry point. Opens its own DB session so it's safe to call
    from a thread pool — never share a SQLAlchemy session across threads."""
    from .database import SessionLocal
    from .models import AppSettings, Monitor

    db = SessionLocal()
    try:
        monitor = db.get(Monitor, monitor_id)
        if not monitor:
            return OUTCOME_UNREACHABLE
        app_cfg = db.get(AppSettings, 1)

        # No way to verify (Kuma not configured, or no kuma_monitor_id to look up)
        # → fall back to the legacy "trust the kuma_missing flag, do a full reset"
        # path. Without an upstream to ask, the user clicking the button is the
        # best signal we have.
        if not (app_cfg and app_cfg.kuma_url and monitor.kuma_monitor_id):
            _reset_and_reschedule(monitor, db)
            return OUTCOME_RECREATED

        try:
            from .kuma import monitor_exists
            exists = monitor_exists(
                monitor.kuma_monitor_id,
                app_cfg.kuma_url, app_cfg.kuma_username, app_cfg.kuma_password,
            )
        except Exception as exc:
            logger.warning(
                "Recreate verify for monitor %d failed (%s: %s) — leaving state untouched",
                monitor_id, type(exc).__name__, exc,
            )
            return OUTCOME_UNREACHABLE

        if exists:
            if monitor.kuma_missing:
                monitor.kuma_missing = False
                db.commit()
                logger.info(
                    "Recreate: monitor %d still present on Kuma — cleared stale kuma_missing",
                    monitor_id,
                )
            return OUTCOME_VERIFIED

        _reset_and_reschedule(monitor, db)
        return OUTCOME_RECREATED
    finally:
        db.close()


def _reset_and_reschedule(monitor, db) -> None:
    cancel_monitor_tasks(db, monitor.id)
    monitor.kuma_monitor_id = None
    monitor.push_token = None
    monitor.kuma_synced = False
    monitor.kuma_missing = False
    db.commit()
    add_check_job(monitor.id, monitor.interval)
