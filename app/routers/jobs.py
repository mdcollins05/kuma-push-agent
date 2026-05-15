from fastapi import APIRouter, Depends
from fastapi.responses import JSONResponse
from sqlalchemy.orm import Session

from ..dependencies import get_db, require_auth
from ..models import KumaJob

router = APIRouter(prefix="/jobs")


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
