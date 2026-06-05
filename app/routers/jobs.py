from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse
from sqlalchemy.orm import Session

from ..dependencies import get_db, require_auth
from ..models import AppSettings, KumaJob
from ..templates import templates

router = APIRouter(prefix="/jobs")


@router.get("")
async def jobs_page(
    request: Request,
    db: Session = Depends(get_db),
    user: str = Depends(require_auth),
):
    cfg = db.get(AppSettings, 1)
    tz = (cfg.timezone or "UTC") if cfg else "UTC"
    jobs = (
        db.query(KumaJob)
        .order_by(KumaJob.created_at.desc())
        .limit(200)
        .all()
    )
    return templates.TemplateResponse(request, "jobs.html", {
        "user": user,
        "jobs": jobs,
        "tz": tz,
    })


@router.get("/data")
async def jobs_data(
    db: Session = Depends(get_db),
    user: str = Depends(require_auth),
):
    jobs = (
        db.query(KumaJob)
        .order_by(KumaJob.created_at.desc())
        .limit(200)
        .all()
    )
    return JSONResponse({
        "jobs": [
            {
                "id": j.id,
                "job_type": j.job_type,
                "monitor_id": j.monitor_id,
                "monitor_name": j.monitor_name,
                "status": j.status,
                "error": j.error,
                "retry_count": j.retry_count or 0,
                "created_at": j.created_at.isoformat() if j.created_at else None,
            }
            for j in jobs
        ],
    })


@router.get("/status")
async def jobs_status(
    db: Session = Depends(get_db),
    user: str = Depends(require_auth),
):
    jobs = (
        db.query(KumaJob)
        .order_by(KumaJob.created_at.desc())
        .limit(20)
        .all()
    )
    return JSONResponse({
        "pending": sum(1 for j in jobs if j.status == "pending"),
        "failed": sum(1 for j in jobs if j.status == "failed"),
        "jobs": [
            {
                "id": j.id,
                "type": j.job_type,
                "monitor_id": j.monitor_id,
                "monitor_name": j.monitor_name,
                "status": j.status,
                "error": j.error,
                "created_at": j.created_at.strftime("%H:%M:%S"),
            }
            for j in jobs
        ],
    })
