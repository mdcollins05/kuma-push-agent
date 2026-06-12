import pytest

from app.dependencies import require_api_key
from app.main import app
from app.models import Monitor
from tests.conftest import HEADERS, TestingSessionLocal


# ── Helpers ──────────────────────────────────────────────────────────────────

def _payload(name: str, url: str = "https://example.com", *, interval: int = 60, **config_extra) -> dict:
    """Build a v0.3.0-shape MonitorCreate payload."""
    config = {"type": "http", "url": url, **config_extra}
    return {"name": name, "interval": interval, "config": config}


def _config_only(url: str = "https://example.com", **extra) -> dict:
    return {"type": "http", "url": url, **extra}


# ── Fixtures ──────────────────────────────────────────────────────────────────

@pytest.fixture
def monitor_id(client):
    resp = client.post("/api/v1/monitors", json=_payload("Fixture Monitor"), headers=HEADERS)
    assert resp.status_code == 201
    mid = resp.json()["id"]
    yield mid
    client.delete(f"/api/v1/monitors/{mid}", headers=HEADERS)


def _create_monitor_direct(name="Status Monitor", url="https://status.example.com") -> int:
    db = TestingSessionLocal()
    try:
        m = Monitor(name=name, interval=60, config=_config_only(url), enabled=True)
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
    resp = client.post("/api/v1/monitors", json=_payload("Create Test", "https://create.example.com"), headers=HEADERS)
    assert resp.status_code == 201
    body = resp.json()
    assert body["name"] == "Create Test"
    assert body["config"]["url"] == "https://create.example.com"
    assert body["config"]["type"] == "http"
    assert body["interval"] == 60
    assert body["enabled"] is True
    assert "id" in body
    client.delete(f"/api/v1/monitors/{body['id']}", headers=HEADERS)


def test_create_with_all_fields(client):
    payload = _payload(
        "Full Monitor", "https://full.example.com", interval=30,
        expected_codes=[200, 201], keyword="healthy", verify_ssl=False,
    )
    resp = client.post("/api/v1/monitors", json=payload, headers=HEADERS)
    assert resp.status_code == 201
    body = resp.json()
    assert body["interval"] == 30
    assert body["config"]["expected_codes"] == [200, 201]
    assert body["config"]["keyword"] == "healthy"
    assert body["config"]["verify_ssl"] is False
    client.delete(f"/api/v1/monitors/{body['id']}", headers=HEADERS)


def test_create_with_advanced_http_options(client):
    """HTTP method, headers, and body round-trip through create + read."""
    payload = _payload(
        "POST Monitor", "https://post.example.com",
        method="POST",
        headers={"Authorization": "Bearer xyz", "Content-Type": "application/json"},
        body='{"probe": true}',
    )
    resp = client.post("/api/v1/monitors", json=payload, headers=HEADERS)
    assert resp.status_code == 201
    body = resp.json()
    try:
        assert body["config"]["method"] == "POST"
        assert body["config"]["headers"] == {"Authorization": "Bearer xyz", "Content-Type": "application/json"}
        assert body["config"]["body"] == '{"probe": true}'
        # And confirm it round-trips through GET as well
        get = client.get(f"/api/v1/monitors/{body['id']}", headers=HEADERS)
        assert get.json()["config"]["method"] == "POST"
    finally:
        client.delete(f"/api/v1/monitors/{body['id']}", headers=HEADERS)


def test_create_rejects_unsupported_http_method(client):
    payload = _payload("Bad Method", "https://m.example.com", method="TRACE")
    resp = client.post("/api/v1/monitors", json=payload, headers=HEADERS)
    assert resp.status_code == 422


def test_create_defaults_http_method_to_get(client):
    resp = client.post("/api/v1/monitors", json=_payload("Default Method", "https://d.example.com"), headers=HEADERS)
    assert resp.status_code == 201
    body = resp.json()
    try:
        assert body["config"]["method"] == "GET"
        assert body["config"]["headers"] == {}
        assert body["config"]["body"] is None
    finally:
        client.delete(f"/api/v1/monitors/{body['id']}", headers=HEADERS)


def test_create_missing_required_fields(client):
    resp = client.post("/api/v1/monitors", json={}, headers=HEADERS)
    assert resp.status_code == 422


def test_create_missing_config_returns_422(client):
    resp = client.post("/api/v1/monitors", json={"name": "No Config"}, headers=HEADERS)
    assert resp.status_code == 422


