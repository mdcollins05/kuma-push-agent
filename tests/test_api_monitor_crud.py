import pytest

from app.dependencies import require_api_key
from app.main import app
from app.models import Monitor
from tests.conftest import HEADERS, TestingSessionLocal


# ── Fixtures ──────────────────────────────────────────────────────────────────

@pytest.fixture
def monitor_id(client):
    resp = client.post("/api/v1/monitors", json={"name": "Fixture Monitor", "url": "https://example.com"}, headers=HEADERS)
    assert resp.status_code == 201
    mid = resp.json()["id"]
    yield mid
    client.delete(f"/api/v1/monitors/{mid}", headers=HEADERS)


def _create_monitor_direct(name="Status Monitor", url="https://status.example.com") -> int:
    db = TestingSessionLocal()
    try:
        m = Monitor(name=name, url=url, interval=60, enabled=True)
        db.add(m)
        db.commit()
        db.refresh(m)
        return m.id
    finally:
        db.close()


def _delete_monitor_direct(monitor_id: int):
    db = TestingSessionLocal()
    try:
        m = db.get(Monitor, monitor_id)
        if m:
            db.delete(m)
            db.commit()
    finally:
        db.close()


# ── List ──────────────────────────────────────────────────────────────────────

def test_list_returns_200(client):
    resp = client.get("/api/v1/monitors", headers=HEADERS)
    assert resp.status_code == 200
    assert isinstance(resp.json(), list)


# ── Create ────────────────────────────────────────────────────────────────────

def test_create_returns_201(client):
    resp = client.post("/api/v1/monitors", json={"name": "Create Test", "url": "https://create.example.com"}, headers=HEADERS)
    assert resp.status_code == 201
    body = resp.json()
    assert body["name"] == "Create Test"
    assert body["url"] == "https://create.example.com"
    assert body["interval"] == 60
    assert body["enabled"] is True
    assert "id" in body
    client.delete(f"/api/v1/monitors/{body['id']}", headers=HEADERS)


def test_create_with_all_fields(client):
    payload = {
        "name": "Full Monitor",
        "url": "https://full.example.com",
        "interval": 30,
        "expected_codes": [200, 201],
        "keyword": "healthy",
        "verify_ssl": False,
    }
    resp = client.post("/api/v1/monitors", json=payload, headers=HEADERS)
    assert resp.status_code == 201
    body = resp.json()
    assert body["interval"] == 30
    assert body["expected_codes"] == [200, 201]
    assert body["keyword"] == "healthy"
    assert body["verify_ssl"] is False
    client.delete(f"/api/v1/monitors/{body['id']}", headers=HEADERS)


def test_create_missing_required_fields(client):
    resp = client.post("/api/v1/monitors", json={}, headers=HEADERS)
    assert resp.status_code == 422


def test_create_interval_too_short(client):
    resp = client.post("/api/v1/monitors", json={"name": "Fast", "url": "https://x.com", "interval": 10}, headers=HEADERS)
    assert resp.status_code == 422


def test_create_invalid_status_code(client):
    resp = client.post("/api/v1/monitors", json={"name": "Bad", "url": "https://x.com", "expected_codes": [999]}, headers=HEADERS)
    assert resp.status_code == 422


def test_create_persists_tag_and_notification_ids(client):
    resp = client.post("/api/v1/monitors", json={
        "name": "Tagged Monitor",
        "url": "https://tagged.example.com",
        "tag_ids": [1, 2],
        "notification_ids": [10],
    }, headers=HEADERS)
    assert resp.status_code == 201
    body = resp.json()
    try:
        assert body["tag_ids"] == [1, 2]
        assert body["notification_ids"] == [10]
    finally:
        client.delete(f"/api/v1/monitors/{body['id']}", headers=HEADERS)


# ── Get ───────────────────────────────────────────────────────────────────────

def test_get_returns_monitor(client, monitor_id):
    resp = client.get(f"/api/v1/monitors/{monitor_id}", headers=HEADERS)
    assert resp.status_code == 200
    assert resp.json()["id"] == monitor_id


def test_get_not_found(client):
    resp = client.get("/api/v1/monitors/999999", headers=HEADERS)
    assert resp.status_code == 404


# ── Update ────────────────────────────────────────────────────────────────────

