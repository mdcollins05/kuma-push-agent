import logging
from typing import List, Optional

from fastapi import APIRouter, Depends, Form, Request
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import JSONResponse, RedirectResponse
from sqlalchemy.orm import Session

from .. import kuma as kuma_module
from ..dependencies import get_db, require_auth
from ..models import AppSettings, KumaJob, Monitor
from ..scheduler import add_check_job, pause_check_job, remove_check_job, resume_check_job
from ..templates import templates

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/monitors")


def _parse_codes(raw: str) -> list[int]:
    try:
        return [int(c.strip()) for c in raw.split(",") if c.strip()]
    except ValueError:
        return [200]


def _kuma_creds(db: Session):
    s = db.get(AppSettings, 1)
    if s and s.configured:
        return s.kuma_url, s.kuma_username, s.kuma_password
    return None, None, None


def _fetch_notifications() -> list:
    from ..notification_cache import get
    return get()


def _fetch_tags() -> list:
    from ..tag_cache import get
    return get()


def _monitor_status_dict(m: Monitor, db: Session = None, tz: str = "UTC") -> dict:
    pending_jobs = 0
    failed_jobs = 0
    pending_create_tags = False
    if db is not None:
        from sqlalchemy import func
        rows = (
            db.query(KumaJob.status, func.count(KumaJob.id))
            .filter(KumaJob.monitor_id == m.id, KumaJob.status.in_(["pending", "failed"]))
            .group_by(KumaJob.status)
            .all()
        )
        counts = dict(rows)
        pending_jobs = counts.get("pending", 0)
        failed_jobs = counts.get("failed", 0)
        pending_create_tags = db.query(KumaJob).filter(
            KumaJob.monitor_id == m.id,
            KumaJob.job_type == "create_tags",
            KumaJob.status.in_(["pending", "failed"]),
        ).first() is not None

    from ..templates import _local_dt
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


@router.post("/tags/refresh")
async def tags_refresh(
    user: str = Depends(require_auth),
):
    from ..tag_cache import refresh as refresh_tag_cache
    try:
        await run_in_threadpool(refresh_tag_cache, True)
    except Exception as exc:
        return JSONResponse({"ok": False, "error": str(exc)}, status_code=502)
    return JSONResponse({"ok": True})


@router.get("/statuses")
async def monitor_statuses(
    db: Session = Depends(get_db),
    user: str = Depends(require_auth),
):
    from sqlalchemy import func
    cfg = db.get(AppSettings, 1)
    tz = (cfg.timezone or "UTC") if cfg else "UTC"
    monitors = db.query(Monitor).all()

    job_counts: dict[int, dict[str, int]] = {}
    for monitor_id, status, count in (
        db.query(KumaJob.monitor_id, KumaJob.status, func.count(KumaJob.id))
        .filter(KumaJob.monitor_id.isnot(None), KumaJob.status.in_(["pending", "failed"]))
        .group_by(KumaJob.monitor_id, KumaJob.status)
        .all()
    ):
        job_counts.setdefault(monitor_id, {})[status] = count

    pending_create_tag_ids = {
        monitor_id for (monitor_id,) in (
            db.query(KumaJob.monitor_id)
            .filter(
                KumaJob.monitor_id.isnot(None),
                KumaJob.job_type == "create_tags",
                KumaJob.status.in_(["pending", "failed"]),
            )
            .distinct()
            .all()
        )
    }

    result = []
    for m in monitors:
        d = _monitor_status_dict(m, tz=tz)
        counts = job_counts.get(m.id, {})
        d["pending_jobs"] = counts.get("pending", 0)
        d["failed_jobs"] = counts.get("failed", 0)
        d["pending_create_tags"] = m.id in pending_create_tag_ids
        result.append(d)
    return JSONResponse(result)


@router.get("/{monitor_id}/status")
async def monitor_status(
    monitor_id: int,
    db: Session = Depends(get_db),
    user: str = Depends(require_auth),
):
    monitor = db.get(Monitor, monitor_id)
    if not monitor:
        return JSONResponse({"error": "not found"}, status_code=404)
    cfg = db.get(AppSettings, 1)
    tz = (cfg.timezone or "UTC") if cfg else "UTC"
    return JSONResponse(_monitor_status_dict(monitor, db, tz=tz))


