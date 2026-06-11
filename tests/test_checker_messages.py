"""Tests for checker.run_check error messages — keyword-mismatch vs HTTP status."""
from unittest.mock import patch

import httpx
import pytest

from app import checker
from app.models import Monitor
from tests.conftest import TestingSessionLocal, testing_session_factory


@pytest.fixture(autouse=True)
def patch_session_local():
    with patch("app.database.SessionLocal", testing_session_factory):
        yield


def _make_monitor(**kw) -> int:
    db = TestingSessionLocal()
    try:
        m = Monitor(
            name=kw.pop("name", "T"),
            url=kw.pop("url", "https://x.test/"),
            interval=60,
            enabled=True,
            **kw,
        )
        db.add(m)
        db.commit()
        db.refresh(m)
        return m.id
    finally:
        db.close()


def _read(monitor_id: int) -> Monitor:
    db = TestingSessionLocal()
    try:
        return db.get(Monitor, monitor_id)
    finally:
        db.close()


def _stub_client(monkeypatch, response: httpx.Response):
    transport = httpx.MockTransport(lambda req: response)

    class StubClient(httpx.Client):
        def __init__(self, **kw):
            kw["transport"] = transport
            super().__init__(**kw)

    monkeypatch.setattr("app.checker.httpx.Client", StubClient)


def test_keyword_missing_reports_keyword_message(client, monkeypatch):
    """When the status code is fine but the keyword is missing, the error
    should call that out instead of reporting 'HTTP 200'."""
    mid = _make_monitor(name="KW Missing", keyword="healthy")
    _stub_client(monkeypatch, httpx.Response(200, text="other body"))

    checker.run_check(mid)

    m = _read(mid)
    try:
        assert m.last_status == "down"
        assert "Keyword" in m.last_error
        assert "healthy" in m.last_error
        assert "HTTP 200" not in m.last_error
    finally:
        _delete(mid)


def test_keyword_present_reports_ok(client, monkeypatch):
    mid = _make_monitor(name="KW Present", keyword="healthy")
    _stub_client(monkeypatch, httpx.Response(200, text="all healthy here"))

    checker.run_check(mid)

    m = _read(mid)
    try:
        assert m.last_status == "up"
        assert m.last_error is None
    finally:
        _delete(mid)


def test_unexpected_status_reports_http_code(client, monkeypatch):
    mid = _make_monitor(name="HTTP 500")
    _stub_client(monkeypatch, httpx.Response(500))

    checker.run_check(mid)

    m = _read(mid)
    try:
        assert m.last_status == "down"
        assert m.last_error == "HTTP 500"
    finally:
        _delete(mid)


def _delete(monitor_id: int):
    db = TestingSessionLocal()
    try:
        m = db.get(Monitor, monitor_id)
        if m:
            db.delete(m)
            db.commit()
    finally:
        db.close()
