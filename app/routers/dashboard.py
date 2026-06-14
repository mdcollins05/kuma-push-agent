from fastapi import APIRouter, Depends, Request
from sqlalchemy.orm import Session

from ..dependencies import get_db, require_auth
from ..models import AppSettings, Monitor
from ..monitor_status import task_status_counts
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

    task_counts = task_status_counts(db)

    return templates.TemplateResponse(request, "dashboard.html", {
        "monitors": monitors,
        "user": user,
        "kuma_configured": kuma_configured,
        "task_counts": task_counts,
        "timezone": (app_settings.timezone or "UTC") if app_settings else "UTC",
    })
