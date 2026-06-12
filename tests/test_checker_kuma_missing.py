"""Tests for the kuma_missing flag flow in run_check — push 404 sets it, push 200 clears it."""
import httpx
import pytest

from app import checker
from app.models import AppSettings, Monitor
from tests.conftest import TestingSessionLocal


@pytest.fixture
def configured_settings():
    """Flip AppSettings(1) to configured with a kuma_url for the duration of the test."""
    db = TestingSessionLocal()
    try:
        cfg = db.get(AppSettings, 1)
        before = (cfg.configured, cfg.kuma_url, cfg.kuma_username, cfg.kuma_password)
        cfg.configured = True
        cfg.kuma_url = "http://kuma.test"
        cfg.kuma_username = "u"
        cfg.kuma_password = "p"
        db.commit()
        yield
        # Restore
        cfg = db.get(AppSettings, 1)
        cfg.configured, cfg.kuma_url, cfg.kuma_username, cfg.kuma_password = before
        db.commit()
    finally:
        db.close()


def _make_monitor(*, kuma_missing: bool, url: str) -> int:
    db = TestingSessionLocal()
    try:
        m = Monitor(
            name=f"KumaMissing-{url}",
            interval=60,
            config={"type": "http", "url": url},
            enabled=True,
            kuma_monitor_id=99,
            push_token="testtoken",
            kuma_synced=True,
            kuma_missing=kuma_missing,
        )
        db.add(m)
        db.commit()
        db.refresh(m)
        return m.id
    finally:
        db.close()


def _delete_monitor(mid: int):
    db = TestingSessionLocal()
    try:
        m = db.get(Monitor, mid)
        if m:
            db.delete(m)
            db.commit()
    finally:
        db.close()


def _patch_session_local(monkeypatch):
    """Route checker's SessionLocal at the import site to the testing session factory."""
    import app.database as database_mod
    monkeypatch.setattr(database_mod, "SessionLocal", TestingSessionLocal)


def _stub_httpx(monkeypatch, handler):
    transport = httpx.MockTransport(handler)

    class StubClient(httpx.Client):
        def __init__(self, **kw):
            kw["transport"] = transport
            super().__init__(**kw)

    monkeypatch.setattr("app.checker.httpx.Client", StubClient)


def test_push_404_sets_kuma_missing(monkeypatch, client, configured_settings):
    _patch_session_local(monkeypatch)
    mid = _make_monitor(kuma_missing=False, url="https://target-404.test/")

    def handler(req: httpx.Request) -> httpx.Response:
        if "/api/push/" in str(req.url):
            return httpx.Response(404, text="not found")
        return httpx.Response(200, text="OK")

    _stub_httpx(monkeypatch, handler)

    try:
        checker.run_check(mid)
        db = TestingSessionLocal()
        try:
            row = db.get(Monitor, mid)
            assert row.kuma_missing is True
        finally:
            db.close()
    finally:
        _delete_monitor(mid)


def test_push_200_clears_kuma_missing(monkeypatch, client, configured_settings):
    _patch_session_local(monkeypatch)
    mid = _make_monitor(kuma_missing=True, url="https://target-ok.test/")

    def handler(req: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text="OK")

    _stub_httpx(monkeypatch, handler)

    try:
        checker.run_check(mid)
        db = TestingSessionLocal()
        try:
            row = db.get(Monitor, mid)
            assert row.kuma_missing is False
        finally:
            db.close()
    finally:
        _delete_monitor(mid)


def test_push_404_no_op_when_already_missing(monkeypatch, client, configured_settings):
    """Already-missing monitors don't churn the DB on each 404 — flag stays True without re-commit storm."""
    _patch_session_local(monkeypatch)
    mid = _make_monitor(kuma_missing=True, url="https://target-still-404.test/")

    def handler(req: httpx.Request) -> httpx.Response:
        if "/api/push/" in str(req.url):
            return httpx.Response(404)
        return httpx.Response(200, text="OK")

    _stub_httpx(monkeypatch, handler)

    try:
        checker.run_check(mid)
        db = TestingSessionLocal()
        try:
            row = db.get(Monitor, mid)
            assert row.kuma_missing is True
        finally:
            db.close()
    finally:
        _delete_monitor(mid)
