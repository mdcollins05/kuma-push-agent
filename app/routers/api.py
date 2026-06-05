import uuid
from typing import List, Optional

from fastapi import APIRouter, Depends, HTTPException
from fastapi.concurrency import run_in_threadpool
from sqlalchemy.orm import Session

from ..dependencies import get_db, require_api_key
from ..models import AppSettings, Monitor
from ..monitor_status import monitor_status_dict
from ..notification_cache import get as get_notifications
from ..passwords import hash_password
from ..schemas import (
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
    status_code=201,
    tags=["Tags"],
    summary="Create a tag",
    description="Creates a new tag in Uptime Kuma and refreshes the local tag cache. Requires Kuma to be configured.",
    responses={503: {"description": "Uptime Kuma not configured"}},
)
async def create_tag(payload: TagCreate, db: Session = Depends(get_db)):
    from ..kuma import create_tag as kuma_create_tag
    from ..tag_cache import refresh as refresh_tags
    cfg = db.get(AppSettings, 1)
    if not cfg or not cfg.configured:
        raise HTTPException(status_code=503, detail="Uptime Kuma is not configured")
    result = await run_in_threadpool(
        kuma_create_tag, payload.name, payload.color,
        cfg.kuma_url, cfg.kuma_username, cfg.kuma_password,
    )
    try:
        await run_in_threadpool(refresh_tags)
    except Exception:
        pass
    return TagResponse(id=result["id"], name=result["name"], color=result["color"])


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
    cfg = db.get(AppSettings, 1)
    tz = (cfg.timezone or "UTC") if cfg else "UTC"
    monitors = db.query(Monitor).order_by(Monitor.name).all()
    return [monitor_status_dict(m, db=db, tz=tz) for m in monitors]


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
    description="Creates a new health-check monitor and schedules it immediately. Also provisions a Push monitor in Uptime Kuma if configured.",
)
async def create_monitor(payload: MonitorCreate, db: Session = Depends(get_db)):
    monitor = Monitor(
        name=payload.name,
        url=payload.url,
        interval=payload.interval,
        expected_codes=payload.expected_codes,
        keyword=payload.keyword,
        verify_ssl=payload.verify_ssl,
        tag_ids=payload.tag_ids,
        notification_ids=payload.notification_ids,
    )
    db.add(monitor)
    db.commit()
    db.refresh(monitor)

    app_cfg = db.get(AppSettings, 1)
    if app_cfg and app_cfg.configured and app_cfg.kuma_url:
        try:
            from ..kuma import create_push_monitor
            kuma_id, push_token = await run_in_threadpool(
                create_push_monitor,
                monitor.name, monitor.interval,
                app_cfg.kuma_url, app_cfg.kuma_username, app_cfg.kuma_password,
            )
            monitor.kuma_monitor_id = kuma_id
            monitor.push_token = push_token
            monitor.kuma_synced = True
            db.commit()
        except Exception:
            pass

    add_check_job(monitor.id, monitor.interval)
    return _to_response(monitor)


@router.put(
    "/monitors/{monitor_id}",
    response_model=MonitorResponse,
    tags=["Monitors"],
    summary="Update a monitor",
    description="Replaces all fields of an existing monitor. Reschedules the check job if the interval changed.",
    responses=_404,
)
def update_monitor(monitor_id: int, payload: MonitorUpdate, db: Session = Depends(get_db)):
    monitor = db.get(Monitor, monitor_id)
    if not monitor:
        raise HTTPException(status_code=404, detail="Monitor not found")

    interval_changed = monitor.interval != payload.interval
    monitor.name = payload.name
    monitor.url = payload.url
    monitor.interval = payload.interval
    monitor.expected_codes = payload.expected_codes
    monitor.keyword = payload.keyword
    monitor.verify_ssl = payload.verify_ssl
    monitor.tag_ids = payload.tag_ids
    monitor.notification_ids = payload.notification_ids
    db.commit()

    if interval_changed:
        add_check_job(monitor.id, monitor.interval)

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
    remove_check_job(monitor_id)
    db.delete(monitor)
    db.commit()


def _to_response(m: Monitor) -> MonitorResponse:
    return MonitorResponse(
        id=m.id,
        name=m.name,
        url=m.url,
        interval=m.interval,
        expected_codes=m.expected_codes or [200],
        keyword=m.keyword,
        verify_ssl=m.verify_ssl,
        tag_ids=m.tag_ids or [],
        notification_ids=m.notification_ids or [],
        kuma_synced=m.kuma_synced,
        last_status=m.last_status,
        last_check_time=m.last_check_time.isoformat() if m.last_check_time else None,
        last_response_ms=m.last_response_ms,
        last_error=m.last_error,
        enabled=m.enabled,
    )
