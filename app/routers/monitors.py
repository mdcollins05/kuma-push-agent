import json
import logging
from typing import List, Optional

from fastapi import APIRouter, Depends, Form, Request
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import JSONResponse, RedirectResponse
from sqlalchemy.orm import Session

from .. import kuma as kuma_module
from ..dependencies import get_db, require_auth
from ..models import AppSettings, KumaTask, Monitor
from ..monitor_status import monitor_status_dict, task_status_counts
from ..scheduler import add_check_job, pause_check_job, remove_check_job, resume_check_job
from ..templates import templates

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/monitors")

VALID_HTTP_METHODS = {"GET", "POST", "PUT", "PATCH", "DELETE", "HEAD"}


def _parse_codes(raw: str) -> list[int]:
    try:
        return [int(c.strip()) for c in raw.split(",") if c.strip()]
    except ValueError:
        return [200]


def _parse_headers(raw: str) -> tuple[dict | None, str | None]:
    """Parse the headers textarea. Returns (headers_dict, error_msg). Empty input → ({}, None)."""
    raw = (raw or "").strip()
    if not raw:
        return {}, None
    try:
        data = json.loads(raw)
    except json.JSONDecodeError as exc:
        return None, f"Headers must be valid JSON: {exc.msg}"
    if not isinstance(data, dict):
        return None, "Headers must be a JSON object (e.g. {\"X-Header\": \"value\"})"
    out = {}
    for k, v in data.items():
        if not isinstance(k, str) or not isinstance(v, str):
            return None, "Headers JSON keys and values must both be strings"
        out[k] = v
    return out, None


def _build_http_config(
    url: str, method: str, headers_json: str, body: str,
    expected_codes_raw: str, keyword: Optional[str], max_response_ms: Optional[int],
    verify_ssl_flag: Optional[str],
) -> tuple[dict | None, str | None]:
    """Assemble the HTTP config dict from form fields. Returns (config, error_msg)."""
    method = (method or "GET").upper()
    if method not in VALID_HTTP_METHODS:
        return None, f"Unsupported HTTP method: {method}"
    headers, err = _parse_headers(headers_json)
    if err:
        return None, err
    return {
        "type": "http",
        "url": url,
        "method": method,
        "headers": headers,
        "body": body if body and body.strip() else None,
        "expected_codes": _parse_codes(expected_codes_raw),
        "keyword": keyword or None,
        "max_response_ms": max_response_ms,
        "verify_ssl": verify_ssl_flag is not None,
    }, None


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


def _fetch_groups() -> list:
    from ..group_cache import get
    return get()





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


@router.post("/notifications/refresh")
async def notifications_refresh(
    user: str = Depends(require_auth),
):
    from ..notification_cache import refresh as refresh_notification_cache
    try:
        await run_in_threadpool(refresh_notification_cache, True)
    except Exception as exc:
        return JSONResponse({"ok": False, "error": str(exc)}, status_code=502)
    return JSONResponse({"ok": True})


@router.get("/statuses")
async def monitor_statuses(
    db: Session = Depends(get_db),
    user: str = Depends(require_auth),
):
    cfg = db.get(AppSettings, 1)
    tz = (cfg.timezone or "UTC") if cfg else "UTC"
    monitors = db.query(Monitor).all()

    task_counts = task_status_counts(db)

    pending_create_tag_ids = {
        monitor_id for (monitor_id,) in (
            db.query(KumaTask.monitor_id)
            .filter(
                KumaTask.monitor_id.isnot(None),
                KumaTask.task_type == "create_tags",
                KumaTask.status.in_(["pending", "failed"]),
            )
            .distinct()
            .all()
        )
    }

    result = []
    for m in monitors:
        d = monitor_status_dict(m, tz=tz)
        counts = task_counts.get(m.id, {})
        d["pending_tasks"] = counts.get("pending", 0)
        d["failed_tasks"] = counts.get("failed", 0)
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
    d = monitor_status_dict(monitor, db, tz=tz)
    d["pending_tasks"] = d.pop("pending_jobs")
    d["failed_tasks"] = d.pop("failed_jobs")
    return JSONResponse(d)


@router.get("/new")
async def monitor_new_get(
    request: Request,
    db: Session = Depends(get_db),
    user: str = Depends(require_auth),
):
    kuma_url, _, __ = _kuma_creds(db)
    notifications = _fetch_notifications()
    available_tags = _fetch_tags()
    groups = _fetch_groups()
    return templates.TemplateResponse(request, "monitor_form.html", {
        "monitor": None, "user": user, "error": None,
        "notifications": notifications, "available_tags": available_tags,
        "groups": groups,
        "kuma_configured": bool(kuma_url), "selected_tag_ids": [],
    })


