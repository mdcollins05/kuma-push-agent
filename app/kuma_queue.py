import logging
import threading
from datetime import datetime, timedelta

COMPLETED_TASK_TTL_HOURS = 24

logger = logging.getLogger(__name__)

MAX_RETRIES = 3
RETRY_DELAY_SECONDS = 60

_status_lock = threading.Lock()
_last_run: datetime | None = None
_last_error: str | None = None


def processor_status() -> dict:
    return {
        "last_run": _last_run.isoformat() if _last_run else None,
        "last_error": _last_error,
    }


def enqueue(db, task_type: str, payload: dict, monitor_name: str = None, monitor_id: int = None) -> None:
    from .models import KumaTask
    db.add(KumaTask(
        task_type=task_type,
        payload=payload,
        monitor_name=monitor_name,
        monitor_id=monitor_id,
    ))
    db.commit()


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

        now = datetime.utcnow()
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
                logger.warning("Kuma task %d (%s) failed (attempt %d): %r", task.id, task.task_type, task.retry_count + 1, exc)
                if task.retry_count < MAX_RETRIES:
                    task.retry_count += 1
                    task.status = "pending"
                    task.next_retry_at = datetime.utcnow() + timedelta(seconds=RETRY_DELAY_SECONDS)
                    task.error = error_msg
                else:
                    task.status = "failed"
                    task.error = error_msg
            db.commit()
        cutoff = datetime.utcnow() - timedelta(hours=COMPLETED_TASK_TTL_HOURS)
        db.query(KumaTask).filter(
            KumaTask.status.in_(["done", "cancelled"]),
            KumaTask.created_at < cutoff,
        ).delete(synchronize_session=False)
        db.commit()
        with _status_lock:
            _last_run = datetime.utcnow()
            _last_error = None
    except Exception as exc:
        with _status_lock:
            _last_run = datetime.utcnow()
            _last_error = f"{type(exc).__name__}: {exc}"
        raise
    finally:
        db.close()


def _run(task, app_cfg) -> None:
    from .kuma import update_monitor, pause_monitor, resume_monitor, delete_monitor, add_monitor_tag, delete_monitor_tag, create_tag

    url = app_cfg.kuma_url
    user = app_cfg.kuma_username
    pw = app_cfg.kuma_password
    p = task.payload

    if task.task_type == "update":
        update_monitor(p["kuma_monitor_id"], url, user, pw, **p["fields"])
    elif task.task_type == "pause":
        pause_monitor(p["kuma_monitor_id"], url, user, pw)
    elif task.task_type == "resume":
        resume_monitor(p["kuma_monitor_id"], url, user, pw)
    elif task.task_type == "delete":
        try:
            delete_monitor(p["kuma_monitor_id"], url, user, pw)
        except Exception as exc:
            if "does not exist" in str(exc).lower() or "not found" in str(exc).lower():
                return  # already gone — treat as success
            raise
    elif task.task_type == "update_tags":
        for tag_id in p.get("added", []):
            add_monitor_tag(p["kuma_monitor_id"], tag_id, url, user, pw)
        for tag_id in p.get("removed", []):
            try:
                delete_monitor_tag(p["kuma_monitor_id"], tag_id, url, user, pw)
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
            for tag in p.get("tags", []):
                try:
                    result = create_tag(tag["name"], tag["color"], url, user, pw)
                except Exception as exc:
                    logger.warning("Failed to create tag %r: %s — skipping", tag.get("name"), exc)
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
                        add_monitor_tag(p["kuma_monitor_id"], new_id, url, user, pw)
                    except Exception as exc:
                        logger.warning(
                            "Failed to associate tag %d with kuma monitor %d: %s — "
                            "will be applied on next resync",
                            new_id, p["kuma_monitor_id"], exc,
                        )
        finally:
            db.close()
        try:
            refresh_tag_cache()
        except Exception as exc:
            logger.warning("Tag cache refresh failed after create_tags: %s", exc)
    else:
        raise ValueError(f"Unknown task type: {task.task_type}")
