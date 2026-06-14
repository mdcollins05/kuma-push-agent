"""Tests for the defensive Recreate flow (app/recreate.py).

When Recreate is clicked, the helper probes Kuma first to decide whether the
monitor really is missing. This prevents the common footgun where a restored
Kuma monitor would otherwise get duplicated by an unconditional fresh create.
"""
import httpx
import pytest

from app import recreate
from app.models import AppSettings, KumaTask, Monitor
from tests.conftest import TestingSessionLocal


# ── Helpers ──────────────────────────────────────────────────────────────────

def _make_monitor(*, push_token: str | None, kuma_missing: bool = True) -> int:
    db = TestingSessionLocal()
    try:
        m = Monitor(
            name=f"Recreate {push_token!r}",
            interval=60, enabled=True,
            config={"type": "http", "url": "https://x.test"},
            kuma_monitor_id=42, push_token=push_token,
            kuma_synced=True, kuma_missing=kuma_missing,
        )
        db.add(m)
        db.commit()
        db.refresh(m)
        return m.id
    finally:
        db.close()


def _cleanup(mid: int):
    db = TestingSessionLocal()
    try:
        db.query(KumaTask).filter(KumaTask.monitor_id == mid).delete()
        m = db.get(Monitor, mid)
        if m:
            db.delete(m)
        db.commit()
    finally:
        db.close()


def _read(mid: int) -> Monitor:
    db = TestingSessionLocal()
    try:
        return db.get(Monitor, mid)
    finally:
        db.close()


def _configured_settings(kuma_url: str = "http://kuma.test") -> AppSettings:
    """Return a fake AppSettings instance (not committed) just for app_cfg arg."""
    return AppSettings(id=1, kuma_url=kuma_url, configured=True)


def _stub_httpx(monkeypatch, response_or_exc):
    transport_or_exc = response_or_exc

    class StubClient:
        def __init__(self, **kw):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def get(self, url):
            if isinstance(transport_or_exc, Exception):
                raise transport_or_exc
            return transport_or_exc

    monkeypatch.setattr("app.recreate.httpx.Client", StubClient)


def _stub_add_check_job(monkeypatch) -> list:
    calls: list[tuple[int, int]] = []

    def fake(monitor_id, interval, last_check_time=None):
        calls.append((monitor_id, interval))

    monkeypatch.setattr("app.recreate.add_check_job", fake)
    return calls


# ── Probe outcomes ───────────────────────────────────────────────────────────

def test_probe_404_triggers_full_reset(client, monkeypatch):
    """Confirmed-missing path: drop IDs, reset flags, reschedule check."""
    mid = _make_monitor(push_token="t")
    try:
        _stub_httpx(monkeypatch, httpx.Response(404))
        calls = _stub_add_check_job(monkeypatch)
        db = TestingSessionLocal()
        try:
            outcome = recreate.recreate_or_verify(db.get(Monitor, mid), _configured_settings(), db)
        finally:
            db.close()
        assert outcome == recreate.OUTCOME_RECREATED
        m = _read(mid)
        assert m.kuma_monitor_id is None
        assert m.push_token is None
        assert m.kuma_synced is False
        assert m.kuma_missing is False
        assert calls == [(mid, 60)]
    finally:
        _cleanup(mid)


def test_probe_200_clears_missing_only(client, monkeypatch):
    """Verified-alive path: clear kuma_missing but keep IDs so we don't duplicate."""
    mid = _make_monitor(push_token="t")
    try:
        _stub_httpx(monkeypatch, httpx.Response(200, text="OK"))
        calls = _stub_add_check_job(monkeypatch)
        db = TestingSessionLocal()
        try:
            outcome = recreate.recreate_or_verify(db.get(Monitor, mid), _configured_settings(), db)
        finally:
            db.close()
        assert outcome == recreate.OUTCOME_VERIFIED
        m = _read(mid)
        assert m.kuma_monitor_id == 42
        assert m.push_token == "t"
        assert m.kuma_synced is True
        assert m.kuma_missing is False
        assert calls == [], "no immediate check needed — monitor is still alive on Kuma"
    finally:
        _cleanup(mid)


def test_probe_5xx_is_unreachable_and_makes_no_change(client, monkeypatch):
    """Inconclusive response: don't touch state so we don't risk creating a duplicate."""
    mid = _make_monitor(push_token="t")
    try:
        _stub_httpx(monkeypatch, httpx.Response(503))
        calls = _stub_add_check_job(monkeypatch)
        db = TestingSessionLocal()
        try:
            outcome = recreate.recreate_or_verify(db.get(Monitor, mid), _configured_settings(), db)
        finally:
            db.close()
        assert outcome == recreate.OUTCOME_UNREACHABLE
        m = _read(mid)
        assert m.kuma_monitor_id == 42
        assert m.kuma_missing is True, "flag unchanged when we can't confirm anything"
        assert calls == []
    finally:
        _cleanup(mid)


def test_probe_network_error_is_unreachable_and_makes_no_change(client, monkeypatch):
    mid = _make_monitor(push_token="t")
    try:
        _stub_httpx(monkeypatch, httpx.ConnectError("kuma down"))
        calls = _stub_add_check_job(monkeypatch)
        db = TestingSessionLocal()
        try:
            outcome = recreate.recreate_or_verify(db.get(Monitor, mid), _configured_settings(), db)
        finally:
            db.close()
        assert outcome == recreate.OUTCOME_UNREACHABLE
        m = _read(mid)
        assert m.kuma_monitor_id == 42
        assert m.kuma_missing is True
        assert calls == []
    finally:
        _cleanup(mid)


# ── Pre-conditions that fall through to the legacy "full reset" path ─────────

def test_no_push_token_falls_through_to_reset(client, monkeypatch):
    """Without a push_token we can't probe — old behaviour: reset."""
    mid = _make_monitor(push_token=None)
    try:
        calls = _stub_add_check_job(monkeypatch)
        db = TestingSessionLocal()
        try:
            outcome = recreate.recreate_or_verify(db.get(Monitor, mid), _configured_settings(), db)
        finally:
            db.close()
        assert outcome == recreate.OUTCOME_RECREATED
        assert calls == [(mid, 60)]
        m = _read(mid)
        assert m.kuma_monitor_id is None
    finally:
        _cleanup(mid)


def test_no_kuma_url_falls_through_to_reset(client, monkeypatch):
    """Kuma not configured: can't probe; do a reset so the local row is clean
    once the user fixes config."""
    mid = _make_monitor(push_token="t")
    try:
        calls = _stub_add_check_job(monkeypatch)
        db = TestingSessionLocal()
        try:
            outcome = recreate.recreate_or_verify(db.get(Monitor, mid), None, db)
        finally:
            db.close()
        assert outcome == recreate.OUTCOME_RECREATED
        assert calls == [(mid, 60)]
        m = _read(mid)
        assert m.kuma_monitor_id is None
    finally:
        _cleanup(mid)
