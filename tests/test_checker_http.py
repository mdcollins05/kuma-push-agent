"""Tests for the _check_http path in checker — method, headers, body, keyword, response time."""
import httpx
import pytest

from app.checker import _check_http


def _client(handler):
    return httpx.MockTransport(handler)


def test_get_default_method(monkeypatch):
    seen = {}

    def handler(req: httpx.Request) -> httpx.Response:
        seen["method"] = req.method
        return httpx.Response(200, text="OK")

    transport = _client(handler)

    class StubClient(httpx.Client):
        def __init__(self, **kw):
            kw["transport"] = transport
            super().__init__(**kw)

    monkeypatch.setattr("app.checker.httpx.Client", StubClient)
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

    transport = _client(handler)

    class StubClient(httpx.Client):
        def __init__(self, **kw):
            kw["transport"] = transport
            super().__init__(**kw)

    monkeypatch.setattr("app.checker.httpx.Client", StubClient)
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
    def handler(req: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text="not the right body")

    transport = _client(handler)

    class StubClient(httpx.Client):
        def __init__(self, **kw):
            kw["transport"] = transport
            super().__init__(**kw)

    monkeypatch.setattr("app.checker.httpx.Client", StubClient)
    status, msg, _ = _check_http({
        "type": "http", "url": "https://x.test/",
        "expected_codes": [200],
        "keyword": "healthy",
    })
    assert status == "down"
    assert "Keyword" in msg
    assert "healthy" in msg


def test_unexpected_status_code(monkeypatch):
    def handler(req: httpx.Request) -> httpx.Response:
        return httpx.Response(500)

    transport = _client(handler)

    class StubClient(httpx.Client):
        def __init__(self, **kw):
            kw["transport"] = transport
            super().__init__(**kw)

    monkeypatch.setattr("app.checker.httpx.Client", StubClient)
    status, msg, _ = _check_http({"type": "http", "url": "https://x.test/", "expected_codes": [200]})
    assert status == "down"
    assert msg == "HTTP 500"


def test_method_is_uppercased(monkeypatch):
    seen = {}

    def handler(req: httpx.Request) -> httpx.Response:
        seen["method"] = req.method
        return httpx.Response(200)

    transport = _client(handler)

    class StubClient(httpx.Client):
        def __init__(self, **kw):
            kw["transport"] = transport
            super().__init__(**kw)

    monkeypatch.setattr("app.checker.httpx.Client", StubClient)
    _check_http({"type": "http", "url": "https://x.test/", "method": "put", "expected_codes": [200]})
    assert seen["method"] == "PUT"
