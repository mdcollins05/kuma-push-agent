import pytest

from app.dependencies import require_api_key
from app.main import app
from tests.conftest import HEADERS


# ── Fixtures ──────────────────────────────────────────────────────────────────

@pytest.fixture
def monitor_id(client):
    """Create a monitor, yield its id, delete it after the test."""
    resp = client.post("/api/v1/monitors", json={"name": "Fixture Monitor", "url": "https://example.com"}, headers=HEADERS)
    assert resp.status_code == 201
    mid = resp.json()["id"]
    yield mid
    client.delete(f"/api/v1/monitors/{mid}", headers=HEADERS)


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
    # cleanup
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
    # Temporarily remove the override so the real auth check runs
    saved = app.dependency_overrides.pop(require_api_key)
    try:
        resp = client.get("/api/v1/monitors")  # no X-API-Key header
        assert resp.status_code == 401
    finally:
        app.dependency_overrides[require_api_key] = saved
