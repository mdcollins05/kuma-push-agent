"""Tests for the Recreate helper (app/recreate.py).

verify_then_reset() asks Kuma (Socket.IO `getMonitor`, a real read-only call)
whether the monitor still exists, then either resets the local sync state and
schedules an immediate check (if confirmed missing) or just clears
kuma_missing (if Kuma still has it) or leaves state untouched (if the verify
call itself failed).
"""
from unittest.mock import patch

import pytest

from app import recreate
from app.models import AppSettings, KumaTask, Monitor
from tests.conftest import TestingSessionLocal, testing_session_factory


# ── Helpers ──────────────────────────────────────────────────────────────────

@pytest.fixture(autouse=True)
def _patch_session_local():
    """Route the worker's SessionLocal at the import site to the testing factory
    so DB writes inside verify_then_reset land in the same in-memory DB the
    tests read from."""
    with patch("app.database.SessionLocal", testing_session_factory):
        yield


@pytest.fixture
def configured_settings():
    """Flip AppSettings(1) to configured for the duration of the test."""
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
        cfg = db.get(AppSettings, 1)
        cfg.configured, cfg.kuma_url, cfg.kuma_username, cfg.kuma_password = before
        db.commit()
    finally:
        db.close()


def _make_monitor(**overrides) -> int:
    db = TestingSessionLocal()
    try:
        m = Monitor(
            name=overrides.pop("name", "Recreate Subject"),
            interval=60, enabled=True,
            config={"type": "http", "url": "https://x.test"},
            kuma_monitor_id=overrides.pop("kuma_monitor_id", 42),
            push_token=overrides.pop("push_token", "oldtoken"),
            kuma_synced=overrides.pop("kuma_synced", True),
            kuma_missing=overrides.pop("kuma_missing", True),
            **overrides,
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


def _stub_add_check_job(monkeypatch) -> list:
    calls: list[tuple[int, int]] = []

    def fake(monitor_id, interval, last_check_time=None):
        calls.append((monitor_id, interval))

    monkeypatch.setattr("app.recreate.add_check_job", fake)
    return calls


# ── Probe outcomes ───────────────────────────────────────────────────────────

def test_verify_returns_false_triggers_reset(client, monkeypatch, configured_settings):
    """Kuma confirms the monitor is gone → drop IDs, reset flags, reschedule check."""
    calls = _stub_add_check_job(monkeypatch)
    monkeypatch.setattr("app.kuma.monitor_exists", lambda *a, **kw: False)

    mid = _make_monitor()
    try:
        outcome = recreate.verify_then_reset(mid)
        assert outcome == recreate.OUTCOME_RECREATED
        m = _read(mid)
        assert m.kuma_monitor_id is None
        assert m.push_token is None
        assert m.kuma_synced is False
        assert m.kuma_missing is False
        assert calls == [(mid, 60)]
    finally:
        _cleanup(mid)


def test_verify_returns_true_clears_flag_only(client, monkeypatch, configured_settings):
    """Kuma still has the monitor → clear kuma_missing but keep the IDs so we
    don't end up with a duplicate."""
    calls = _stub_add_check_job(monkeypatch)
    monkeypatch.setattr("app.kuma.monitor_exists", lambda *a, **kw: True)

    mid = _make_monitor()
    try:
        outcome = recreate.verify_then_reset(mid)
        assert outcome == recreate.OUTCOME_VERIFIED
        m = _read(mid)
        assert m.kuma_monitor_id == 42
        assert m.push_token == "oldtoken"
        assert m.kuma_synced is True
        assert m.kuma_missing is False
        assert calls == [], "no immediate check needed — monitor is still alive on Kuma"
    finally:
        _cleanup(mid)


def test_verify_raises_leaves_state_untouched(client, monkeypatch, configured_settings):
    """Kuma unreachable / auth failed → don't change anything so the user can retry."""
    calls = _stub_add_check_job(monkeypatch)

    def raise_(*a, **kw):
        raise ConnectionError("kuma down")

    monkeypatch.setattr("app.kuma.monitor_exists", raise_)

    mid = _make_monitor()
    try:
        outcome = recreate.verify_then_reset(mid)
        assert outcome == recreate.OUTCOME_UNREACHABLE
        m = _read(mid)
        assert m.kuma_monitor_id == 42
        assert m.kuma_missing is True, "flag unchanged when verification fails"
        assert calls == []
    finally:
        _cleanup(mid)


# ── Pre-conditions where there's nothing to verify against ───────────────────

def test_kuma_not_configured_falls_through_to_reset(client, monkeypatch):
    """AppSettings.configured is False in the default conftest — verify_then_reset
    can't probe so it falls back to the legacy 'trust the user' full reset."""
    calls = _stub_add_check_job(monkeypatch)
    mid = _make_monitor()
    try:
        outcome = recreate.verify_then_reset(mid)
        assert outcome == recreate.OUTCOME_RECREATED
        assert calls == [(mid, 60)]
        m = _read(mid)
        assert m.kuma_monitor_id is None
    finally:
        _cleanup(mid)


def test_no_kuma_monitor_id_falls_through_to_reset(client, monkeypatch, configured_settings):
    """If the cached kuma_monitor_id is already gone, there's nothing to look up."""
    calls = _stub_add_check_job(monkeypatch)
    mid = _make_monitor(kuma_monitor_id=None, push_token=None)
    try:
        outcome = recreate.verify_then_reset(mid)
        assert outcome == recreate.OUTCOME_RECREATED
        assert calls == [(mid, 60)]
    finally:
        _cleanup(mid)


def test_unknown_monitor_id_is_unreachable(client, monkeypatch, configured_settings):
    """Defensive: caller already validated, but the worker should handle a
    missing row gracefully without raising."""
    _stub_add_check_job(monkeypatch)
    outcome = recreate.verify_then_reset(99999999)
    assert outcome == recreate.OUTCOME_UNREACHABLE
