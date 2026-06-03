import logging
from datetime import datetime, timedelta

COMPLETED_JOB_TTL_HOURS = 24

logger = logging.getLogger(__name__)

MAX_RETRIES = 3
RETRY_DELAY_SECONDS = 60


def enqueue(db, job_type: str, payload: dict, monitor_name: str = None, monitor_id: int = None) -> None:
    from .models import KumaJob
    db.add(KumaJob(
        job_type=job_type,
        payload=payload,
        monitor_name=monitor_name,
        monitor_id=monitor_id,
    ))
    db.commit()


def cancel_monitor_jobs(db, monitor_id: int) -> None:
    """Cancel all pending/failed jobs for a monitor before re-enqueueing."""
    from .models import KumaJob
    db.query(KumaJob).filter(
        KumaJob.monitor_id == monitor_id,
        KumaJob.status.in_(["pending", "failed"]),
    ).update({"status": "cancelled"}, synchronize_session=False)
    db.commit()


def process_kuma_jobs() -> None:
    """Process pending Kuma sync jobs. Runs in APScheduler thread pool."""
    from sqlalchemy import or_
    from .database import SessionLocal
    from .models import AppSettings, KumaJob

    db = SessionLocal()
    try:
        app_cfg = db.get(AppSettings, 1)
        if not app_cfg or not app_cfg.configured:
            return

        now = datetime.utcnow()
        pending = (
            db.query(KumaJob)
            .filter(
                KumaJob.status == "pending",
                or_(KumaJob.next_retry_at == None, KumaJob.next_retry_at <= now),
            )
            .order_by(KumaJob.created_at)
            .limit(10)
            .all()
        )
        for job in pending:
            try:
                _run(job, app_cfg)
                job.status = "done"
            except Exception as exc:
                error_msg = f"{type(exc).__name__}: {exc}"[:1000] or type(exc).__name__
                logger.warning("Kuma job %d (%s) failed (attempt %d): %r", job.id, job.job_type, job.retry_count + 1, exc)
                if job.retry_count < MAX_RETRIES:
                    job.retry_count += 1
                    job.status = "pending"
                    job.next_retry_at = datetime.utcnow() + timedelta(seconds=RETRY_DELAY_SECONDS)
                    job.error = error_msg
                else:
                    job.status = "failed"
                    job.error = error_msg
            db.commit()
        cutoff = datetime.utcnow() - timedelta(hours=COMPLETED_JOB_TTL_HOURS)
        db.query(KumaJob).filter(
            KumaJob.status.in_(["done", "cancelled"]),
            KumaJob.created_at < cutoff,
        ).delete(synchronize_session=False)
        db.commit()
    finally:
        db.close()


def _run(job, app_cfg) -> None:
    from .kuma import update_monitor, pause_monitor, resume_monitor, delete_monitor, add_monitor_tag, delete_monitor_tag, create_tag

    url = app_cfg.kuma_url
    user = app_cfg.kuma_username
    pw = app_cfg.kuma_password
    p = job.payload

    if job.job_type == "update":
        update_monitor(p["kuma_monitor_id"], url, user, pw, **p["fields"])
    elif job.job_type == "pause":
        pause_monitor(p["kuma_monitor_id"], url, user, pw)
    elif job.job_type == "resume":
        resume_monitor(p["kuma_monitor_id"], url, user, pw)
    elif job.job_type == "delete":
        try:
            delete_monitor(p["kuma_monitor_id"], url, user, pw)
        except Exception as exc:
            if "does not exist" in str(exc).lower() or "not found" in str(exc).lower():
                return  # already gone — treat as success
            raise
    elif job.job_type == "update_tags":
        for tag_id in p.get("added", []):
            add_monitor_tag(p["kuma_monitor_id"], tag_id, url, user, pw)
        for tag_id in p.get("removed", []):
            try:
                delete_monitor_tag(p["kuma_monitor_id"], tag_id, url, user, pw)
            except Exception as exc:
                if "does not exist" in str(exc).lower() or "not found" in str(exc).lower():
                    continue
                raise
    elif job.job_type == "create_tags":
        from .database import SessionLocal
        from .models import Monitor
        from .tag_cache import refresh as refresh_tag_cache
        db = SessionLocal()
        try:
            for tag in p.get("tags", []):
                result = create_tag(tag["name"], tag["color"], url, user, pw)
                new_id = result["id"]
                # Persist the new tag ID before attempting monitor association so it's
                # never lost if association fails or the job errors out.
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
        raise ValueError(f"Unknown job type: {job.job_type}")
