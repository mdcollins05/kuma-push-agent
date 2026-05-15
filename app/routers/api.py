from typing import List, Optional

from fastapi import APIRouter, Depends, HTTPException
from fastapi.concurrency import run_in_threadpool
from sqlalchemy.orm import Session

from ..dependencies import get_db, require_api_key
from ..models import AppSettings, Monitor
from ..schemas import MonitorCreate, MonitorResponse, MonitorUpdate
from ..scheduler import add_check_job, remove_check_job

router = APIRouter(prefix="/api/v1", dependencies=[Depends(require_api_key)])


@router.get("/monitors", response_model=List[MonitorResponse])
def list_monitors(db: Session = Depends(get_db)):
    monitors = db.query(Monitor).order_by(Monitor.name).all()
    return [_to_response(m) for m in monitors]


@router.get("/monitors/{monitor_id}", response_model=MonitorResponse)
def get_monitor(monitor_id: int, db: Session = Depends(get_db)):
    monitor = db.get(Monitor, monitor_id)
    if not monitor:
        raise HTTPException(status_code=404, detail="Monitor not found")
    return _to_response(monitor)


@router.post("/monitors", response_model=MonitorResponse, status_code=201)
async def create_monitor(payload: MonitorCreate, db: Session = Depends(get_db)):
    monitor = Monitor(
        name=payload.name,
        url=payload.url,
        interval=payload.interval,
        expected_codes=payload.expected_codes,
        keyword=payload.keyword,
        verify_ssl=payload.verify_ssl,
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


@router.put("/monitors/{monitor_id}", response_model=MonitorResponse)
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
    db.commit()

    if interval_changed:
        add_check_job(monitor.id, monitor.interval)

    return _to_response(monitor)


@router.delete("/monitors/{monitor_id}", status_code=204)
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
        kuma_synced=m.kuma_synced,
        last_status=m.last_status,
        last_check_time=m.last_check_time.isoformat() if m.last_check_time else None,
        last_response_ms=m.last_response_ms,
        last_error=m.last_error,
        enabled=m.enabled,
    )
