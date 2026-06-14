"""Shared logic for the 'Recreate Kuma monitor' action.

Verifies the apparent kuma_missing state via a defensive push probe before
committing to a fresh create, so a restored Kuma monitor doesn't get
duplicated when the user clicks Recreate.
"""
import logging

import httpx

from .kuma import build_push_url
from .kuma_queue import cancel_monitor_tasks
from .scheduler import add_check_job

logger = logging.getLogger(__name__)


# Outcomes returned by recreate_or_verify().
OUTCOME_VERIFIED = "verified"   # push probe returned <400 → monitor still exists, only cleared kuma_missing
OUTCOME_RECREATED = "recreated"  # probe returned 404 (or no probe possible) → full reset done
OUTCOME_UNREACHABLE = "unreachable"  # probe failed or returned 5xx — no change to avoid duplicate risk


def recreate_or_verify(monitor, app_cfg, db) -> str:
    """Probe Kuma to see whether the monitor really is missing. If it responds,
    just clear kuma_missing. If 404, reset and trigger an immediate check. If
    the probe is inconclusive (5xx, timeout, no kuma_url), make no change.

    Caller is expected to have already confirmed that the monitor has prior
    sync state (kuma_monitor_id or kuma_synced).
    """
    kuma_url = app_cfg.kuma_url if app_cfg else None

    # No way to probe — fall back to the old "trust the user" full reset path.
    # Without a push token we can't ask Kuma about this monitor at all.
    if not kuma_url or not monitor.push_token:
        return _reset_and_reschedule(monitor, db)

    try:
        url = build_push_url(kuma_url, monitor.push_token, status="up", msg="OK", ping_ms=0)
        with httpx.Client(timeout=5.0) as client:
            resp = client.get(url)
    except Exception as exc:
        logger.warning(
            "Recreate probe for monitor %d failed (%s: %s) — refusing to reset to avoid duplicate",
            monitor.id, type(exc).__name__, exc,
        )
        return OUTCOME_UNREACHABLE

    if resp.status_code == 404:
        return _reset_and_reschedule(monitor, db)

    if resp.status_code < 400:
        # Monitor responded — it's still on Kuma. The kuma_missing flag was
        # stale (likely Kuma was down or the monitor was restored). Clear the
        # flag and keep the existing kuma_monitor_id / push_token.
        if monitor.kuma_missing:
            monitor.kuma_missing = False
            db.commit()
            logger.info("Recreate: monitor %d still present on Kuma; cleared kuma_missing", monitor.id)
        return OUTCOME_VERIFIED

    # 4xx other than 404, or 5xx — be conservative.
    logger.warning(
        "Recreate probe for monitor %d returned %d — refusing to reset to avoid duplicate",
        monitor.id, resp.status_code,
    )
    return OUTCOME_UNREACHABLE


def _reset_and_reschedule(monitor, db) -> str:
    cancel_monitor_tasks(db, monitor.id)
    monitor.kuma_monitor_id = None
    monitor.push_token = None
    monitor.kuma_synced = False
    monitor.kuma_missing = False
    db.commit()
    add_check_job(monitor.id, monitor.interval)
    return OUTCOME_RECREATED
