"""The app's scheduler must not run jobs during tests.

The TestClient lifespan starts APScheduler, and app.database.SessionLocal points
at the shared in-memory test DB. Creating a monitor schedules run_check to fire
immediately, so an unpaused scheduler runs it in a worker thread that loads and
commits the row while the test is still mutating it — the source of the flaky
`Could not refresh instance` / `StaleDataError` failures in the CRUD tests.
"""
import time
from unittest.mock import patch

from app.scheduler import scheduler
from tests.conftest import HEADERS


def test_creating_a_monitor_schedules_but_does_not_run_its_check(client):
    with patch("app.checker.run_check") as run_check:
        resp = client.post(
            "/api/v1/monitors",
            json={"name": "Isolation", "interval": 60, "config": {"type": "http", "url": "https://example.com"}},
            headers=HEADERS,
        )
        assert resp.status_code == 201, resp.text
        mid = resp.json()["id"]
        try:
            assert scheduler.get_job(f"monitor_{mid}") is not None, "job should still be registered"
            time.sleep(0.5)  # an unpaused scheduler fires a due job within milliseconds
            run_check.assert_not_called()
        finally:
            client.delete(f"/api/v1/monitors/{mid}", headers=HEADERS)
