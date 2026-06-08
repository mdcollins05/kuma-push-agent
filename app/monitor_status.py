from sqlalchemy.orm import Session

from .models import KumaTask, Monitor


def monitor_status_dict(m: Monitor, db: Session = None, tz: str = "UTC") -> dict:
    pending_jobs = 0
    failed_jobs = 0
    pending_create_tags = False
    if db is not None:
        from sqlalchemy import func
        rows = (
            db.query(KumaTask.status, func.count(KumaTask.id))
            .filter(KumaTask.monitor_id == m.id, KumaTask.status.in_(["pending", "failed"]))
            .group_by(KumaTask.status)
            .all()
        )
        counts = dict(rows)
        pending_jobs = counts.get("pending", 0)
        failed_jobs = counts.get("failed", 0)
        pending_create_tags = db.query(KumaTask).filter(
            KumaTask.monitor_id == m.id,
            KumaTask.task_type == "create_tags",
            KumaTask.status.in_(["pending", "failed"]),
        ).first() is not None

    from .templates import _local_dt
    return {
        "id": m.id,
        "enabled": m.enabled,
        "last_status": m.last_status,
        "last_check_time": _local_dt(m.last_check_time, tz),
        "last_response_ms": m.last_response_ms,
        "last_error": m.last_error,
        "kuma_synced": m.kuma_synced,
        "kuma_monitor_id": m.kuma_monitor_id,
        "pending_jobs": pending_jobs,
        "failed_jobs": failed_jobs,
        "pending_create_tags": pending_create_tags,
    }
