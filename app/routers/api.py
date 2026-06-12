import uuid
from typing import List, Optional

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from ..dependencies import get_db, require_api_key
from ..group_cache import get as get_groups
from ..models import AppSettings, KumaTask, Monitor
from ..monitor_status import monitor_status_dict
from ..notification_cache import get as get_notifications
from ..passwords import hash_password
from ..schemas import (
    GroupResponse,
    KumaSettingsRequest, KumaSettingsResponse,
    MonitorCreate, MonitorResponse, MonitorStatus, MonitorUpdate,
    NotificationResponse,
    SetupRequest, SetupResponse,
    TagCreate, TagResponse,
)
from ..scheduler import add_check_job, remove_check_job
from ..tag_cache import get as get_tags
from ..timezones import TIMEZONE_NAMES

# Unauthenticated — only hosts bootstrap endpoints
public_router = APIRouter(prefix="/api/v1")

# All routes below require X-API-Key
router = APIRouter(prefix="/api/v1", dependencies=[Depends(require_api_key)])

_404 = {404: {"description": "Monitor not found"}}


@public_router.post(
    "/setup",
    response_model=SetupResponse,
    tags=["Setup"],
    summary="Bootstrap the application",
    description=(
        "Creates the initial admin user and generates the API key. "
        "Returns `409` if the application has already been set up. "
        "No authentication required — this endpoint is only useful once."
    ),
    responses={409: {"description": "Already configured"}},
)
def api_setup(payload: SetupRequest, db: Session = Depends(get_db)):
    app_settings = db.get(AppSettings, 1)
    if app_settings and app_settings.ui_username:
        raise HTTPException(status_code=409, detail="Application is already configured")

    timezone = payload.timezone if payload.timezone in TIMEZONE_NAMES else "UTC"

    if not app_settings:
        app_settings = AppSettings(id=1)
        db.add(app_settings)

    api_key = str(uuid.uuid4())
    app_settings.ui_username = payload.username
    app_settings.ui_password_hash = hash_password(payload.password)
    app_settings.api_key = api_key
    app_settings.timezone = timezone
    db.commit()
    return SetupResponse(api_key=api_key)


@router.put(
    "/settings/kuma",
    response_model=KumaSettingsResponse,
    tags=["Settings"],
    summary="Configure Uptime Kuma credentials",
    description="Saves the Uptime Kuma server URL and login credentials. Omit `kuma_password` to keep the existing password.",
)
def configure_kuma(payload: KumaSettingsRequest, db: Session = Depends(get_db)):
    app_settings = db.get(AppSettings, 1)
    if not app_settings or not app_settings.ui_username:
        raise HTTPException(status_code=409, detail="Application is not set up yet")
    if not payload.kuma_url.strip() or not payload.kuma_username.strip():
        raise HTTPException(status_code=422, detail="kuma_url and kuma_username are required")
    app_settings.kuma_url = payload.kuma_url.rstrip("/")
    app_settings.kuma_username = payload.kuma_username
    if payload.kuma_password is not None:
        app_settings.kuma_password = payload.kuma_password
    app_settings.configured = True
    db.commit()
    return KumaSettingsResponse(
        configured=app_settings.configured,
        kuma_url=app_settings.kuma_url,
        kuma_username=app_settings.kuma_username,
    )


@router.get(
    "/tags",
    response_model=List[TagResponse],
    tags=["Tags"],
    summary="List all tags",
    description="Returns all Uptime Kuma tags from the local cache. The cache refreshes every 5 minutes.",
)
def list_tags():
    return get_tags()


@router.post(
    "/tags",
    response_model=TagResponse,
    status_code=202,
    tags=["Tags"],
    summary="Create a tag",
    description=(
        "Enqueues creation of a new tag in Uptime Kuma. Returns 202 immediately — "
        "`id` will be null until the queue processor runs (within ~10 seconds). "
        "Requires Kuma to be configured."
    ),
    responses={503: {"description": "Uptime Kuma not configured"}},
)
def create_tag(payload: TagCreate, db: Session = Depends(get_db)):
    cfg = db.get(AppSettings, 1)
    if not cfg or not cfg.configured:
        raise HTTPException(status_code=503, detail="Uptime Kuma is not configured")
    from ..kuma_queue import enqueue
    enqueue(db, "create_tag", {"name": payload.name, "color": payload.color})
    return TagResponse(id=None, name=payload.name, color=payload.color)


@router.get(
    "/notifications",
    response_model=List[NotificationResponse],
    tags=["Notifications"],
    summary="List all notification channels",
    description="Returns all Uptime Kuma notification channels from the local cache. The cache refreshes every 5 minutes.",
)
def list_notifications():
    return [NotificationResponse(id=n["id"], name=n["name"]) for n in get_notifications()]


