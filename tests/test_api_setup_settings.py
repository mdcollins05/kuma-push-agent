from app.models import AppSettings
from tests.conftest import HEADERS, TestingSessionLocal


# ── POST /api/v1/setup ────────────────────────────────────────────────────────

def test_setup_already_configured_returns_409(client):
    resp = client.post("/api/v1/setup", json={
        "username": "admin2", "password": "secret", "timezone": "UTC",
    })
    assert resp.status_code == 409


def test_setup_creates_user_on_fresh_db(client):
    db = TestingSessionLocal()
    s = db.get(AppSettings, 1)
    saved = (s.ui_username, s.ui_password_hash, s.api_key)
    s.ui_username = None
    s.ui_password_hash = None
    s.api_key = None
    db.commit()
    try:
        resp = client.post("/api/v1/setup", json={
            "username": "admin", "password": "secret", "timezone": "America/New_York",
        })
        assert resp.status_code == 200
        body = resp.json()
        assert "api_key" in body
        assert len(body["api_key"]) > 0
    finally:
        s = db.get(AppSettings, 1)
        s.ui_username, s.ui_password_hash, s.api_key = saved
        db.commit()
        db.close()


# ── PUT /api/v1/settings/kuma ─────────────────────────────────────────────────

def _reset_kuma_settings():
    db = TestingSessionLocal()
    try:
        s = db.get(AppSettings, 1)
        s.configured = False
        s.kuma_url = None
        s.kuma_username = None
        s.kuma_password = None
        db.commit()
    finally:
        db.close()


def test_configure_kuma_saves_settings(client):
    resp = client.put("/api/v1/settings/kuma", json={
        "kuma_url": "http://kuma:3001",
        "kuma_username": "admin",
        "kuma_password": "secret",
    }, headers=HEADERS)
    assert resp.status_code == 200
    body = resp.json()
    assert body["kuma_url"] == "http://kuma:3001"
    assert body["kuma_username"] == "admin"
    assert body["configured"] is True
    _reset_kuma_settings()


def test_configure_kuma_strips_trailing_slash(client):
    resp = client.put("/api/v1/settings/kuma", json={
        "kuma_url": "http://kuma:3001/",
        "kuma_username": "admin",
        "kuma_password": "secret",
    }, headers=HEADERS)
    assert resp.status_code == 200
    assert resp.json()["kuma_url"] == "http://kuma:3001"
    _reset_kuma_settings()


def test_configure_kuma_blank_url_returns_422(client):
    resp = client.put("/api/v1/settings/kuma", json={
        "kuma_url": "   ",
        "kuma_username": "admin",
    }, headers=HEADERS)
    assert resp.status_code == 422


def test_configure_kuma_blank_username_returns_422(client):
    resp = client.put("/api/v1/settings/kuma", json={
        "kuma_url": "http://kuma:3001",
        "kuma_username": "   ",
    }, headers=HEADERS)
    assert resp.status_code == 422