@router.get("/new")
async def monitor_new_get(
    request: Request,
    db: Session = Depends(get_db),
    user: str = Depends(require_auth),
):
    kuma_url, _, __ = _kuma_creds(db)
    notifications = _fetch_notifications()
    available_tags = _fetch_tags()
    return templates.TemplateResponse(request, "monitor_form.html", {
        "monitor": None, "user": user, "error": None,
        "notifications": notifications, "available_tags": available_tags,
        "kuma_configured": bool(kuma_url), "selected_tag_ids": [],
    })


@router.post("/new")
async def monitor_new_post(
    request: Request,
    name: str = Form(...),
    url: str = Form(...),
    interval: int = Form(60),
    expected_codes_raw: str = Form("200"),
    keyword: Optional[str] = Form(None),
    max_response_ms: Optional[int] = Form(None),
    notification_ids: List[int] = Form(default=[]),
    tag_ids: List[int] = Form(default=[]),
    new_tag_names: List[str] = Form(default=[]),
    new_tag_colors: List[str] = Form(default=[]),
    verify_ssl: Optional[str] = Form(None),  # checkbox: present="true", absent=None
    db: Session = Depends(get_db),
    user: str = Depends(require_auth),
):
    expected_codes = _parse_codes(expected_codes_raw)
    kuma_url, kuma_user, kuma_pass = _kuma_creds(db)

    def _error(msg: str, status: int = 400):
        available_tags = _fetch_tags()
        return templates.TemplateResponse(
            request, "monitor_form.html",
            {"monitor": None, "user": user, "error": msg,
             "notifications": _fetch_notifications(), "available_tags": available_tags,
             "kuma_configured": bool(kuma_url), "selected_tag_ids": tag_ids},
            status_code=status,
        )

    if interval < 20:
        return _error("Interval must be at least 20 seconds.")

    new_pairs = [(n.strip(), c) for n, c in zip(new_tag_names, new_tag_colors) if n.strip()]
    new_names = [n for n, _ in new_pairs]
    new_colors = [c for _, c in new_pairs]

    monitor = Monitor(
        name=name,
        url=url,
        interval=interval,
        expected_codes=expected_codes,
        keyword=keyword or None,
        max_response_ms=max_response_ms,
        notification_ids=notification_ids or [],
        tag_ids=tag_ids or [],
        verify_ssl=verify_ssl is not None,
    )
    db.add(monitor)
    db.commit()
    db.refresh(monitor)

    if new_names and kuma_url:
        from ..kuma_queue import enqueue as kuma_enqueue
        kuma_enqueue(db, "create_tags", {
            "monitor_id": monitor.id,
            "kuma_monitor_id": monitor.kuma_monitor_id,
            "tags": [{"name": n, "color": c or "#059669"} for n, c in zip(new_names, new_colors)],
        }, monitor.name, monitor_id=monitor.id)

    add_check_job(monitor.id, monitor.interval)  # checker handles Kuma create lazily
    return RedirectResponse("/", status_code=302)


@router.get("/{monitor_id}/edit")
async def monitor_edit_get(
    monitor_id: int,
    request: Request,
    db: Session = Depends(get_db),
    user: str = Depends(require_auth),
):
    monitor = db.get(Monitor, monitor_id)
    if not monitor:
        return RedirectResponse("/", status_code=302)
    kuma_url, kuma_user, kuma_pass = _kuma_creds(db)
    notifications = _fetch_notifications()
    available_tags = _fetch_tags()
    job_counts = dict(
        db.query(KumaJob.status, __import__("sqlalchemy").func.count(KumaJob.id))
        .filter(KumaJob.monitor_id == monitor_id, KumaJob.status.in_(["pending", "failed"]))
        .group_by(KumaJob.status)
        .all()
    )
    pending_new_tags = []
    for job in (
        db.query(KumaJob)
        .filter(KumaJob.monitor_id == monitor_id, KumaJob.job_type == "create_tags",
                KumaJob.status.in_(["pending", "failed"]))
        .all()
    ):
        for t in (job.payload or {}).get("tags", []):
            pending_new_tags.append({"name": t["name"], "color": t["color"],
                                     "failed": job.status == "failed"})

    selected_tag_ids = monitor.tag_ids or []

    cfg = db.get(AppSettings, 1)
    return templates.TemplateResponse(request, "monitor_form.html", {
        "monitor": monitor, "user": user, "error": None,
        "notifications": notifications, "kuma_configured": bool(kuma_url),
        "available_tags": available_tags,
        "selected_tag_ids": selected_tag_ids,
"pending_new_tags": pending_new_tags,
        "pending_jobs": job_counts.get("pending", 0),
        "failed_jobs": job_counts.get("failed", 0),
        "timezone": (cfg.timezone or "UTC") if cfg else "UTC",
    })