@router.post("/new")
async def monitor_new_post(
    request: Request,
    name: str = Form(...),
    url: str = Form(...),
    interval: int = Form(60),
    method: str = Form("GET"),
    headers_json: str = Form(""),
    body: str = Form(""),
    expected_codes_raw: str = Form("200"),
    keyword: Optional[str] = Form(None),
    max_response_ms: Optional[int] = Form(None),
    notification_ids: List[int] = Form(default=[]),
    tag_ids: List[int] = Form(default=[]),
    new_tag_names: List[str] = Form(default=[]),
    new_tag_colors: List[str] = Form(default=[]),
    kuma_group_id: Optional[int] = Form(None),
    verify_ssl: Optional[str] = Form(None),  # checkbox: present="true", absent=None
    db: Session = Depends(get_db),
    user: str = Depends(require_auth),
):
    kuma_url, kuma_user, kuma_pass = _kuma_creds(db)

    def _error(msg: str, status: int = 400):
        available_tags = _fetch_tags()
        return templates.TemplateResponse(
            request, "monitor_form.html",
            {"monitor": None, "user": user, "error": msg,
             "notifications": _fetch_notifications(), "available_tags": available_tags,
             "groups": _fetch_groups(),
             "kuma_configured": bool(kuma_url), "selected_tag_ids": tag_ids,
             "form_values": {
                 "name": name, "url": url, "interval": interval, "method": method,
                 "headers_json": headers_json, "body": body,
                 "expected_codes_raw": expected_codes_raw, "keyword": keyword,
                 "max_response_ms": max_response_ms, "verify_ssl": verify_ssl is not None,
                 "kuma_group_id": kuma_group_id,
             }},
            status_code=status,
        )

    if interval < 20:
        return _error("Interval must be at least 20 seconds.")

    config, err = _build_http_config(
        url=url, method=method, headers_json=headers_json, body=body,
        expected_codes_raw=expected_codes_raw, keyword=keyword,
        max_response_ms=max_response_ms, verify_ssl_flag=verify_ssl,
    )
    if err:
        return _error(err)

    new_pairs = [(n.strip(), c) for n, c in zip(new_tag_names, new_tag_colors) if n.strip()]
    new_names = [n for n, _ in new_pairs]
    new_colors = [c for _, c in new_pairs]

    monitor = Monitor(
        name=name,
        interval=interval,
        config=config,
        notification_ids=notification_ids or [],
        tag_ids=tag_ids or [],
        kuma_group_id=kuma_group_id,
    )
    db.add(monitor)
    db.commit()
    db.refresh(monitor)

    if new_names:
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
    groups = _fetch_groups()
    task_counts = task_status_counts(db, [monitor_id]).get(monitor_id, {})
    pending_new_tags = []
    for task in (
        db.query(KumaTask)
        .filter(KumaTask.monitor_id == monitor_id, KumaTask.task_type == "create_tags",
                KumaTask.status.in_(["pending", "failed"]))
        .all()
    ):
        for t in (task.payload or {}).get("tags", []):
            pending_new_tags.append({"name": t["name"], "color": t["color"],
                                     "failed": task.status == "failed"})

    selected_tag_ids = monitor.tag_ids or []

    cfg = db.get(AppSettings, 1)
    return templates.TemplateResponse(request, "monitor_form.html", {
        "monitor": monitor, "user": user, "error": None,
        "notifications": notifications, "kuma_configured": bool(kuma_url),
        "available_tags": available_tags,
        "groups": groups,
        "selected_tag_ids": selected_tag_ids,
        "pending_new_tags": pending_new_tags,
        "pending_tasks": task_counts.get("pending", 0),
        "failed_tasks": task_counts.get("failed", 0),
        "timezone": (cfg.timezone or "UTC") if cfg else "UTC",
    })


