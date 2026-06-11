"""Tests for tag_cache, notification_cache, and group_cache — load_from_db, refresh, and refresh endpoints."""
import pytest
from unittest.mock import patch

from app.models import KumaGroup, KumaNotification, KumaTag
from tests.conftest import HEADERS, testing_session_factory


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _patch_session():
    """Patch app.database.SessionLocal with the in-memory test session factory."""
    return patch("app.database.SessionLocal", testing_session_factory)


# ---------------------------------------------------------------------------
# notification_cache
# ---------------------------------------------------------------------------

class TestNotificationCacheLoadFromDb:
    def test_unconfigured_returns_empty_cache(self, db_session):
        # AppSettings.configured=False is the default in conftest
        import app.notification_cache as nc
        nc._cache = [{"id": 99, "name": "stale"}]  # pre-seed stale data

        with _patch_session():
            nc.load_from_db()

        assert nc.get() == []

    def test_configured_populates_cache_from_db(self, db_session):
        from app.models import AppSettings
        import app.notification_cache as nc

        cfg = db_session.get(AppSettings, 1)
        cfg.configured = True
        cfg.kuma_url = "http://kuma"
        db_session.add(KumaNotification(id=1, name="Email"))
        db_session.add(KumaNotification(id=2, name="Slack"))
        db_session.commit()

        try:
            with _patch_session():
                nc.load_from_db()

            cache = nc.get()
            assert len(cache) == 2
            assert {"id": 1, "name": "Email"} in cache
            assert {"id": 2, "name": "Slack"} in cache
        finally:
            db_session.query(KumaNotification).delete()
            cfg = db_session.get(AppSettings, 1)
            cfg.configured = False
            cfg.kuma_url = None
            db_session.commit()


class TestNotificationCacheRefresh:
    def test_unconfigured_clears_db_rows_and_cache(self, db_session):
        import app.notification_cache as nc

        # Seed stale rows that should be cleared
        db_session.add(KumaNotification(id=10, name="Old"))
        db_session.commit()
        nc._cache = [{"id": 10, "name": "Old"}]

        with _patch_session():
            nc.refresh()

        assert nc.get() == []
        assert db_session.query(KumaNotification).count() == 0

    def test_configured_syncs_cache_and_db(self, db_session):
        from app.models import AppSettings
        import app.notification_cache as nc

        cfg = db_session.get(AppSettings, 1)
        cfg.configured = True
        cfg.kuma_url = "http://kuma"
        cfg.kuma_username = "u"
        cfg.kuma_password = "p"
        db_session.commit()

        kuma_data = [{"id": 5, "name": "Webhook"}, {"id": 6, "name": "PagerDuty"}]

        try:
            with _patch_session(), patch("app.kuma.get_notifications", return_value=kuma_data):
                nc.refresh()

            cache = nc.get()
            assert len(cache) == 2
            assert {"id": 5, "name": "Webhook"} in cache
            assert db_session.query(KumaNotification).filter_by(id=5).count() == 1
        finally:
            db_session.query(KumaNotification).delete()
            cfg = db_session.get(AppSettings, 1)
            cfg.configured = False
            cfg.kuma_url = None
            cfg.kuma_username = None
            cfg.kuma_password = None
            db_session.commit()

    def test_raise_on_error_propagates_exception(self, db_session):
        from app.models import AppSettings
        import app.notification_cache as nc

        cfg = db_session.get(AppSettings, 1)
        cfg.configured = True
        cfg.kuma_url = "http://kuma"
        cfg.kuma_username = "u"
        cfg.kuma_password = "p"
        db_session.commit()

        try:
            with _patch_session(), patch("app.kuma.get_notifications", side_effect=RuntimeError("connection refused")):
                with pytest.raises(RuntimeError, match="connection refused"):
                    nc.refresh(raise_on_error=True)
        finally:
            cfg = db_session.get(AppSettings, 1)
            cfg.configured = False
            cfg.kuma_url = None
            cfg.kuma_username = None
            cfg.kuma_password = None
            db_session.commit()


