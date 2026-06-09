from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse
from sqlalchemy.orm import Session

from ..dependencies import get_db, require_auth
from ..models import AppSettings, KumaTask
from ..templates import templates
from .. import tag_cache, notification_cache, kuma_queue, update_cache
from ..scheduler import scheduler

router = APIRouter(prefix="/tasks")


@router.get("")
async def tasks_page(
    request: Request,
    db: Session = Depends(get_db),
    user: str = Depends(require_auth),
):
    cfg = db.get(AppSettings, 1)
    tz = (cfg.timezone or "UTC") if cfg else "UTC"
    tasks = (
        db.query(KumaTask)
        .order_by(KumaTask.created_at.desc())
        .limit(200)
        .all()
    )
    return templates.TemplateResponse(request, "tasks.html", {
        "user": user,
        "tasks": tasks,
        "tz": tz,
    })


@router.get("/data")
async def tasks_data(
    db: Session = Depends(get_db),
    user: str = Depends(require_auth),
):
    tasks = (
        db.query(KumaTask)
        .order_by(KumaTask.created_at.desc())
        .limit(200)
        .all()
    )
    return JSONResponse({
        "tasks": [
            {
                "id": t.id,
                "task_type": t.task_type,
                "monitor_id": t.monitor_id,
                "monitor_name": t.monitor_name,
                "status": t.status,
                "error": t.error,
                "retry_count": t.retry_count or 0,
                "created_at": t.created_at.isoformat() if t.created_at else None,
            }
            for t in tasks
        ],
    })


@router.get("/system-status")
async def system_status(user: str = Depends(require_auth)):
    def next_run(task_id: str):
        task = scheduler.get_job(task_id)
        if task and task.next_run_time:
            return task.next_run_time.isoformat()
        return None

    return JSONResponse({
        "tasks": [
            {
                "id": "kuma_task_processor",
                "label": "Kuma task processor",
                "interval": "every 10s",
                **kuma_queue.processor_status(),
                "next_run": next_run("kuma_task_processor"),
            },
            {
                "id": "tag_cache_refresher",
                "label": "Tag cache refresh",
                "interval": "every 5m",
                **tag_cache.status(),
                "next_run": next_run("tag_cache_refresher"),
            },
            {
                "id": "notification_cache_refresher",
                "label": "Notification cache refresh",
                "interval": "every 5m",
                **notification_cache.status(),
                "next_run": next_run("notification_cache_refresher"),
            },
            {
                "id": "update_checker",
                "label": "Update checker",
                "interval": "every 6h",
                **update_cache.status(),
                "next_run": next_run("update_checker"),
            },
        ]
    })


@router.get("/status")
async def tasks_status(
    db: Session = Depends(get_db),
    user: str = Depends(require_auth),
):
    tasks = (
        db.query(KumaTask)
        .order_by(KumaTask.created_at.desc())
        .limit(20)
        .all()
    )
    return JSONResponse({
        "pending": sum(1 for t in tasks if t.status == "pending"),
        "failed": sum(1 for t in tasks if t.status == "failed"),
        "tasks": [
            {
                "id": t.id,
                "type": t.task_type,
                "monitor_id": t.monitor_id,
                "monitor_name": t.monitor_name,
                "status": t.status,
                "error": t.error,
                "created_at": t.created_at.strftime("%H:%M:%S") if t.created_at else None,
            }
            for t in tasks
        ],
    })
