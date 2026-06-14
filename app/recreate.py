"""Shared logic for the 'Recreate Kuma monitor' action.

Drops the cached Kuma sync state and reschedules the check job to fire
immediately, so the checker provisions a fresh Kuma monitor and the
sync_monitor task shows up in the banner within seconds rather than waiting
up to `interval` seconds for the next tick.

We deliberately do NOT probe Kuma to defend against the "user restored the
monitor on Kuma between the last failed push and clicking Recreate" race —
there is no read-only Kuma endpoint that can verify a push token's existence
(the `/api/push/{token}` endpoint records a heartbeat as a side effect), so
any probe would itself mutate Kuma state from a synchronous request handler.

The flag this guards (`kuma_missing`) is set by checker.run_check() on push
404 and cleared on push <400. The user only sees the Recreate banner when
the flag is True, which means the most recent push round confirmed the
monitor is gone. The race window where a user could click Recreate after a
fast Kuma-side restore is bounded by one check interval and accepted as a
known limitation.
"""
import logging

from .kuma_queue import cancel_monitor_tasks
from .scheduler import add_check_job

logger = logging.getLogger(__name__)


def reset_and_reschedule(monitor, db) -> None:
    """Clear the cached Kuma sync state, cancel queued tasks targeting the now-dead
    kuma_monitor_id, and fire the check job immediately so the sync_monitor task
    appears right away."""
    cancel_monitor_tasks(db, monitor.id)
    monitor.kuma_monitor_id = None
    monitor.push_token = None
    monitor.kuma_synced = False
    monitor.kuma_missing = False
    db.commit()
    add_check_job(monitor.id, monitor.interval)