# ---------------------------------------------------------------------------
# tag_cache
# ---------------------------------------------------------------------------

class TestTagCacheLoadFromDb:
    def test_unconfigured_returns_empty_cache(self, db_session):
        import app.tag_cache as tc
        tc._cache = [{"id": 99, "name": "stale", "color": "#fff"}]

        with _patch_session():
            tc.load_from_db()

        assert tc.get() == []

    def test_configured_populates_cache_from_db(self, db_session):
        from app.models import AppSettings
        import app.tag_cache as tc

        cfg = db_session.get(AppSettings, 1)
        cfg.configured = True
        cfg.kuma_url = "http://kuma"
        db_session.add(KumaTag(id=1, name="prod", color="#f00"))
        db_session.add(KumaTag(id=2, name="staging", color="#0f0"))
        db_session.commit()

        try:
            with _patch_session():
                tc.load_from_db()

            cache = tc.get()
            assert len(cache) == 2
            assert {"id": 1, "name": "prod", "color": "#f00"} in cache
        finally:
            db_session.query(KumaTag).delete()
            cfg = db_session.get(AppSettings, 1)
            cfg.configured = False
            cfg.kuma_url = None
            db_session.commit()


class TestTagCacheRefresh:
    def test_unconfigured_clears_db_rows_and_cache(self, db_session):
        import app.tag_cache as tc

        db_session.add(KumaTag(id=20, name="old", color="#aaa"))
        db_session.commit()
        tc._cache = [{"id": 20, "name": "old", "color": "#aaa"}]

        with _patch_session():
            tc.refresh()

        assert tc.get() == []
        assert db_session.query(KumaTag).count() == 0

    def test_configured_syncs_cache_and_db(self, db_session):
        from app.models import AppSettings
        import app.tag_cache as tc

        cfg = db_session.get(AppSettings, 1)
        cfg.configured = True
        cfg.kuma_url = "http://kuma"
        cfg.kuma_username = "u"
        cfg.kuma_password = "p"
        db_session.commit()

        kuma_data = [{"id": 7, "name": "prod", "color": "#f00"}]

        try:
            with _patch_session(), patch("app.kuma.get_tags", return_value=kuma_data):
                tc.refresh()

            cache = tc.get()
            assert len(cache) == 1
            assert {"id": 7, "name": "prod", "color": "#f00"} in cache
            assert db_session.query(KumaTag).filter_by(id=7).count() == 1
        finally:
            db_session.query(KumaTag).delete()
            cfg = db_session.get(AppSettings, 1)
            cfg.configured = False
            cfg.kuma_url = None
            cfg.kuma_username = None
            cfg.kuma_password = None
            db_session.commit()

    def test_raise_on_error_propagates_exception(self, db_session):
        from app.models import AppSettings
        import app.tag_cache as tc

        cfg = db_session.get(AppSettings, 1)
        cfg.configured = True
        cfg.kuma_url = "http://kuma"
        cfg.kuma_username = "u"
        cfg.kuma_password = "p"
        db_session.commit()

        try:
            with _patch_session(), patch("app.kuma.get_tags", side_effect=RuntimeError("timeout")):
                with pytest.raises(RuntimeError, match="timeout"):
                    tc.refresh(raise_on_error=True)
        finally:
            cfg = db_session.get(AppSettings, 1)
            cfg.configured = False
            cfg.kuma_url = None
            cfg.kuma_username = None
            cfg.kuma_password = None
            db_session.commit()


# ---------------------------------------------------------------------------
# group_cache
# ---------------------------------------------------------------------------