@router.post("/{monitor_id}/edit")
async def monitor_edit_post(
    monitor_id: int,
    request: Request,
    name: str = Form(...),
    url: str = Form(...),
    interval: int = Form(60),
    expected_codes_raw: str = Form("200"),
    keyword: Optional[str] = Form(None),
    max_response_ms: Optional[int] = Form(None),
    notification_ids: List[int] = Form(default=[]),
    tag_ids: List[int] = Form(default=[]),
    new_tag_names: List[str] = Form(default=[]),
    new_tag_colors: List[str] = Form(default=[]),
    verify_ssl: Optional[str] = Form(None),  # checkbox: present="true", absent=None
    db: Session = Depends(get_db),
    user: str = Depends(require_auth),
):
    monitor = db.get(Monitor, monitor_id)
    if not monitor:
        return RedirectResponse("/", status_code=302)

    kuma_url, kuma_user, kuma_pass = _kuma_creds(db)

    def _error(msg: str, status: int = 400):
        available_tags = _fetch_tags()
        cfg = db.get(AppSettings, 1)
        return templates.TemplateResponse(
            request, "monitor_form.html",
            {"monitor": monitor, "user": user, "error": msg,
             "notifications": _fetch_notifications(), "kuma_configured": bool(kuma_url),
             "available_tags": available_tags, "selected_tag_ids": tag_ids,
             "pending_jobs": 0, "failed_jobs": 0,
             "timezone": (cfg.timezone or "UTC") if cfg else "UTC"},
            status_code=status,
        )

    if interval < 20:
        return _error("Interval must be at least 20 seconds.")

    new_pairs = [(n.strip(), c) for n, c in zip(new_tag_names, new_tag_colors) if n.strip()]
    new_names = [n for n, _ in new_pairs]
    new_colors = [c for _, c in new_pairs]

    old_tag_ids = set(monitor.tag_ids or [])
    new_tag_ids = set(tag_ids)
    tags_changed = old_tag_ids != new_tag_ids

    name_changed = monitor.name != name
    interval_changed = monitor.interval != interval
    notifications_changed = sorted(monitor.notification_ids or []) != sorted(notification_ids)

    monitor.name = name
    monitor.url = url
    monitor.interval = interval
    monitor.expected_codes = _parse_codes(expected_codes_raw)
    monitor.keyword = keyword or None
    monitor.max_response_ms = max_response_ms
    monitor.notification_ids = notification_ids
    monitor.tag_ids = list(new_tag_ids)
    monitor.verify_ssl = verify_ssl is not None
    db.commit()

    if monitor.kuma_synced and monitor.kuma_monitor_id and (name_changed or interval_changed or notifications_changed):
        if kuma_url:
            from ..kuma_queue import enqueue
            fields = {}
            if name_changed:
                fields["name"] = name
            if interval_changed:
                fields["interval"] = interval + max(30, interval // 2)
            if notifications_changed:
                fields["notificationIDList"] = {str(nid): True for nid in notification_ids}
            enqueue(db, "update", {"kuma_monitor_id": monitor.kuma_monitor_id, "fields": fields},
                    monitor.name, monitor_id=monitor_id)

    if monitor.kuma_synced and monitor.kuma_monitor_id and tags_changed and kuma_url:
        from ..kuma_queue import enqueue as kuma_enqueue
        kuma_enqueue(db, "update_tags", {
            "kuma_monitor_id": monitor.kuma_monitor_id,
            "added": list(new_tag_ids - old_tag_ids),
            "removed": list(old_tag_ids - new_tag_ids),
        }, monitor.name, monitor_id=monitor_id)

    if new_names and kuma_url:
        from ..kuma_queue import enqueue as kuma_enqueue
        kuma_enqueue(db, "create_tags", {
            "monitor_id": monitor_id,
            "kuma_monitor_id": monitor.kuma_monitor_id,
            "tags": [{"name": n, "color": c or "#059669"} for n, c in zip(new_names, new_colors)],
        }, monitor.name, monitor_id=monitor_id)

    if interval_changed:
        add_check_job(monitor.id, monitor.interval)

    return RedirectResponse(f"/monitors/{monitor_id}/edit", status_code=302)


@router.post("/{monitor_id}/resync")
async def monitor_resync(
    monitor_id: int,
    db: Session = Depends(get_db),
    user: str = Depends(require_auth),
):
    monitor = db.get(Monitor, monitor_id)
    if not monitor or not monitor.kuma_synced or not monitor.kuma_monitor_id:
        return RedirectResponse(f"/monitors/{monitor_id}/edit", status_code=302)

    kuma_url, _, __ = _kuma_creds(db)
    if kuma_url:
        from ..kuma_queue import cancel_monitor_jobs, enqueue
        cancel_monitor_jobs(db, monitor_id)
        fields = {
            "name": monitor.name,
            "interval": monitor.interval + max(30, monitor.interval // 2),
        }
        if monitor.notification_ids:
            fields["notificationIDList"] = {str(nid): True for nid in monitor.notification_ids}
        enqueue(db, "update", {"kuma_monitor_id": monitor.kuma_monitor_id, "fields": fields},
                monitor.name, monitor_id=monitor_id)

    return RedirectResponse(f"/monitors/{monitor_id}/edit", status_code=302)


@router.post("/{monitor_id}/orphan")
async def monitor_orphan(
    monitor_id: int,
    db: Session = Depends(get_db),
    user: str = Depends(require_auth),
):
    monitor = db.get(Monitor, monitor_id)
    if monitor:
        remove_check_job(monitor_id)
        db.delete(monitor)
        db.commit()
    return RedirectResponse("/", status_code=302)





@router.post("/{monitor_id}/pause")
async def monitor_pause(
    monitor_id: int,
    db: Session = Depends(get_db),
    user: str = Depends(require_auth),
):
    monitor = db.get(Monitor, monitor_id)
    if not monitor:
        return RedirectResponse("/", status_code=302)

    kuma_url, _, __ = _kuma_creds(db)
    if kuma_url and monitor.kuma_monitor_id:
        from ..kuma_queue import enqueue
        enqueue(db, "pause", {"kuma_monitor_id": monitor.kuma_monitor_id}, monitor.name, monitor_id=monitor_id)

    pause_check_job(monitor_id)
    monitor.enabled = False
    db.commit()
    return RedirectResponse(f"/monitors/{monitor_id}/edit", status_code=302)


@router.post("/{monitor_id}/resume")
async def monitor_resume(
    monitor_id: int,
    db: Session = Depends(get_db),
    user: str = Depends(require_auth),
):
    monitor = db.get(Monitor, monitor_id)
    if not monitor:
        return RedirectResponse("/", status_code=302)

    kuma_url, _, __ = _kuma_creds(db)
    if kuma_url and monitor.kuma_monitor_id:
        from ..kuma_queue import enqueue
        enqueue(db, "resume", {"kuma_monitor_id": monitor.kuma_monitor_id}, monitor.name, monitor_id=monitor_id)

    resume_check_job(monitor_id)
    monitor.enabled = True
    db.commit()
    return RedirectResponse(f"/monitors/{monitor_id}/edit", status_code=302)


@router.post("/{monitor_id}/delete")
async def monitor_delete(
    monitor_id: int,
    remove_from_kuma: Optional[str] = Form(None),
    db: Session = Depends(get_db),
    user: str = Depends(require_auth),
):
    monitor = db.get(Monitor, monitor_id)
    if not monitor:
        return RedirectResponse("/", status_code=302)

    if remove_from_kuma is not None and monitor.kuma_synced and monitor.kuma_monitor_id:
        kuma_url, _, __ = _kuma_creds(db)
        if kuma_url:
            from ..kuma_queue import enqueue
            enqueue(db, "delete", {"kuma_monitor_id": monitor.kuma_monitor_id}, monitor.name, monitor_id=monitor_id)

    remove_check_job(monitor_id)
    db.delete(monitor)
    db.commit()
    return RedirectResponse("/", status_code=302)