@router.get(
    "/groups",
    response_model=List[GroupResponse],
    tags=["Groups"],
    summary="List all monitor groups",
    description="Returns all Uptime Kuma group monitors from the local cache. The cache refreshes every 5 minutes.",
)
def list_groups():
    return [GroupResponse(kuma_id=g["kuma_id"], name=g["name"]) for g in get_groups()]


@router.get(
    "/monitors",
    response_model=List[MonitorResponse],
    tags=["Monitors"],
    summary="List all monitors",
    description="Returns all monitors ordered alphabetically by name.",
)
def list_monitors(db: Session = Depends(get_db)):
    monitors = db.query(Monitor).order_by(Monitor.name).all()
    return [_to_response(m) for m in monitors]


# /monitors/statuses must be registered before /monitors/{monitor_id} so FastAPI's
# int coercion on {monitor_id} rejects the literal string "statuses" correctly.
@router.get(
    "/monitors/statuses",
    response_model=List[MonitorStatus],
    tags=["Monitors"],
    summary="List all monitor statuses",
    description="Returns live status fields for all monitors — last check time, last status, response time, error, and Kuma sync state.",
)
def list_monitor_statuses(db: Session = Depends(get_db)):
    from sqlalchemy import func
    cfg = db.get(AppSettings, 1)
    tz = (cfg.timezone or "UTC") if cfg else "UTC"
    monitors = db.query(Monitor).order_by(Monitor.name).all()

    task_counts: dict[int, dict[str, int]] = {}
    for monitor_id, status, count in (
        db.query(KumaTask.monitor_id, KumaTask.status, func.count(KumaTask.id))
        .filter(KumaTask.monitor_id.isnot(None), KumaTask.status.in_(["pending", "failed"]))
        .group_by(KumaTask.monitor_id, KumaTask.status)
        .all()
    ):
        task_counts.setdefault(monitor_id, {})[status] = count

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
        d["pending_jobs"] = counts.get("pending", 0)
        d["failed_jobs"] = counts.get("failed", 0)
        d["pending_create_tags"] = m.id in pending_create_tag_ids
        result.append(d)
    return result


@router.get(
    "/monitors/{monitor_id}/status",
    response_model=MonitorStatus,
    tags=["Monitors"],
    summary="Get a monitor's status",
    description="Returns live status fields for a single monitor.",
    responses=_404,
)
def get_monitor_status(monitor_id: int, db: Session = Depends(get_db)):
    monitor = db.get(Monitor, monitor_id)
    if not monitor:
        raise HTTPException(status_code=404, detail="Monitor not found")
    cfg = db.get(AppSettings, 1)
    tz = (cfg.timezone or "UTC") if cfg else "UTC"
    return monitor_status_dict(monitor, db=db, tz=tz)


@router.get(
    "/monitors/{monitor_id}",
    response_model=MonitorResponse,
    tags=["Monitors"],
    summary="Get a monitor",
    description="Returns a single monitor by its ID.",
    responses=_404,
)
def get_monitor(monitor_id: int, db: Session = Depends(get_db)):
    monitor = db.get(Monitor, monitor_id)
    if not monitor:
        raise HTTPException(status_code=404, detail="Monitor not found")
    return _to_response(monitor)


@router.post(
    "/monitors",
    response_model=MonitorResponse,
    status_code=201,
    tags=["Monitors"],
    summary="Create a monitor",
    description="Creates a new health-check monitor and schedules it immediately. Uptime Kuma provisioning happens lazily on the first check cycle.",
)
def create_monitor(payload: MonitorCreate, db: Session = Depends(get_db)):
    monitor = Monitor(
        name=payload.name,
        interval=payload.interval,
        config=payload.config.model_dump(),
        tag_ids=payload.tag_ids,
        notification_ids=payload.notification_ids,
        kuma_group_id=payload.kuma_group_id,
    )
    db.add(monitor)
    db.commit()
    db.refresh(monitor)
    add_check_job(monitor.id, monitor.interval)
    return _to_response(monitor)


