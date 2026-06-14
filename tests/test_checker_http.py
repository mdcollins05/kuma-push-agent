"""Tests for the HTTP check path in checker — unit-level _check_http plus
the run_check integration that persists last_status / last_error on Monitor."""
from unittest.mock import patch

import httpx
import pytest

from app import checker
from app.checker import _check_http
from app.models import Monitor
from tests.conftest import TestingSessionLocal, testing_session_factory


# ── Helpers ──────────────────────────────────────────────────────────────────

def _stub_transport(handler):
    return httpx.MockTransport(handler)


def _stub_checker_client(monkeypatch, transport):
    class StubClient(httpx.Client):
        def __init__(self, **kw):
            kw["transport"] = transport
            super().__init__(**kw)

    monkeypatch.setattr("app.checker.httpx.Client", StubClient)


def _stub_response(monkeypatch, response: httpx.Response):
    _stub_checker_client(monkeypatch, _stub_transport(lambda req: response))


# ── _check_http() unit — return tuple behaviour ──────────────────────────────

def test_get_default_method(monkeypatch):
    seen = {}

    def handler(req: httpx.Request) -> httpx.Response:
        seen["method"] = req.method
        return httpx.Response(200, text="OK")

    _stub_checker_client(monkeypatch, _stub_transport(handler))
    status, msg, _ = _check_http({"type": "http", "url": "https://x.test/", "expected_codes": [200]})
    assert status == "up"
    assert msg == "OK"
    assert seen["method"] == "GET"


def test_post_with_body_and_headers(monkeypatch):
    seen = {}

    def handler(req: httpx.Request) -> httpx.Response:
        seen["method"] = req.method
        seen["headers"] = dict(req.headers)
        seen["body"] = req.content.decode("utf-8")
        return httpx.Response(204)

    _stub_checker_client(monkeypatch, _stub_transport(handler))
    cfg = {
        "type": "http", "url": "https://api.test/probe",
        "method": "POST",
        "headers": {"X-Test": "yes", "Authorization": "Bearer t"},
        "body": '{"probe": 1}',
        "expected_codes": [204],
    }
    status, msg, _ = _check_http(cfg)
    assert status == "up"
    assert msg == "OK"
    assert seen["method"] == "POST"
    assert seen["headers"]["x-test"] == "yes"
    assert seen["headers"]["authorization"] == "Bearer t"
    assert seen["body"] == '{"probe": 1}'


def test_keyword_required(monkeypatch):
    _stub_response(monkeypatch, httpx.Response(200, text="not the right body"))
    status, msg, _ = _check_http({
        "type": "http", "url": "https://x.test/",
        "expected_codes": [200],
        "keyword": "healthy",
    })
    assert status == "down"
    assert "Keyword" in msg
    assert "healthy" in msg


def test_unexpected_status_code(monkeypatch):
    _stub_response(monkeypatch, httpx.Response(500))
    status, msg, _ = _check_http({"type": "http", "url": "https://x.test/", "expected_codes": [200]})
    assert status == "down"
    assert msg == "HTTP 500"


def test_method_is_uppercased(monkeypatch):
    seen = {}

    def handler(req: httpx.Request) -> httpx.Response:
        seen["method"] = req.method
        return httpx.Response(200)

    _stub_checker_client(monkeypatch, _stub_transport(handler))
    _check_http({"type": "http", "url": "https://x.test/", "method": "put", "expected_codes": [200]})
    assert seen["method"] == "PUT"


# ── run_check() integration — last_status / last_error persisted on Monitor ──

@pytest.fixture(autouse=False)
def patch_session_local():
    """Route the checker's SessionLocal at the import site to the test DB so
    run_check() persists into the same in-memory database the assertions read."""
    with patch("app.database.SessionLocal", testing_session_factory):
        yield


def _make_monitor(**kw) -> int:
    name = kw.pop("name", "T")
    config = {
        "type": "http",
        "url": kw.pop("url", "https://x.test/"),
        "method": "GET",
        "headers": {},
        "body": None,
        "expected_codes": kw.pop("expected_codes", [200]),
        "keyword": kw.pop("keyword", None),
        "max_response_ms": kw.pop("max_response_ms", None),
        "verify_ssl": kw.pop("verify_ssl", True),
    }
    db = TestingSessionLocal()
    try:
        m = Monitor(name=name, interval=60, enabled=True, config=config, **kw)
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


def _delete(monitor_id: int):
    db = TestingSessionLocal()
    try:
        m = db.get(Monitor, monitor_id)
        if m:
            db.delete(m)
            db.commit()
    finally:
        db.close()


def test_keyword_missing_reports_keyword_message(client, monkeypatch, patch_session_local):
    """When the status code is fine but the keyword is missing, the error
    should call that out instead of reporting 'HTTP 200'."""
    mid = _make_monitor(name="KW Missing", keyword="healthy")
    _stub_response(monkeypatch, httpx.Response(200, text="other body"))

    checker.run_check(mid)

    m = _read(mid)
    try:
        assert m.last_status == "down"
        assert "Keyword" in m.last_error
        assert "healthy" in m.last_error
        assert "HTTP 200" not in m.last_error
    finally:
        _delete(mid)


def test_keyword_present_reports_ok(client, monkeypatch, patch_session_local):
    mid = _make_monitor(name="KW Present", keyword="healthy")
    _stub_response(monkeypatch, httpx.Response(200, text="all healthy here"))

    checker.run_check(mid)

    m = _read(mid)
    try:
        assert m.last_status == "up"
        assert m.last_error is None
    finally:
        _delete(mid)


def test_unexpected_status_reports_http_code(client, monkeypatch, patch_session_local):
    mid = _make_monitor(name="HTTP 500")
    _stub_response(monkeypatch, httpx.Response(500))

    checker.run_check(mid)

    m = _read(mid)
    try:
        assert m.last_status == "down"
        assert m.last_error == "HTTP 500"
    finally:
        _delete(mid)