@router.post("/{monitor_id}/edit")
async def monitor_edit_post(
    monitor_id: int,
    request: Request,
    name: str = Form(...),
    url: str = Form(...),
    interval: int = Form(60),
    method: str = Form("GET"),
    headers_json: str = Form(""),
    body: str = Form(""),
    expected_codes_raw: str = Form("200"),
    keyword: Optional[str] = Form(None),
    max_response_ms: Optional[int] = Form(None),
    notification_ids: List[int] = Form(default=[]),
    tag_ids: List[int] = Form(default=[]),
    new_tag_names: List[str] = Form(default=[]),
    new_tag_colors: List[str] = Form(default=[]),
    kuma_group_id: Optional[int] = Form(None),
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
             "groups": _fetch_groups(),
             "pending_tasks": 0, "failed_tasks": 0,
             "timezone": (cfg.timezone or "UTC") if cfg else "UTC",
             "form_values": {
                 "name": name, "url": url, "interval": interval, "method": method,
                 "headers_json": headers_json, "body": body,
                 "expected_codes_raw": expected_codes_raw, "keyword": keyword,
                 "max_response_ms": max_response_ms, "verify_ssl": verify_ssl is not None,
                 "kuma_group_id": kuma_group_id,
             }},
            status_code=status,
        )

    if interval < 20:
        return _error("Interval must be at least 20 seconds.")

    config, err = _build_http_config(
        url=url, method=method, headers_json=headers_json, body=body,
        expected_codes_raw=expected_codes_raw, keyword=keyword,
        max_response_ms=max_response_ms, verify_ssl_flag=verify_ssl,
    )
    if err:
        return _error(err)

    new_pairs = [(n.strip(), c) for n, c in zip(new_tag_names, new_tag_colors) if n.strip()]
    new_names = [n for n, _ in new_pairs]
    new_colors = [c for _, c in new_pairs]

    old_tag_ids = set(monitor.tag_ids or [])
    new_tag_ids = set(tag_ids)
    tags_changed = old_tag_ids != new_tag_ids

    name_changed = monitor.name != name
    interval_changed = monitor.interval != interval
    notifications_changed = sorted(monitor.notification_ids or []) != sorted(notification_ids)
    group_changed = monitor.kuma_group_id != kuma_group_id

    monitor.name = name
    monitor.interval = interval
    monitor.config = config
    monitor.notification_ids = notification_ids
    monitor.tag_ids = list(new_tag_ids)
    monitor.kuma_group_id = kuma_group_id
    db.commit()

    if monitor.kuma_synced and monitor.kuma_monitor_id and (name_changed or interval_changed or notifications_changed or group_changed):
        from ..kuma_queue import enqueue
        fields = {}
        if name_changed:
            fields["name"] = name
        if interval_changed:
            fields["interval"] = interval + max(30, interval // 2)
        if notifications_changed:
            fields["notificationIDList"] = {str(nid): True for nid in notification_ids}
        if group_changed:
            fields["parent"] = kuma_group_id
        enqueue(db, "update_monitor", {"kuma_monitor_id": monitor.kuma_monitor_id, "fields": fields},
                monitor.name, monitor_id=monitor_id)

    if monitor.kuma_synced and monitor.kuma_monitor_id and tags_changed:
        from ..kuma_queue import enqueue as kuma_enqueue
        kuma_enqueue(db, "update_tags", {
            "kuma_monitor_id": monitor.kuma_monitor_id,
            "added": list(new_tag_ids - old_tag_ids),
            "removed": list(old_tag_ids - new_tag_ids),
        }, monitor.name, monitor_id=monitor_id)

    if new_names:
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

    from ..kuma_queue import enqueue
    fields = {
        "name": monitor.name,
        "interval": monitor.interval + max(30, monitor.interval // 2),
    }
    if monitor.notification_ids:
        fields["notificationIDList"] = {str(nid): True for nid in monitor.notification_ids}
    # enqueue() coalesces — the new update_monitor cancels any prior pending one,
    # so the explicit cancel_monitor_tasks() the old code did is no longer needed.
    enqueue(db, "update_monitor", {"kuma_monitor_id": monitor.kuma_monitor_id, "fields": fields},
            monitor.name, monitor_id=monitor_id)

    return RedirectResponse(f"/monitors/{monitor_id}/edit", status_code=302)


@router.post("/{monitor_id}/recreate-kuma")
async def monitor_recreate_kuma(
    monitor_id: int,
    db: Session = Depends(get_db),
    user: str = Depends(require_auth),
):
    monitor = db.get(Monitor, monitor_id)
    if monitor and (monitor.kuma_monitor_id or monitor.kuma_synced):
        from ..recreate import reset_and_reschedule
        reset_and_reschedule(monitor, db)
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

    if monitor.kuma_monitor_id:
        from ..kuma_queue import enqueue
        enqueue(db, "pause_monitor", {"kuma_monitor_id": monitor.kuma_monitor_id}, monitor.name, monitor_id=monitor_id)

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

    if monitor.kuma_monitor_id:
        from ..kuma_queue import enqueue
        enqueue(db, "resume_monitor", {"kuma_monitor_id": monitor.kuma_monitor_id}, monitor.name, monitor_id=monitor_id)

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
        from ..kuma_queue import enqueue
        enqueue(db, "delete_monitor", {"kuma_monitor_id": monitor.kuma_monitor_id}, monitor.name, monitor_id=monitor_id)

    remove_check_job(monitor_id)
    db.delete(monitor)
    db.commit()
    return RedirectResponse("/", status_code=302)