def test_create_interval_too_short(client):
    resp = client.post("/api/v1/monitors", json=_payload("Fast", "https://x.com", interval=10) | {"interval": 10}, headers=HEADERS)
    assert resp.status_code == 422


def test_create_invalid_status_code(client):
    resp = client.post("/api/v1/monitors", json=_payload("Bad", "https://x.com", expected_codes=[999]), headers=HEADERS)
    assert resp.status_code == 422


def test_create_rejects_blank_url(client):
    resp = client.post("/api/v1/monitors", json=_payload("Blank URL", ""), headers=HEADERS)
    assert resp.status_code == 422


def test_create_rejects_whitespace_url(client):
    resp = client.post("/api/v1/monitors", json=_payload("Whitespace URL", "   "), headers=HEADERS)
    assert resp.status_code == 422


def test_create_rejects_empty_expected_codes(client):
    resp = client.post("/api/v1/monitors", json=_payload("Empty Codes", "https://x.com", expected_codes=[]), headers=HEADERS)
    assert resp.status_code == 422


def test_create_rejects_non_positive_max_response_ms(client):
    for bad in (0, -1):
        resp = client.post(
            "/api/v1/monitors",
            json=_payload(f"Bad MRT {bad}", "https://x.com", max_response_ms=bad),
            headers=HEADERS,
        )
        assert resp.status_code == 422, f"expected 422 for max_response_ms={bad}"


def test_create_persists_group_id(client):
    payload = _payload("Grouped Monitor", "https://grouped.example.com") | {"kuma_group_id": 42}
    resp = client.post("/api/v1/monitors", json=payload, headers=HEADERS)
    assert resp.status_code == 201
    body = resp.json()
    try:
        assert body["kuma_group_id"] == 42
    finally:
        client.delete(f"/api/v1/monitors/{body['id']}", headers=HEADERS)


def test_create_without_group_returns_null(client):
    resp = client.post("/api/v1/monitors", json=_payload("Top-Level Monitor", "https://top.example.com"), headers=HEADERS)
    assert resp.status_code == 201
    body = resp.json()
    try:
        assert body["kuma_group_id"] is None
    finally:
        client.delete(f"/api/v1/monitors/{body['id']}", headers=HEADERS)


def test_update_persists_group_id(client):
    create = client.post("/api/v1/monitors", json=_payload("Group Update Test", "https://groupupdate.example.com"), headers=HEADERS)
    assert create.status_code == 201
    mid = create.json()["id"]
    try:
        resp = client.put(
            f"/api/v1/monitors/{mid}",
            json=_payload("Group Update Test", "https://groupupdate.example.com") | {"kuma_group_id": 7},
            headers=HEADERS,
        )
        assert resp.status_code == 200
        assert resp.json()["kuma_group_id"] == 7

        resp = client.get(f"/api/v1/monitors/{mid}", headers=HEADERS)
        assert resp.json()["kuma_group_id"] == 7

        resp = client.put(
            f"/api/v1/monitors/{mid}",
            json=_payload("Group Update Test", "https://groupupdate.example.com") | {"kuma_group_id": None},
            headers=HEADERS,
        )
        assert resp.status_code == 200
        assert resp.json()["kuma_group_id"] is None
    finally:
        client.delete(f"/api/v1/monitors/{mid}", headers=HEADERS)


def test_create_persists_tag_and_notification_ids(client):
    payload = _payload("Tagged Monitor", "https://tagged.example.com") | {"tag_ids": [1, 2], "notification_ids": [10]}
    resp = client.post("/api/v1/monitors", json=payload, headers=HEADERS)
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
        json=_payload("Renamed", "https://renamed.example.com", interval=120),
        headers=HEADERS,
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["name"] == "Renamed"
    assert body["interval"] == 120
    assert body["config"]["url"] == "https://renamed.example.com"


