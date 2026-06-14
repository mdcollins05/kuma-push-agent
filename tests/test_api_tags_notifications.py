from app.models import KumaTask
from tests.conftest import HEADERS, TestingSessionLocal


# ── GET /api/v1/tags ──────────────────────────────────────────────────────────

def test_list_tags_returns_list(client):
    resp = client.get("/api/v1/tags", headers=HEADERS)
    assert resp.status_code == 200
    assert isinstance(resp.json(), list)


# ── POST /api/v1/tags ─────────────────────────────────────────────────────────

def test_create_tag_without_kuma_queues_task(client):
    """Tag creation always queues — the processor holds the task until Kuma is reachable.
    Previously this returned 503 when Kuma wasn't configured, silently losing the request."""
    resp = client.post("/api/v1/tags", json={"name": "queued-prod", "color": "#3396FF"}, headers=HEADERS)
    assert resp.status_code == 202, resp.text
    body = resp.json()
    assert body["name"] == "queued-prod"
    assert body["id"] is None

    db = TestingSessionLocal()
    try:
        queued = db.query(KumaTask).filter(
            KumaTask.task_type == "create_tag",
            KumaTask.status == "pending",
        ).all()
        assert any(t.payload.get("name") == "queued-prod" for t in queued)
        for t in queued:
            if t.payload.get("name") == "queued-prod":
                db.delete(t)
        db.commit()
    finally:
        db.close()


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
