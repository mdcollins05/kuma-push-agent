"""Tests for the Recreate helper (app/recreate.py).

reset_and_reschedule() drops the cached Kuma sync state, cancels queued tasks
targeting the dead kuma_monitor_id, and fires the check job immediately so the
sync banner shows up right away.
"""
import pytest

from app import recreate
from app.models import KumaTask, Monitor
from tests.conftest import TestingSessionLocal


# ── Helpers ──────────────────────────────────────────────────────────────────

def _make_monitor() -> int:
    db = TestingSessionLocal()
    try:
        m = Monitor(
            name="Recreate Subject", interval=60, enabled=True,
            config={"type": "http", "url": "https://x.test"},
            kuma_monitor_id=42, push_token="oldtoken",
            kuma_synced=True, kuma_missing=True,
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


def _stub_add_check_job(monkeypatch) -> list:
    calls: list[tuple[int, int]] = []

    def fake(monitor_id, interval, last_check_time=None):
        calls.append((monitor_id, interval))

    monkeypatch.setattr("app.recreate.add_check_job", fake)
    return calls


# ── Behaviour ────────────────────────────────────────────────────────────────

def test_resets_sync_state(client, monkeypatch):
    _stub_add_check_job(monkeypatch)
    mid = _make_monitor()
    try:
        db = TestingSessionLocal()
        try:
            recreate.reset_and_reschedule(db.get(Monitor, mid), db)
        finally:
            db.close()

        db = TestingSessionLocal()
        try:
            m = db.get(Monitor, mid)
            assert m.kuma_monitor_id is None
            assert m.push_token is None
            assert m.kuma_synced is False
            assert m.kuma_missing is False
        finally:
            db.close()
    finally:
        _cleanup(mid)


def test_cancels_pending_tasks(client, monkeypatch):
    """Queued tasks targeting the dead kuma_monitor_id are cancelled so they
    don't retry against the stale ID after the fresh provision lands."""
    _stub_add_check_job(monkeypatch)
    mid = _make_monitor()
    try:
        db = TestingSessionLocal()
        try:
            for status in ("pending", "failed"):
                db.add(KumaTask(
                    task_type="update_monitor", monitor_id=mid, monitor_name="x",
                    payload={"kuma_monitor_id": 42, "fields": {}}, status=status,
                ))
            db.commit()
            recreate.reset_and_reschedule(db.get(Monitor, mid), db)

            statuses = sorted(s for (s,) in db.query(KumaTask.status).filter(KumaTask.monitor_id == mid).all())
            assert statuses == ["cancelled", "cancelled"]
        finally:
            db.close()
    finally:
        _cleanup(mid)


def test_fires_immediate_check(client, monkeypatch):
    """add_check_job is called with the monitor's interval so the sync_monitor
    task appears right away."""
    calls = _stub_add_check_job(monkeypatch)
    mid = _make_monitor()
    try:
        db = TestingSessionLocal()
        try:
            recreate.reset_and_reschedule(db.get(Monitor, mid), db)
        finally:
            db.close()
        assert calls == [(mid, 60)]
    finally:
        _cleanup(mid)
