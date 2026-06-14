import logging
import threading
from datetime import datetime, timedelta, timezone

COMPLETED_TASK_TTL_HOURS = 24

logger = logging.getLogger(__name__)

MAX_RETRIES = 3
BASE_RETRY_SECONDS = 10  # exponential backoff: 10s, 20s, 40s

_status_lock = threading.Lock()
_last_run: datetime | None = None
_last_error: str | None = None


def processor_status() -> dict:
    return {
        "last_run": _last_run.isoformat() if _last_run else None,
        "last_error": _last_error,
    }


def enqueue(db, task_type: str, payload: dict, monitor_name: str = None, monitor_id: int = None) -> None:
    """Insert a Kuma sync task, coalescing against any pending/failed prior tasks
    that the new one would supersede. This keeps the queue from stacking stale
    intent during periods when Kuma is unreachable.

    Coalescing rules per task_type (only when monitor_id is set):
      - delete_monitor: cancel ALL pending/failed tasks for the monitor — they're
        moot once Kuma's row is gone.
      - update_monitor: cancel prior pending/failed update_monitor — the latest
        full-field replacement is the only intent that matters.
      - pause_monitor / resume_monitor: cancel pending pause/resume so we never
        leave a "pause, then resume" pair stacked.
      - update_tags: merge prior pending added/removed sets into the new payload
        and cancel them. If the merge nets to no change, skip the insert entirely.
      - create_tag / create_tags / sync_monitor: independent, no coalescing.
    """
    from .models import KumaTask

    if monitor_id is not None:
        skip_insert = _coalesce(db, task_type, monitor_id, payload)
        if skip_insert:
            db.commit()
            return

    db.add(KumaTask(
        task_type=task_type,
        payload=payload,
        monitor_name=monitor_name,
        monitor_id=monitor_id,
    ))
    db.commit()


def _coalesce(db, task_type: str, monitor_id: int, payload: dict) -> bool:
    """Cancel superseded prior tasks (and mutate payload for update_tags merge).
    Returns True when the new task should not be inserted because it merged to
    a no-op."""
    from .models import KumaTask

    if task_type == "delete_monitor":
        db.query(KumaTask).filter(
            KumaTask.monitor_id == monitor_id,
            KumaTask.status.in_(["pending", "failed"]),
        ).update({"status": "cancelled"}, synchronize_session=False)
        return False

    if task_type == "update_monitor":
        db.query(KumaTask).filter(
            KumaTask.monitor_id == monitor_id,
            KumaTask.task_type == "update_monitor",
            KumaTask.status.in_(["pending", "failed"]),
        ).update({"status": "cancelled"}, synchronize_session=False)
        return False

    if task_type in ("pause_monitor", "resume_monitor"):
        db.query(KumaTask).filter(
            KumaTask.monitor_id == monitor_id,
            KumaTask.task_type.in_(["pause_monitor", "resume_monitor"]),
            KumaTask.status.in_(["pending", "failed"]),
        ).update({"status": "cancelled"}, synchronize_session=False)
        return False

    if task_type == "update_tags":
        prior = db.query(KumaTask).filter(
            KumaTask.monitor_id == monitor_id,
            KumaTask.task_type == "update_tags",
            KumaTask.status.in_(["pending", "failed"]),
        ).all()
        if not prior:
            return False
        prior_added: set = set()
        prior_removed: set = set()
        for t in prior:
            prior_added |= set((t.payload or {}).get("added", []))
            prior_removed |= set((t.payload or {}).get("removed", []))
            t.status = "cancelled"
        new_added = set(payload.get("added", []))
        new_removed = set(payload.get("removed", []))
        # Net delta: a tag is added iff someone wanted it added and nobody
        # later wanted it removed; symmetrically for removals. This collapses
        # add-then-remove pairs to a no-op. We can't reason about swap cases
        # (add-then-remove of one tag combined with remove-then-add of another)
        # without knowing Kuma's pre-prior-task state, so those merge to no-op too.
        all_added = prior_added | new_added
        all_removed = prior_removed | new_removed
        payload["added"] = sorted(all_added - all_removed)
        payload["removed"] = sorted(all_removed - all_added)
        return not payload["added"] and not payload["removed"]

    return False


def cancel_monitor_tasks(db, monitor_id: int) -> None:
    """Cancel all pending/failed tasks for a monitor before re-enqueueing."""
    from .models import KumaTask
    db.query(KumaTask).filter(
        KumaTask.monitor_id == monitor_id,
        KumaTask.status.in_(["pending", "failed"]),
    ).update({"status": "cancelled"}, synchronize_session=False)
    db.commit()


