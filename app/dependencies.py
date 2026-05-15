from fastapi import Depends, HTTPException, Request
from sqlalchemy.orm import Session

from .database import SessionLocal
from .models import AppSettings


def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


class SetupRequired(Exception):
    pass


class LoginRequired(Exception):
    def __init__(self, next_url: str = "/"):
        self.next_url = next_url


def require_auth(request: Request, db: Session = Depends(get_db)) -> str:
    app_settings = db.get(AppSettings, 1)
    if not app_settings or not app_settings.ui_username:
        raise SetupRequired()
    if not request.session.get("user"):
        raise LoginRequired(next_url=str(request.url.path))
    return request.session["user"]


def require_api_key(request: Request, db: Session = Depends(get_db)) -> None:
    app_settings = db.get(AppSettings, 1)
    provided = request.headers.get("X-API-Key", "")
    if not app_settings or not app_settings.api_key or provided != app_settings.api_key:
        raise HTTPException(status_code=401, detail="Invalid or missing API key")
