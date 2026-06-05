from fastapi import APIRouter, Depends, Request
from sqlalchemy.orm import Session

from sqlalchemy import func

from ..dependencies import get_db, require_auth
from ..models import AppSettings, KumaTask, Monitor
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
    task_rows = (
        db.query(KumaTask.monitor_id, KumaTask.status, func.count(KumaTask.id))
        .filter(KumaTask.monitor_id.isnot(None), KumaTask.status.in_(["pending", "failed"]))
        .group_by(KumaTask.monitor_id, KumaTask.status)
        .all()
    )
    task_counts: dict[int, dict] = {}
    for mid, status, count in task_rows:
        task_counts.setdefault(mid, {"pending": 0, "failed": 0})[status] = count

    return templates.TemplateResponse(request, "dashboard.html", {
        "monitors": monitors,
        "user": user,
        "kuma_configured": kuma_configured,
        "task_counts": task_counts,
        "timezone": (app_settings.timezone or "UTC") if app_settings else "UTC",
    })
