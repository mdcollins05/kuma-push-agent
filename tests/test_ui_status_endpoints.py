from datetime import datetime

import pytest

from app.models import KumaJob, Monitor
from tests.conftest import HEADERS, TestingSessionLocal


# ── Helpers ───────────────────────────────────────────────────────────────────

def _create_monitor(name="Status Monitor", url="https://status.example.com") -> int:
    db = TestingSessionLocal()
    try:
        m = Monitor(name=name, url=url, interval=60, enabled=True)
        db.add(m)
        db.commit()
        db.refresh(m)
        return m.id
    finally:
        db.close()


def _delete_monitor(monitor_id: int):
    db = TestingSessionLocal()
    try:
        m = db.get(Monitor, monitor_id)
        if m:
            db.delete(m)
            db.commit()
    finally:
        db.close()


# ── /monitors/statuses ────────────────────────────────────────────────────────

def test_monitor_statuses_returns_list(client):
    resp = client.get("/monitors/statuses", headers=HEADERS)
    assert resp.status_code == 200
    assert isinstance(resp.json(), list)


def test_monitor_statuses_contains_expected_fields(client):
    mid = _create_monitor("Statuses Field Test")
    try:
        resp = client.get("/monitors/statuses", headers=HEADERS)
        assert resp.status_code == 200
        monitor = next((m for m in resp.json() if m["id"] == mid), None)
        assert monitor is not None
        for field in ("id", "last_status", "last_check_time", "last_response_ms", "kuma_synced", "pending_jobs", "failed_jobs"):
            assert field in monitor, f"missing field: {field}"
    finally:
        _delete_monitor(mid)


# ── /monitors/{id}/status ─────────────────────────────────────────────────────

def test_monitor_status_returns_correct_id(client):
    mid = _create_monitor("Single Status Test")
    try:
        resp = client.get(f"/monitors/{mid}/status", headers=HEADERS)
        assert resp.status_code == 200
        assert resp.json()["id"] == mid
    finally:
        _delete_monitor(mid)


def test_monitor_status_includes_job_counts(client):
    mid = _create_monitor("Job Count Test")
    db = TestingSessionLocal()
    try:
        job = KumaJob(
            job_type="update",
            monitor_id=mid,
            monitor_name="Job Count Test",
            payload={"kuma_monitor_id": 1, "fields": {}},
            status="failed",
            created_at=datetime.utcnow(),
        )
        db.add(job)
        db.commit()

        resp = client.get(f"/monitors/{mid}/status", headers=HEADERS)
        assert resp.status_code == 200
        body = resp.json()
        assert body["failed_jobs"] == 1
        assert body["pending_jobs"] == 0
    finally:
        db.query(KumaJob).filter(KumaJob.monitor_id == mid).delete()
        db.commit()
        db.close()
        _delete_monitor(mid)


def test_monitor_status_not_found(client):
    resp = client.get("/monitors/999999/status", headers=HEADERS)
    assert resp.status_code == 404


# ── /jobs/status ──────────────────────────────────────────────────────────────

def test_jobs_status_returns_expected_shape(client):
    resp = client.get("/jobs/status", headers=HEADERS)
    assert resp.status_code == 200
    body = resp.json()
    assert "pending" in body
    assert "failed" in body
    assert "jobs" in body
    assert isinstance(body["jobs"], list)


def test_jobs_status_reflects_inserted_job(client):
    mid = _create_monitor("Jobs Status Test")
    db = TestingSessionLocal()
    try:
        job = KumaJob(
            job_type="pause",
            monitor_id=mid,
            monitor_name="Jobs Status Test",
            payload={"kuma_monitor_id": 99},
            status="pending",
            created_at=datetime.utcnow(),
        )
        db.add(job)
        db.commit()
        db.refresh(job)

        resp = client.get("/jobs/status", headers=HEADERS)
        assert resp.status_code == 200
        body = resp.json()
        assert body["pending"] >= 1
        job_ids = [j["id"] for j in body["jobs"]]
        assert job.id in job_ids
    finally:
        db.query(KumaJob).filter(KumaJob.monitor_id == mid).delete()
        db.commit()
        db.close()
        _delete_monitor(mid)
