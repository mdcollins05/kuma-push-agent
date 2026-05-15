import uuid
from typing import Optional

import bcrypt
from fastapi import APIRouter, Depends, Form, Request
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import RedirectResponse
from sqlalchemy.orm import Session

from ..dependencies import get_db, require_auth
from ..models import AppSettings
from ..templates import templates

router = APIRouter()


def _hash(password: str) -> str:
    return bcrypt.hashpw(password.encode(), bcrypt.gensalt()).decode()


@router.get("/settings")
async def settings_get(
    request: Request,
    db: Session = Depends(get_db),
    user: str = Depends(require_auth),
):
    from ..timezones import TIMEZONES
    app_settings = db.get(AppSettings, 1)
    saved = request.query_params.get("saved")
    return templates.TemplateResponse(request, "settings.html", {
        "s": app_settings, "user": user, "saved": saved, "error": None,
        "timezones": TIMEZONES,
    })


@router.post("/settings/kuma")
async def settings_kuma_post(
    request: Request,
    kuma_url: str = Form(...),
    kuma_username: str = Form(...),
    kuma_password: Optional[str] = Form(None),
    db: Session = Depends(get_db),
    user: str = Depends(require_auth),
):
    app_settings = db.get(AppSettings, 1)
    app_settings.kuma_url = kuma_url.rstrip("/")
    app_settings.kuma_username = kuma_username
    if kuma_password:
        app_settings.kuma_password = kuma_password
    app_settings.configured = True
    db.commit()
    return RedirectResponse("/settings?saved=1", status_code=302)


@router.post("/settings/kuma/disconnect")
async def settings_kuma_disconnect(
    db: Session = Depends(get_db),
    user: str = Depends(require_auth),
):
    app_settings = db.get(AppSettings, 1)
    app_settings.kuma_url = None
    app_settings.kuma_username = None
    app_settings.kuma_password = None
    app_settings.configured = False
    db.commit()
    return RedirectResponse("/settings?saved=1", status_code=302)


@router.post("/settings/password")
async def settings_password_post(
    request: Request,
    new_password: str = Form(...),
    confirm_password: str = Form(...),
    db: Session = Depends(get_db),
    user: str = Depends(require_auth),
):
    app_settings = db.get(AppSettings, 1)
    if new_password != confirm_password:
        return templates.TemplateResponse(
            request, "settings.html",
            {"s": app_settings, "user": user, "saved": None, "error": "Passwords do not match."},
            status_code=400,
        )
    app_settings.ui_password_hash = _hash(new_password)
    db.commit()
    return RedirectResponse("/settings?saved=1", status_code=302)


@router.post("/settings/test")
async def settings_test(
    kuma_url: str = Form(...),
    kuma_username: str = Form(...),
    kuma_password: str = Form(...),
):
    from ..kuma import test_connection
    try:
        await run_in_threadpool(test_connection, kuma_url.rstrip("/"), kuma_username, kuma_password)
        return {"ok": True}
    except Exception as exc:
        return {"ok": False, "error": str(exc)}


@router.post("/settings/timezone")
async def settings_timezone_post(
    timezone: str = Form("UTC"),
    db: Session = Depends(get_db),
    user: str = Depends(require_auth),
):
    from ..timezones import TIMEZONE_NAMES
    if timezone not in TIMEZONE_NAMES:
        timezone = "UTC"
    app_settings = db.get(AppSettings, 1)
    app_settings.timezone = timezone
    db.commit()
    return RedirectResponse("/settings?saved=1", status_code=302)


@router.post("/settings/regenerate-key")
async def regenerate_api_key(
    db: Session = Depends(get_db),
    user: str = Depends(require_auth),
):
    app_settings = db.get(AppSettings, 1)
    app_settings.api_key = str(uuid.uuid4())
    db.commit()
    return RedirectResponse("/settings?saved=1", status_code=302)