def test_update_returns_updated_fields(client, monitor_id):
    resp = client.put(
        f"/api/v1/monitors/{monitor_id}",
        json={"name": "Renamed", "url": "https://renamed.example.com", "interval": 120},
        headers=HEADERS,
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["name"] == "Renamed"
    assert body["interval"] == 120


def test_update_persists_tag_and_notification_ids(client):
    create = client.post("/api/v1/monitors", json={
        "name": "Update Tag Test",
        "url": "https://updatetag.example.com",
    }, headers=HEADERS)
    assert create.status_code == 201
    mid = create.json()["id"]
    try:
        resp = client.put(f"/api/v1/monitors/{mid}", json={
            "name": "Update Tag Test",
            "url": "https://updatetag.example.com",
            "tag_ids": [5],
            "notification_ids": [20, 21],
        }, headers=HEADERS)
        assert resp.status_code == 200
        body = resp.json()
        assert body["tag_ids"] == [5]
        assert body["notification_ids"] == [20, 21]
    finally:
        client.delete(f"/api/v1/monitors/{mid}", headers=HEADERS)


def test_update_not_found(client):
    resp = client.put(
        "/api/v1/monitors/999999",
        json={"name": "X", "url": "https://x.com"},
        headers=HEADERS,
    )
    assert resp.status_code == 404


# ── Delete ────────────────────────────────────────────────────────────────────

def test_delete_returns_204(client):
    resp = client.post("/api/v1/monitors", json={"name": "To Delete", "url": "https://del.example.com"}, headers=HEADERS)
    mid = resp.json()["id"]
    resp = client.delete(f"/api/v1/monitors/{mid}", headers=HEADERS)
    assert resp.status_code == 204


def test_delete_not_found(client):
    resp = client.delete("/api/v1/monitors/999999", headers=HEADERS)
    assert resp.status_code == 404


def test_deleted_monitor_not_in_list(client):
    resp = client.post("/api/v1/monitors", json={"name": "Temp", "url": "https://temp.example.com"}, headers=HEADERS)
    mid = resp.json()["id"]
    client.delete(f"/api/v1/monitors/{mid}", headers=HEADERS)
    ids = [m["id"] for m in client.get("/api/v1/monitors", headers=HEADERS).json()]
    assert mid not in ids


# ── Auth ──────────────────────────────────────────────────────────────────────

def test_missing_api_key_returns_401(client):
    saved = app.dependency_overrides.pop(require_api_key)
    try:
        resp = client.get("/api/v1/monitors")
        assert resp.status_code == 401
    finally:
        app.dependency_overrides[require_api_key] = saved


# ── Status ────────────────────────────────────────────────────────────────────

@pytest.fixture
def status_monitor_id(client):
    resp = client.post(
        "/api/v1/monitors",
        json={"name": "API Status Monitor", "url": "https://api-status.example.com"},
        headers=HEADERS,
    )
    assert resp.status_code == 201
    mid = resp.json()["id"]
    yield mid
    client.delete(f"/api/v1/monitors/{mid}", headers=HEADERS)


def test_api_monitor_statuses_returns_list(client):
    resp = client.get("/api/v1/monitors/statuses", headers=HEADERS)
    assert resp.status_code == 200
    assert isinstance(resp.json(), list)


def test_api_monitor_statuses_has_expected_fields(client, status_monitor_id):
    resp = client.get("/api/v1/monitors/statuses", headers=HEADERS)
    assert resp.status_code == 200
    monitor = next((m for m in resp.json() if m["id"] == status_monitor_id), None)
    assert monitor is not None
    for field in (
        "id", "enabled", "last_status", "last_check_time",
        "last_response_ms", "kuma_synced", "kuma_monitor_id",
        "pending_jobs", "failed_jobs", "pending_create_tags",
    ):
        assert field in monitor, f"missing field: {field}"


def test_api_monitor_status_returns_correct_id(client, status_monitor_id):
    resp = client.get(f"/api/v1/monitors/{status_monitor_id}/status", headers=HEADERS)
    assert resp.status_code == 200
    assert resp.json()["id"] == status_monitor_id


def test_api_monitor_status_not_found(client):
    resp = client.get("/api/v1/monitors/999999/status", headers=HEADERS)
    assert resp.status_code == 404