class TestGroupCacheRefresh:
    def test_unconfigured_clears_db_rows_and_cache(self, db_session):
        import app.group_cache as gc

        db_session.add(KumaGroup(id=30, name="stale"))
        db_session.commit()
        gc._cache = [{"kuma_id": 30, "name": "stale"}]

        with _patch_session():
            gc.refresh()

        assert gc.get() == []
        assert db_session.query(KumaGroup).count() == 0

    def test_configured_syncs_cache_and_db(self, db_session):
        from app.models import AppSettings
        import app.group_cache as gc

        cfg = db_session.get(AppSettings, 1)
        cfg.configured = True
        cfg.kuma_url = "http://kuma"
        cfg.kuma_username = "u"
        cfg.kuma_password = "p"
        db_session.commit()

        kuma_data = [{"kuma_id": 100, "name": "Production"}, {"kuma_id": 101, "name": "Staging"}]

        try:
            with _patch_session(), patch("app.kuma.get_groups", return_value=kuma_data):
                gc.refresh()

            cache = gc.get()
            assert len(cache) == 2
            assert {"kuma_id": 100, "name": "Production"} in cache
            assert db_session.query(KumaGroup).filter_by(id=100).count() == 1
        finally:
            db_session.query(KumaGroup).delete()
            cfg = db_session.get(AppSettings, 1)
            cfg.configured = False
            cfg.kuma_url = None
            cfg.kuma_username = None
            cfg.kuma_password = None
            db_session.commit()

    def test_raise_on_error_propagates_exception(self, db_session):
        from app.models import AppSettings
        import app.group_cache as gc

        cfg = db_session.get(AppSettings, 1)
        cfg.configured = True
        cfg.kuma_url = "http://kuma"
        cfg.kuma_username = "u"
        cfg.kuma_password = "p"
        db_session.commit()

        try:
            with _patch_session(), patch("app.kuma.get_groups", side_effect=RuntimeError("kaboom")):
                with pytest.raises(RuntimeError, match="kaboom"):
                    gc.refresh(raise_on_error=True)
        finally:
            cfg = db_session.get(AppSettings, 1)
            cfg.configured = False
            cfg.kuma_url = None
            cfg.kuma_username = None
            cfg.kuma_password = None
            db_session.commit()


# ---------------------------------------------------------------------------
# Refresh endpoints
# ---------------------------------------------------------------------------

class TestRefreshEndpoints:
    def test_notifications_refresh_unconfigured_returns_ok(self, client):
        # Kuma not configured → refresh() returns early without calling Kuma
        resp = client.post("/monitors/notifications/refresh", headers=HEADERS)
        assert resp.status_code == 200
        assert resp.json() == {"ok": True}

    def test_tags_refresh_unconfigured_returns_ok(self, client):
        resp = client.post("/monitors/tags/refresh", headers=HEADERS)
        assert resp.status_code == 200
        assert resp.json() == {"ok": True}

    def test_notifications_refresh_kuma_failure_returns_502(self, client):
        from app.models import AppSettings
        db = testing_session_factory()
        cfg = db.get(AppSettings, 1)
        cfg.configured = True
        cfg.kuma_url = "http://kuma"
        cfg.kuma_username = "u"
        cfg.kuma_password = "p"
        db.commit()
        db.close()

        try:
            with _patch_session(), patch("app.kuma.get_notifications", side_effect=RuntimeError("down")):
                resp = client.post("/monitors/notifications/refresh", headers=HEADERS)
            assert resp.status_code == 502
            assert resp.json()["ok"] is False
        finally:
            db = testing_session_factory()
            cfg = db.get(AppSettings, 1)
            cfg.configured = False
            cfg.kuma_url = None
            cfg.kuma_username = None
            cfg.kuma_password = None
            db.commit()
            db.close()

    def test_tags_refresh_kuma_failure_returns_502(self, client):
        from app.models import AppSettings
        db = testing_session_factory()
        cfg = db.get(AppSettings, 1)
        cfg.configured = True
        cfg.kuma_url = "http://kuma"
        cfg.kuma_username = "u"
        cfg.kuma_password = "p"
        db.commit()
        db.close()

        try:
            with _patch_session(), patch("app.kuma.get_tags", side_effect=RuntimeError("down")):
                resp = client.post("/monitors/tags/refresh", headers=HEADERS)
            assert resp.status_code == 502
            assert resp.json()["ok"] is False
        finally:
            db = testing_session_factory()
            cfg = db.get(AppSettings, 1)
            cfg.configured = False
            cfg.kuma_url = None
            cfg.kuma_username = None
            cfg.kuma_password = None
            db.commit()
            db.close()
