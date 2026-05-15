import uuid

from fastapi import APIRouter, Depends, Form, Request
import bcrypt
from fastapi.responses import RedirectResponse
from sqlalchemy.orm import Session

from ..dependencies import get_db
from ..models import AppSettings
from ..templates import templates

router = APIRouter()


def _hash(password: str) -> str:
    return bcrypt.hashpw(password.encode(), bcrypt.gensalt()).decode()


def _verify(password: str, hashed: str) -> bool:
    return bcrypt.checkpw(password.encode(), hashed.encode())


@router.get("/setup")
async def setup_get(request: Request, db: Session = Depends(get_db)):
    from ..timezones import TIMEZONES
    app_settings = db.get(AppSettings, 1)
    if app_settings and app_settings.ui_username:
        return RedirectResponse("/", status_code=302)
    return templates.TemplateResponse(request, "setup.html", {"error": None, "timezones": TIMEZONES})


@router.post("/setup")
async def setup_post(
    request: Request,
    ui_username: str = Form(...),
    ui_password: str = Form(...),
    timezone: str = Form("UTC"),
    db: Session = Depends(get_db),
):
    if not ui_username or not ui_password:
        from ..timezones import TIMEZONES
        return templates.TemplateResponse(
            request, "setup.html", {"error": "All fields are required.", "timezones": TIMEZONES}, status_code=400
        )

    from ..timezones import TIMEZONE_NAMES
    if timezone not in TIMEZONE_NAMES:
        timezone = "UTC"

    app_settings = db.get(AppSettings, 1)
    if not app_settings:
        app_settings = AppSettings(id=1)
        db.add(app_settings)

    app_settings.ui_username = ui_username
    app_settings.ui_password_hash = _hash(ui_password)
    app_settings.api_key = str(uuid.uuid4())
    app_settings.timezone = timezone
    db.commit()

    return RedirectResponse("/login", status_code=302)


@router.get("/login")
async def login_get(request: Request, db: Session = Depends(get_db)):
    app_settings = db.get(AppSettings, 1)
    if not app_settings or not app_settings.ui_username:
        return RedirectResponse("/setup", status_code=302)
    if request.session.get("user"):
        return RedirectResponse("/", status_code=302)
    next_url = request.query_params.get("next", "/")
    return templates.TemplateResponse(request, "login.html", {"next": next_url, "error": None})


@router.post("/login")
async def login_post(
    request: Request,
    username: str = Form(...),
    password: str = Form(...),
    next: str = Form("/"),
    db: Session = Depends(get_db),
):
    app_settings = db.get(AppSettings, 1)
    valid = (
        app_settings
        and app_settings.ui_username
        and username == app_settings.ui_username
        and _verify(password, app_settings.ui_password_hash)
    )
    if not valid:
        return templates.TemplateResponse(
            request, "login.html", {"next": next, "error": "Invalid username or password."}, status_code=401
        )
    request.session["user"] = username
    return RedirectResponse(next if next.startswith("/") else "/", status_code=302)


@router.post("/logout")
async def logout(request: Request):
    request.session.clear()
    return RedirectResponse("/login", status_code=302)
