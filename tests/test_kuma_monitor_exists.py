"""Tests for app.kuma.monitor_exists — the read-only Kuma getMonitor wrapper.

The function maps Kuma's "does not exist" / "not found" error strings to a
False return so callers don't have to string-match themselves. Anything else
(connection error, auth failure) propagates so the caller can leave state
untouched rather than wrongly resetting.
"""
import pytest

from app import kuma


class _FakeSio:
    """Stand-in for the socketio.Client that kuma_session configures and shuts down."""

    reconnection = True

    def shutdown(self):
        return None


class _FakeKumaApi:
    """Minimal stand-in for uptime_kuma_api.UptimeKumaApi used by monitor_exists."""

    def __init__(self, *, on_get_monitor):
        self._on_get_monitor = on_get_monitor
        self.sio = _FakeSio()

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def login(self, *_args, **_kw):
        return None

    def get_monitor(self, monitor_id):
        return self._on_get_monitor(monitor_id)


def _patch_api(monkeypatch, *, on_get_monitor):
    def _factory(url, **_kw):
        return _FakeKumaApi(on_get_monitor=on_get_monitor)

    monkeypatch.setattr("app.kuma.UptimeKumaApi", _factory)


# ── exists / not-exists mapping ──────────────────────────────────────────────

def test_returns_true_when_kuma_returns_monitor_data():
    """Happy path: Kuma returns a row → True."""

    def on_get(mid):
        assert mid == 42
        return {"id": 42, "name": "thing", "pushToken": "t"}

    def setup(monkeypatch):
        _patch_api(monkeypatch, on_get_monitor=on_get)

    with pytest.MonkeyPatch.context() as mp:
        setup(mp)
        assert kuma.monitor_exists(42, "http://k", "u", "p") is True


def test_returns_false_on_does_not_exist():
    def on_get(_):
        raise RuntimeError("Monitor does not exist.")

    with pytest.MonkeyPatch.context() as mp:
        _patch_api(mp, on_get_monitor=on_get)
        assert kuma.monitor_exists(42, "http://k", "u", "p") is False


def test_returns_false_on_not_found():
    def on_get(_):
        raise RuntimeError("Not found")

    with pytest.MonkeyPatch.context() as mp:
        _patch_api(mp, on_get_monitor=on_get)
        assert kuma.monitor_exists(42, "http://k", "u", "p") is False


def test_returns_false_on_mixed_case_message():
    """Mapping is case-insensitive — Kuma sometimes capitalises."""
    def on_get(_):
        raise RuntimeError("Monitor NOT FOUND in DB")

    with pytest.MonkeyPatch.context() as mp:
        _patch_api(mp, on_get_monitor=on_get)
        assert kuma.monitor_exists(42, "http://k", "u", "p") is False


# ── Unexpected errors must propagate, not return False ───────────────────────

def test_connection_error_propagates():
    def on_get(_):
        raise ConnectionError("kuma unreachable")

    with pytest.MonkeyPatch.context() as mp:
        _patch_api(mp, on_get_monitor=on_get)
        with pytest.raises(ConnectionError):
            kuma.monitor_exists(42, "http://k", "u", "p")


def test_auth_error_propagates():
    def on_get(_):
        raise PermissionError("not authorised")

    with pytest.MonkeyPatch.context() as mp:
        _patch_api(mp, on_get_monitor=on_get)
        with pytest.raises(PermissionError):
            kuma.monitor_exists(42, "http://k", "u", "p")


def test_unrelated_error_propagates():
    """An error message that doesn't match the not-found patterns must surface."""
    def on_get(_):
        raise RuntimeError("Database is locked")

    with pytest.MonkeyPatch.context() as mp:
        _patch_api(mp, on_get_monitor=on_get)
        with pytest.raises(RuntimeError, match="Database is locked"):
            kuma.monitor_exists(42, "http://k", "u", "p")
