"""Tests for the "mutate while Kuma is offline" path.

Previously, every enqueue call site was guarded by `if kuma_url:` so mutations
made while Kuma was unconfigured silently vanished. After the fix, all
mutations queue regardless and the processor's "Kuma not configured → skip,
retry later" loop holds them until reachable.
"""
import pytest

from app.models import KumaTask, Monitor
from tests.conftest import HEADERS, TestingSessionLocal


# ── Helpers ──────────────────────────────────────────────────────────────────

def _make_synced_monitor(name: str) -> int:
    """A monitor that the lazy sync would consider 'already in Kuma' so route
    handlers reach the enqueue branch instead of bailing on kuma_synced=False."""
    db = TestingSessionLocal()
    try:
        m = Monitor(
            name=name, interval=60, enabled=True,
            config={"type": "http", "url": "https://x.test"},
            kuma_monitor_id=100, push_token="t",
            kuma_synced=True,
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


def _pending_for(mid: int) -> list[KumaTask]:
    db = TestingSessionLocal()
    try:
        return db.query(KumaTask).filter(
            KumaTask.monitor_id == mid, KumaTask.status == "pending"
        ).order_by(KumaTask.id).all()
    finally:
        db.close()


# ── Route-driven queueing while Kuma not configured ──────────────────────────

def test_api_update_queues_while_kuma_offline(client):
    """conftest seeds AppSettings.configured=False; PUT /monitors/{id} must still queue."""
    mid = _make_synced_monitor("Offline Update")
    try:
        payload = {
            "name": "Offline Update Renamed",
            "interval": 60,
            "config": {"type": "http", "url": "https://x.test", "expected_codes": [200]},
            "tag_ids": [],
            "notification_ids": [],
            "kuma_group_id": None,
        }
        resp = client.put(f"/api/v1/monitors/{mid}", json=payload, headers=HEADERS)
        assert resp.status_code == 200, resp.text

        pending = _pending_for(mid)
        assert any(t.task_type == "update_monitor" for t in pending), (
            "update_monitor should be queued even when Kuma is not configured"
        )
    finally:
        _cleanup(mid)


def test_api_delete_queues_while_kuma_offline(client):
    """DELETE /monitors/{id} queues delete_monitor for the orphan Kuma row."""
    mid = _make_synced_monitor("Offline Delete")
    try:
        resp = client.delete(f"/api/v1/monitors/{mid}", headers=HEADERS)
        assert resp.status_code == 204

        # The monitor row is gone but its delete_monitor task remains.
        db = TestingSessionLocal()
        try:
            pending = db.query(KumaTask).filter(
                KumaTask.monitor_id == mid,
                KumaTask.task_type == "delete_monitor",
                KumaTask.status == "pending",
            ).all()
            assert len(pending) == 1
        finally:
            db.close()
    finally:
        _cleanup(mid)


# ── Repeated offline mutations coalesce ──────────────────────────────────────

def test_repeated_offline_updates_coalesce_to_latest(client):
    """Three rename PUTs while Kuma is offline → only the last one is pending."""
    mid = _make_synced_monitor("Offline Repeat")
    try:
        for name in ("v1", "v2", "v3"):
            payload = {
                "name": name,
                "interval": 60,
                "config": {"type": "http", "url": "https://x.test", "expected_codes": [200]},
                "tag_ids": [], "notification_ids": [], "kuma_group_id": None,
            }
            resp = client.put(f"/api/v1/monitors/{mid}", json=payload, headers=HEADERS)
            assert resp.status_code == 200, resp.text

        pending = [t for t in _pending_for(mid) if t.task_type == "update_monitor"]
        assert len(pending) == 1
        assert pending[0].payload["fields"].get("name") == "v3"
    finally:
        _cleanup(mid)