def process_kuma_tasks() -> None:
    """Process pending Kuma sync tasks. Runs in APScheduler thread pool."""
    from sqlalchemy import or_
    from .database import SessionLocal
    from .models import AppSettings, KumaTask

    global _last_run, _last_error

    db = SessionLocal()
    try:
        app_cfg = db.get(AppSettings, 1)
        if not app_cfg or not app_cfg.configured:
            return

        now = datetime.now(timezone.utc).replace(tzinfo=None)
        pending = (
            db.query(KumaTask)
            .filter(
                KumaTask.status == "pending",
                or_(KumaTask.next_retry_at == None, KumaTask.next_retry_at <= now),
            )
            .order_by(KumaTask.created_at)
            .limit(10)
            .all()
        )
        for task in pending:
            try:
                _run(task, app_cfg)
                task.status = "done"
            except Exception as exc:
                error_msg = f"{type(exc).__name__}: {exc}"[:1000] or type(exc).__name__
                logger.warning("Kuma task %d (%s) failed (attempt %d): %s: %s", task.id, task.task_type, task.retry_count + 1, type(exc).__name__, exc, exc_info=True)
                if task.retry_count < MAX_RETRIES:
                    task.retry_count += 1
                    task.status = "pending"
                    task.next_retry_at = datetime.now(timezone.utc).replace(tzinfo=None) + timedelta(seconds=BASE_RETRY_SECONDS * (2 ** (task.retry_count - 1)))
                    task.error = error_msg
                else:
                    task.status = "failed"
                    task.error = error_msg
            db.commit()
        cutoff = datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(hours=COMPLETED_TASK_TTL_HOURS)
        db.query(KumaTask).filter(
            KumaTask.status.in_(["done", "cancelled"]),
            KumaTask.created_at < cutoff,
        ).delete(synchronize_session=False)
        db.commit()
        with _status_lock:
            _last_run = datetime.now(timezone.utc).replace(tzinfo=None)
            _last_error = None
    except Exception as exc:
        with _status_lock:
            _last_run = datetime.now(timezone.utc).replace(tzinfo=None)
            _last_error = f"{type(exc).__name__}: {exc}"
        raise
    finally:
        db.close()


def _run(task, app_cfg) -> None:
    from .kuma import kuma_session, update_monitor, pause_monitor, resume_monitor, delete_monitor

    url = app_cfg.kuma_url
    user = app_cfg.kuma_username
    pw = app_cfg.kuma_password
    p = task.payload

    if task.task_type == "update_monitor":
        update_monitor(p["kuma_monitor_id"], url, user, pw, **p["fields"])
    elif task.task_type == "pause_monitor":
        pause_monitor(p["kuma_monitor_id"], url, user, pw)
    elif task.task_type == "resume_monitor":
        resume_monitor(p["kuma_monitor_id"], url, user, pw)
    elif task.task_type == "delete_monitor":
        try:
            delete_monitor(p["kuma_monitor_id"], url, user, pw)
        except Exception as exc:
            if "does not exist" in str(exc).lower() or "not found" in str(exc).lower():
                return  # already gone — treat as success
            raise
    elif task.task_type == "update_tags":
        with kuma_session(url, user, pw) as api:
            for tag_id in p.get("added", []):
                api.add_monitor_tag(tag_id=tag_id, monitor_id=p["kuma_monitor_id"])
            for tag_id in p.get("removed", []):
                try:
                    api.delete_monitor_tag(tag_id=tag_id, monitor_id=p["kuma_monitor_id"])
                except Exception as exc:
                    if "does not exist" in str(exc).lower() or "not found" in str(exc).lower():
                        continue
                    raise
    elif task.task_type == "create_tags":
        from .database import SessionLocal
        from .models import Monitor
        from .tag_cache import refresh as refresh_tag_cache
        db = SessionLocal()
        try:
            with kuma_session(url, user, pw) as api:
                for tag in p.get("tags", []):
                    try:
                        result = api.add_tag(name=tag["name"], color=tag["color"])
                    except Exception as exc:
                        logger.warning("Failed to create tag %r: %s: %s — skipping", tag.get("name"), type(exc).__name__, exc, exc_info=True)
                        continue
                    new_id = result["id"]
                    # Persist the new tag ID before attempting monitor association so it's
                    # never lost if association fails or the task errors out.
                    monitor = db.get(Monitor, p["monitor_id"])
                    if monitor:
                        current = list(monitor.tag_ids or [])
                        if new_id not in current:
                            current.append(new_id)
                            monitor.tag_ids = current
                            db.commit()
                    if p.get("kuma_monitor_id"):
                        try:
                            api.add_monitor_tag(tag_id=new_id, monitor_id=p["kuma_monitor_id"])
                        except Exception as exc:
                            logger.warning(
                                "Failed to associate tag %d with kuma monitor %d: %s: %s — "
                                "will be applied on next resync",
                                new_id, p["kuma_monitor_id"], type(exc).__name__, exc,
                                exc_info=True,
                            )
        finally:
            db.close()
        try:
            refresh_tag_cache()
        except Exception as exc:
            logger.warning("Tag cache refresh failed after create_tags: %s: %s", type(exc).__name__, exc, exc_info=True)
    elif task.task_type == "create_tag":
        from .tag_cache import refresh as refresh_tag_cache
        with kuma_session(url, user, pw) as api:
            api.add_tag(name=p["name"], color=p["color"])
        try:
            refresh_tag_cache()
        except Exception as exc:
            logger.warning("Tag cache refresh failed after create_tag: %s: %s", type(exc).__name__, exc, exc_info=True)
    elif task.task_type == "sync_monitor":
        pass  # resolved inline by checker.py; should not reach the queue processor
    else:
        raise ValueError(f"Unknown task type: {task.task_type}")