def test_update_advanced_http_options(client, monitor_id):
    resp = client.put(
        f"/api/v1/monitors/{monitor_id}",
        json=_payload(
            "Fixture Monitor", "https://example.com",
            method="PUT", headers={"X-Test": "1"}, body="payload",
        ),
        headers=HEADERS,
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["config"]["method"] == "PUT"
    assert body["config"]["headers"] == {"X-Test": "1"}
    assert body["config"]["body"] == "payload"


def test_update_persists_tag_and_notification_ids(client):
    create = client.post("/api/v1/monitors", json=_payload("Update Tag Test", "https://updatetag.example.com"), headers=HEADERS)
    assert create.status_code == 201
    mid = create.json()["id"]
    try:
        resp = client.put(
            f"/api/v1/monitors/{mid}",
            json=_payload("Update Tag Test", "https://updatetag.example.com") | {"tag_ids": [5], "notification_ids": [20, 21]},
            headers=HEADERS,
        )
        assert resp.status_code == 200
        body = resp.json()
        assert body["tag_ids"] == [5]
        assert body["notification_ids"] == [20, 21]
    finally:
        client.delete(f"/api/v1/monitors/{mid}", headers=HEADERS)


def test_update_not_found(client):
    resp = client.put(
        "/api/v1/monitors/999999",
        json=_payload("X", "https://x.com"),
        headers=HEADERS,
    )
    assert resp.status_code == 404


# ── Delete ────────────────────────────────────────────────────────────────────

def test_delete_returns_204(client):
    resp = client.post("/api/v1/monitors", json=_payload("To Delete", "https://del.example.com"), headers=HEADERS)
    mid = resp.json()["id"]
    resp = client.delete(f"/api/v1/monitors/{mid}", headers=HEADERS)
    assert resp.status_code == 204


def test_delete_not_found(client):
    resp = client.delete("/api/v1/monitors/999999", headers=HEADERS)
    assert resp.status_code == 404


def test_deleted_monitor_not_in_list(client):
    resp = client.post("/api/v1/monitors", json=_payload("Temp", "https://temp.example.com"), headers=HEADERS)
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
        json=_payload("API Status Monitor", "https://api-status.example.com"),
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
        "last_response_ms", "kuma_synced", "kuma_monitor_id", "kuma_missing",
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


# ── Recreate Kuma monitor ─────────────────────────────────────────────────────

def test_recreate_kuma_clears_sync_state(client):
    """Recreate clears kuma_monitor_id/push_token/kuma_synced/kuma_missing on a previously synced monitor."""
    db = TestingSessionLocal()
    try:
        m = Monitor(
            name="Recreate Target", interval=60,
            config=_config_only("https://recreate.example.com"),
            kuma_monitor_id=42, push_token="oldtoken",
            kuma_synced=True, kuma_missing=True, enabled=True,
        )
        db.add(m)
        db.commit()
        mid = m.id
    finally:
        db.close()

    try:
        resp = client.post(f"/api/v1/monitors/{mid}/recreate-kuma", headers=HEADERS)
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["id"] == mid
        assert body["kuma_synced"] is False
        assert body["kuma_missing"] is False

        db = TestingSessionLocal()
        try:
            row = db.get(Monitor, mid)
            assert row.kuma_monitor_id is None
            assert row.push_token is None
            assert row.kuma_synced is False
            assert row.kuma_missing is False
        finally:
            db.close()
    finally:
        _delete_monitor_direct(mid)


def test_recreate_kuma_returns_404_for_missing_monitor(client):
    resp = client.post("/api/v1/monitors/999999/recreate-kuma", headers=HEADERS)
    assert resp.status_code == 404


def test_recreate_kuma_returns_409_when_never_synced(client):
    db = TestingSessionLocal()
    try:
        m = Monitor(
            name="Never Synced", interval=60,
            config=_config_only("https://never.example.com"),
            kuma_synced=False, enabled=True,
        )
        db.add(m)
        db.commit()
        mid = m.id
    finally:
        db.close()

    try:
        resp = client.post(f"/api/v1/monitors/{mid}/recreate-kuma", headers=HEADERS)
        assert resp.status_code == 409
    finally:
        _delete_monitor_direct(mid)


def test_monitor_response_includes_kuma_missing(client):
    """MonitorResponse exposes kuma_missing on create/get."""
    resp = client.post(
        "/api/v1/monitors",
        json=_payload("KM Field", "https://km.example.com"),
        headers=HEADERS,
    )
    assert resp.status_code == 201
    mid = resp.json()["id"]
    try:
        assert resp.json()["kuma_missing"] is False
        got = client.get(f"/api/v1/monitors/{mid}", headers=HEADERS)
        assert got.json()["kuma_missing"] is False
    finally:
        client.delete(f"/api/v1/monitors/{mid}", headers=HEADERS)
