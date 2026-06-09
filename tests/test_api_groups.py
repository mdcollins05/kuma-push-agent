from tests.conftest import HEADERS


# ── GET /api/v1/groups ────────────────────────────────────────────────────────

def test_list_groups_returns_list(client):
    resp = client.get("/api/v1/groups", headers=HEADERS)
    assert resp.status_code == 200
    assert isinstance(resp.json(), list)


def test_list_groups_items_have_kuma_id_and_name(client):
    import app.group_cache as gc
    gc._cache = [
        {"kuma_id": 11, "name": "Production"},
        {"kuma_id": 22, "name": "Staging"},
    ]
    try:
        resp = client.get("/api/v1/groups", headers=HEADERS)
        assert resp.status_code == 200
        body = resp.json()
        assert len(body) == 2
        for item in body:
            assert "kuma_id" in item
            assert "name" in item
            assert isinstance(item["kuma_id"], int)
            assert isinstance(item["name"], str)
    finally:
        gc._cache = []
