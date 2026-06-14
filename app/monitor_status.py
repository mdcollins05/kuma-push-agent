from typing import Optional

from sqlalchemy.orm import Session, aliased

from .models import KumaTask, Monitor


def task_status_counts(
    db: Session,
    monitor_ids: Optional[list[int]] = None,
) -> dict[int, dict[str, int]]:
    """Return {monitor_id: {"pending": n, "failed": m}} for monitor-attached tasks.

    A `failed` task is excluded when a later (higher-id) `done` task exists
    with the same (monitor_id, task_type). Rationale: a stale failure whose
    intent was later satisfied by a successful retry should not keep a
    sync-warning banner red forever — only currently-unresolved problems
    should count toward `failed`. Pending tasks are always counted as-is.

    `monitor_ids` narrows the query when only a subset is needed.
    """
    from sqlalchemy import func

    later_done = aliased(KumaTask)
    later_done_exists = (
        db.query(later_done.id)
        .filter(
            later_done.monitor_id == KumaTask.monitor_id,
            later_done.task_type == KumaTask.task_type,
            later_done.status == "done",
            later_done.id > KumaTask.id,
        )
        .exists()
    )

    pending_q = (
        db.query(KumaTask.monitor_id, func.count(KumaTask.id))
        .filter(KumaTask.monitor_id.isnot(None), KumaTask.status == "pending")
        .group_by(KumaTask.monitor_id)
    )
    failed_q = (
        db.query(KumaTask.monitor_id, func.count(KumaTask.id))
        .filter(
            KumaTask.monitor_id.isnot(None),
            KumaTask.status == "failed",
            ~later_done_exists,
        )
        .group_by(KumaTask.monitor_id)
    )

    if monitor_ids is not None:
        pending_q = pending_q.filter(KumaTask.monitor_id.in_(monitor_ids))
        failed_q = failed_q.filter(KumaTask.monitor_id.in_(monitor_ids))

    result: dict[int, dict[str, int]] = {}
    for mid, n in pending_q.all():
        result.setdefault(mid, {})["pending"] = n
    for mid, n in failed_q.all():
        result.setdefault(mid, {})["failed"] = n
    return result


def monitor_status_dict(m: Monitor, db: Session = None, tz: str = "UTC") -> dict:
    pending_jobs = 0
    failed_jobs = 0
    pending_create_tags = False
    if db is not None:
        counts = task_status_counts(db, [m.id]).get(m.id, {})
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
        "kuma_missing": bool(m.kuma_missing),
        "pending_jobs": pending_jobs,
        "failed_jobs": failed_jobs,
        "pending_create_tags": pending_create_tags,
    }
