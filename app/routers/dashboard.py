from fastapi import APIRouter, Depends, Request
from sqlalchemy.orm import Session

from sqlalchemy import func

from ..dependencies import get_db, require_auth
from ..models import AppSettings, KumaJob, Monitor
from ..templates import templates

router = APIRouter()


@router.get("/")
async def dashboard(
    request: Request,
    db: Session = Depends(get_db),
    user: str = Depends(require_auth),
):
    monitors = db.query(Monitor).order_by(Monitor.name).all()
    app_settings = db.get(AppSettings, 1)
    kuma_configured = bool(app_settings and app_settings.configured)

    # {monitor_id: {"pending": n, "failed": n}}
    job_rows = (
        db.query(KumaJob.monitor_id, KumaJob.status, func.count(KumaJob.id))
        .filter(KumaJob.monitor_id.isnot(None), KumaJob.status.in_(["pending", "failed"]))
        .group_by(KumaJob.monitor_id, KumaJob.status)
        .all()
    )
    job_counts: dict[int, dict] = {}
    for mid, status, count in job_rows:
        job_counts.setdefault(mid, {"pending": 0, "failed": 0})[status] = count

    return templates.TemplateResponse(request, "dashboard.html", {
        "monitors": monitors,
        "user": user,
        "kuma_configured": kuma_configured,
        "job_counts": job_counts,
        "timezone": (app_settings.timezone or "UTC") if app_settings else "UTC",
    })
