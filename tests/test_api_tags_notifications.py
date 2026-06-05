from tests.conftest import HEADERS


# ── GET /api/v1/tags ──────────────────────────────────────────────────────────

def test_list_tags_returns_list(client):
    resp = client.get("/api/v1/tags", headers=HEADERS)
    assert resp.status_code == 200
    assert isinstance(resp.json(), list)


# ── POST /api/v1/tags ─────────────────────────────────────────────────────────

def test_create_tag_without_kuma_returns_503(client):
    # conftest seeds configured=False so Kuma calls are skipped
    resp = client.post("/api/v1/tags", json={"name": "prod", "color": "#3396FF"}, headers=HEADERS)
    assert resp.status_code == 503


# ── GET /api/v1/notifications ─────────────────────────────────────────────────

def test_list_notifications_returns_list(client):
    resp = client.get("/api/v1/notifications", headers=HEADERS)
    assert resp.status_code == 200
    assert isinstance(resp.json(), list)


def test_list_notifications_items_have_id_and_name(client):
    resp = client.get("/api/v1/notifications", headers=HEADERS)
    assert resp.status_code == 200
    for item in resp.json():
        assert "id" in item
        assert "name" in item