@router.put(
    "/monitors/{monitor_id}",
    response_model=MonitorResponse,
    tags=["Monitors"],
    summary="Update a monitor",
    description="Replaces all fields of an existing monitor. Reschedules the check job if the interval changed. Enqueues Kuma sync jobs for any changed fields.",
    responses=_404,
)
def update_monitor(monitor_id: int, payload: MonitorUpdate, db: Session = Depends(get_db)):
    monitor = db.get(Monitor, monitor_id)
    if not monitor:
        raise HTTPException(status_code=404, detail="Monitor not found")

    old_tag_ids = set(monitor.tag_ids or [])
    new_tag_ids = set(payload.tag_ids or [])
    name_changed = monitor.name != payload.name
    interval_changed = monitor.interval != payload.interval
    notifications_changed = sorted(monitor.notification_ids or []) != sorted(payload.notification_ids or [])
    tags_changed = old_tag_ids != new_tag_ids
    group_changed = monitor.kuma_group_id != payload.kuma_group_id

    monitor.name = payload.name
    monitor.interval = payload.interval
    monitor.config = payload.config.model_dump()
    monitor.tag_ids = list(new_tag_ids)
    monitor.notification_ids = payload.notification_ids
    monitor.kuma_group_id = payload.kuma_group_id
    db.commit()

    app_cfg = db.get(AppSettings, 1)
    kuma_url = app_cfg.kuma_url if (app_cfg and app_cfg.configured) else None

    if monitor.kuma_synced and monitor.kuma_monitor_id and kuma_url:
        from ..kuma_queue import enqueue
        if name_changed or interval_changed or notifications_changed or group_changed:
            fields = {}
            if name_changed:
                fields["name"] = payload.name
            if interval_changed:
                fields["interval"] = payload.interval + max(30, payload.interval // 2)
            if notifications_changed:
                fields["notificationIDList"] = {str(nid): True for nid in (payload.notification_ids or [])}
            if group_changed:
                fields["parent"] = payload.kuma_group_id
            enqueue(db, "update_monitor", {"kuma_monitor_id": monitor.kuma_monitor_id, "fields": fields},
                    monitor.name, monitor_id=monitor_id)
        if tags_changed:
            enqueue(db, "update_tags", {
                "kuma_monitor_id": monitor.kuma_monitor_id,
                "added": list(new_tag_ids - old_tag_ids),
                "removed": list(old_tag_ids - new_tag_ids),
            }, monitor.name, monitor_id=monitor_id)

    if interval_changed:
        add_check_job(monitor.id, monitor.interval)

    return _to_response(monitor)


@router.post(
    "/monitors/{monitor_id}/recreate-kuma",
    response_model=MonitorResponse,
    tags=["Monitors"],
    summary="Recreate the Uptime Kuma monitor",
    description=(
        "Recovers from a Kuma monitor that was deleted server-side (push returning 404). "
        "Clears the cached `kuma_monitor_id` / `push_token` and resets `kuma_synced` so the "
        "next check cycle (within ~`interval` seconds) creates a fresh Kuma monitor and "
        "fetches a new push token. The old Kuma monitor ID is discarded without contacting "
        "Kuma — call this only when the monitor is genuinely missing on Kuma."
    ),
    responses={
        **_404,
        409: {"description": "Monitor has never been synced — nothing to recreate"},
    },
)
def recreate_kuma_monitor(monitor_id: int, db: Session = Depends(get_db)):
    monitor = db.get(Monitor, monitor_id)
    if not monitor:
        raise HTTPException(status_code=404, detail="Monitor not found")
    if not monitor.kuma_monitor_id and not monitor.kuma_synced:
        raise HTTPException(
            status_code=409,
            detail="Monitor has never been synced with Uptime Kuma — nothing to recreate",
        )
    monitor.kuma_monitor_id = None
    monitor.push_token = None
    monitor.kuma_synced = False
    monitor.kuma_missing = False
    db.commit()
    db.refresh(monitor)
    return _to_response(monitor)


@router.delete(
    "/monitors/{monitor_id}",
    status_code=204,
    tags=["Monitors"],
    summary="Delete a monitor",
    description="Deletes a monitor and stops its check job. Also queues deletion of the corresponding Uptime Kuma monitor if one exists.",
    responses=_404,
)
def delete_monitor_api(monitor_id: int, db: Session = Depends(get_db)):
    monitor = db.get(Monitor, monitor_id)
    if not monitor:
        raise HTTPException(status_code=404, detail="Monitor not found")

    if monitor.kuma_synced and monitor.kuma_monitor_id:
        app_cfg = db.get(AppSettings, 1)
        if app_cfg and app_cfg.kuma_url:
            from ..kuma_queue import enqueue
            enqueue(db, "delete_monitor", {"kuma_monitor_id": monitor.kuma_monitor_id},
                    monitor.name, monitor_id=monitor_id)

    remove_check_job(monitor_id)
    db.delete(monitor)
    db.commit()


def _to_response(m: Monitor) -> MonitorResponse:
    return MonitorResponse(
        id=m.id,
        name=m.name,
        interval=m.interval,
        config=m.config,
        tag_ids=m.tag_ids or [],
        notification_ids=m.notification_ids or [],
        kuma_group_id=m.kuma_group_id,
        kuma_synced=m.kuma_synced,
        kuma_missing=bool(m.kuma_missing),
        last_status=m.last_status,
        last_check_time=m.last_check_time.isoformat() if m.last_check_time else None,
        last_response_ms=m.last_response_ms,
        last_error=m.last_error,
        enabled=m.enabled,
    )
