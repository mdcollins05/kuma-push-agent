from datetime import datetime

import pytest

from app import tag_cache, notification_cache, kuma_queue
from app.models import KumaTask, Monitor
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
        for field in ("id", "last_status", "last_check_time", "last_response_ms", "kuma_synced", "pending_tasks", "failed_tasks"):
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


def test_monitor_status_includes_task_counts(client):
    mid = _create_monitor("Task Count Test")
    db = TestingSessionLocal()
    try:
        task = KumaTask(
            task_type="update",
            monitor_id=mid,
            monitor_name="Task Count Test",
            payload={"kuma_monitor_id": 1, "fields": {}},
            status="failed",
            created_at=datetime.utcnow(),
        )
        db.add(task)
        db.commit()

        resp = client.get(f"/monitors/{mid}/status", headers=HEADERS)
        assert resp.status_code == 200
        body = resp.json()
        assert body["failed_tasks"] == 1
        assert body["pending_tasks"] == 0
    finally:
        db.query(KumaTask).filter(KumaTask.monitor_id == mid).delete()
        db.commit()
        db.close()
        _delete_monitor(mid)


def test_monitor_status_not_found(client):
    resp = client.get("/monitors/999999/status", headers=HEADERS)
    assert resp.status_code == 404


# ── /tasks/status ─────────────────────────────────────────────────────────────

def test_tasks_status_returns_expected_shape(client):
    resp = client.get("/tasks/status", headers=HEADERS)
    assert resp.status_code == 200
    body = resp.json()
    assert "pending" in body
    assert "failed" in body
    assert "tasks" in body
    assert isinstance(body["tasks"], list)


# ── /tasks/system-status ──────────────────────────────────────────────────────

SYSTEM_TASK_IDS = {"kuma_task_processor", "tag_cache_refresher", "notification_cache_refresher"}
SYSTEM_TASK_FIELDS = {"id", "label", "interval", "last_run", "last_error", "next_run"}


def test_system_status_returns_expected_shape(client):
    resp = client.get("/tasks/system-status", headers=HEADERS)
    assert resp.status_code == 200
    body = resp.json()
    assert "tasks" in body
    assert isinstance(body["tasks"], list)
    assert len(body["tasks"]) == 3
    for task in body["tasks"]:
        assert SYSTEM_TASK_FIELDS == set(task.keys())


def test_system_status_contains_all_task_ids(client):
    resp = client.get("/tasks/system-status", headers=HEADERS)
    assert resp.status_code == 200
    ids = {t["id"] for t in resp.json()["tasks"]}
    assert ids == SYSTEM_TASK_IDS


def test_system_status_next_run_is_string_or_none(client):
    resp = client.get("/tasks/system-status", headers=HEADERS)
    assert resp.status_code == 200
    for task in resp.json()["tasks"]:
        assert task["next_run"] is None or isinstance(task["next_run"], str)


def test_system_status_reflects_tag_cache_state(client):
    import app.tag_cache as tc
    with tc._lock:
        tc._last_run = datetime(2025, 1, 1, 12, 0, 0)
        tc._last_error = "TestError: boom"
    try:
        resp = client.get("/tasks/system-status", headers=HEADERS)
        assert resp.status_code == 200
        task = next(t for t in resp.json()["tasks"] if t["id"] == "tag_cache_refresher")
        assert task["last_run"] == "2025-01-01T12:00:00"
        assert task["last_error"] == "TestError: boom"
    finally:
        with tc._lock:
            tc._last_run = None
            tc._last_error = None


def test_system_status_ok_when_no_errors(client):
    resp = client.get("/tasks/system-status", headers=HEADERS)
    assert resp.status_code == 200
    for task in resp.json()["tasks"]:
        assert task["last_error"] is None


# ── /tasks/status reflects inserted task ──────────────────────────────────────

def test_tasks_status_reflects_inserted_task(client):
    mid = _create_monitor("Tasks Status Test")
    db = TestingSessionLocal()
    try:
        task = KumaTask(
            task_type="pause",
            monitor_id=mid,
            monitor_name="Tasks Status Test",
            payload={"kuma_monitor_id": 99},
            status="pending",
            created_at=datetime.utcnow(),
        )
        db.add(task)
        db.commit()
        db.refresh(task)

        resp = client.get("/tasks/status", headers=HEADERS)
        assert resp.status_code == 200
        body = resp.json()
        assert body["pending"] >= 1
        task_ids = [t["id"] for t in body["tasks"]]
        assert task.id in task_ids
    finally:
        db.query(KumaTask).filter(KumaTask.monitor_id == mid).delete()
        db.commit()
        db.close()
        _delete_monitor(mid)
