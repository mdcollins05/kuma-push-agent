"""Tests for update_cache — version comparison, refresh, and dev no-op."""
from unittest.mock import MagicMock, patch

import pytest


class TestIsNewer:
    def test_newer_patch(self):
        from app.update_cache import _is_newer
        assert _is_newer("0.2.1", "0.2.0") is True

    def test_newer_minor(self):
        from app.update_cache import _is_newer
        assert _is_newer("0.3.0", "0.2.0") is True

    def test_newer_major(self):
        from app.update_cache import _is_newer
        assert _is_newer("1.0.0", "0.2.0") is True

    def test_same_version(self):
        from app.update_cache import _is_newer
        assert _is_newer("0.2.0", "0.2.0") is False

    def test_older_version(self):
        from app.update_cache import _is_newer
        assert _is_newer("0.1.9", "0.2.0") is False

    def test_stable_vs_prerelease(self):
        from app.update_cache import _is_newer
        assert _is_newer("0.2.0", "0.2.0-dev.3") is True

    def test_invalid_version_does_not_crash(self):
        from app.update_cache import _is_newer
        assert _is_newer("not-a-version", "0.2.0") is False

    @pytest.mark.parametrize("current", ["0.4.0-dev.1", "0.4.0.dev1"])
    def test_prerelease_is_not_downgraded_to_older_stable(self, current):
        """A pre-release of an unreleased version must not offer an older stable as an
        upgrade. hatch-vcs normalises the "0.4.0-dev.1" git tag to PEP 440 "0.4.0.dev1"
        before the runtime sees it, so both spellings must compare the same way."""
        from app.update_cache import _is_newer
        assert _is_newer("0.3.1", current) is False

    @pytest.mark.parametrize("current", ["0.4.0-dev.1", "0.4.0.dev1"])
    def test_prerelease_still_sees_its_own_stable(self, current):
        from app.update_cache import _is_newer
        assert _is_newer("0.4.0", current) is True

    @pytest.mark.parametrize("current", ["0.4.0-dev.1", "0.4.0.dev1"])
    def test_prerelease_still_sees_later_release(self, current):
        from app.update_cache import _is_newer
        assert _is_newer("0.5.0", current) is True

    def test_later_prerelease_is_newer_than_earlier(self):
        from app.update_cache import _is_newer
        assert _is_newer("0.4.0-dev.2", "0.4.0-dev.1") is True
        assert _is_newer("0.4.0-dev.1", "0.4.0-dev.2") is False

    def test_prerelease_build_still_checks_for_updates(self):
        """A published pre-release is a real release — it must not be treated as an
        unreleased local build, or the badge would never appear."""
        from app.update_cache import _is_dev_version
        assert _is_dev_version("0.4.0.dev1") is False
        assert _is_dev_version("0.4.0-dev.1") is False


class TestRefresh:
    def test_dev_version_is_noop(self):
        import app.update_cache as uc
        uc._latest_version = None
        uc._update_available = False

        with patch("app.update_cache.APP_VERSION", "dev"):
            with patch("app.update_cache.httpx.Client") as mock_client:
                uc.refresh()
                mock_client.assert_not_called()

        assert uc._latest_version is None

    @pytest.mark.parametrize("version", [
        "0.0.0+unknown",        # hatch-vcs fallback when no tag and no env
        "0.3.1.dev1+g6186ca3",  # PEP 440 local-version: post-tag dev build
        "0.0.0",                # bare 0.0.0 — also unreleased
    ])
    def test_unreleased_build_is_noop(self, version):
        """Untagged / local-version builds must not trigger update checks or show the badge."""
        import app.update_cache as uc
        uc._latest_version = None
        uc._update_available = False

        with patch("app.update_cache.APP_VERSION", version):
            with patch("app.update_cache.httpx.Client") as mock_client:
                uc.refresh()
                mock_client.assert_not_called()

        assert uc._latest_version is None
        assert uc._update_available is False

    def test_update_available_when_newer_release(self):
        import app.update_cache as uc

        mock_resp = MagicMock()
        mock_resp.json.return_value = {"tag_name": "v0.3.0"}
        mock_resp.raise_for_status.return_value = None

        with patch("app.update_cache.APP_VERSION", "0.2.0"):
            with patch("app.update_cache.httpx.Client") as mock_client:
                mock_client.return_value.__enter__.return_value.get.return_value = mock_resp
                uc.refresh()

        assert uc._latest_version == "0.3.0"
        assert uc._update_available is True

    def test_no_update_when_current(self):
        import app.update_cache as uc

        mock_resp = MagicMock()
        mock_resp.json.return_value = {"tag_name": "v0.2.0"}
        mock_resp.raise_for_status.return_value = None

        with patch("app.update_cache.APP_VERSION", "0.2.0"):
            with patch("app.update_cache.httpx.Client") as mock_client:
                mock_client.return_value.__enter__.return_value.get.return_value = mock_resp
                uc.refresh()

        assert uc._latest_version == "0.2.0"
        assert uc._update_available is False

    def test_http_error_does_not_crash(self):
        import app.update_cache as uc
        uc._latest_version = None
        uc._update_available = False

        with patch("app.update_cache.APP_VERSION", "0.2.0"):
            with patch("app.update_cache.httpx.Client") as mock_client:
                mock_client.return_value.__enter__.return_value.get.side_effect = Exception("timeout")
                uc.refresh()  # must not raise

        assert uc._latest_version is None

    def test_get_returns_current_state(self):
        import app.update_cache as uc
        uc._latest_version = "0.3.0"
        uc._update_available = True

        result = uc.get()
        assert result == {"latest": "0.3.0", "update_available": True}


class TestPersistence:
    """latest_version is persisted to AppSettings.latest_version and restored on startup."""

    def test_refresh_persists_latest_version(self, client):
        import app.update_cache as uc
        import app.database as database_mod
        from app.models import AppSettings
        from tests.conftest import TestingSessionLocal

        monkeypatch_target = database_mod.SessionLocal
        database_mod.SessionLocal = TestingSessionLocal
        try:
            mock_resp = MagicMock()
            mock_resp.json.return_value = {"tag_name": "v0.4.0"}
            mock_resp.raise_for_status.return_value = None

            with patch("app.update_cache.APP_VERSION", "0.2.0"):
                with patch("app.update_cache.httpx.Client") as mock_client:
                    mock_client.return_value.__enter__.return_value.get.return_value = mock_resp
                    uc.refresh()

            db = TestingSessionLocal()
            try:
                cfg = db.get(AppSettings, 1)
                assert cfg.latest_version == "0.4.0"
                assert cfg.last_update_check is not None
            finally:
                db.close()
            # Cleanup so persisted values don't leak into later tests
            db = TestingSessionLocal()
            try:
                cfg = db.get(AppSettings, 1)
                cfg.latest_version = None
                cfg.last_update_check = None
                db.commit()
            finally:
                db.close()
        finally:
            database_mod.SessionLocal = monkeypatch_target

    def test_load_from_db_restores_last_check_without_version(self, client):
        """If the DB has a timestamp but no latest_version, _last_run still restores."""
        import app.update_cache as uc
        import app.database as database_mod
        from datetime import datetime
        from app.models import AppSettings
        from tests.conftest import TestingSessionLocal

        uc._latest_version = None
        uc._update_available = False
        uc._last_run = None

        check_dt = datetime(2026, 6, 12, 11, 0, 0)
        db = TestingSessionLocal()
        try:
            cfg = db.get(AppSettings, 1)
            cfg.latest_version = None
            cfg.last_update_check = check_dt
            db.commit()
        finally:
            db.close()

        monkeypatch_target = database_mod.SessionLocal
        database_mod.SessionLocal = TestingSessionLocal
        try:
            with patch("app.update_cache.APP_VERSION", "0.2.0"):
                uc.load_from_db()
        finally:
            database_mod.SessionLocal = monkeypatch_target

        assert uc._latest_version is None
        assert uc._update_available is False
        assert uc._last_run == check_dt.isoformat()

        db = TestingSessionLocal()
        try:
            cfg = db.get(AppSettings, 1)
            cfg.last_update_check = None
            db.commit()
        finally:
            db.close()

    def test_load_from_db_restores_state(self, client):
        import app.update_cache as uc
        import app.database as database_mod
        from datetime import datetime
        from app.models import AppSettings
        from tests.conftest import TestingSessionLocal

        uc._latest_version = None
        uc._update_available = False
        uc._last_run = None

        check_dt = datetime(2026, 6, 12, 10, 0, 0)
        db = TestingSessionLocal()
        try:
            cfg = db.get(AppSettings, 1)
            cfg.latest_version = "0.9.0"
            cfg.last_update_check = check_dt
            db.commit()
        finally:
            db.close()

        monkeypatch_target = database_mod.SessionLocal
        database_mod.SessionLocal = TestingSessionLocal
        try:
            with patch("app.update_cache.APP_VERSION", "0.2.0"):
                uc.load_from_db()
        finally:
            database_mod.SessionLocal = monkeypatch_target

        assert uc._latest_version == "0.9.0"
        assert uc._update_available is True
        assert uc._last_run == check_dt.isoformat()

        # Cleanup so the row doesn't leak into other tests
        db = TestingSessionLocal()
        try:
            cfg = db.get(AppSettings, 1)
            cfg.latest_version = None
            cfg.last_update_check = None
            db.commit()
        finally:
            db.close()

    def test_load_from_db_is_noop_for_dev_version(self, client):
        import app.update_cache as uc

        uc._latest_version = None
        uc._update_available = False
        with patch("app.update_cache.APP_VERSION", "dev"):
            uc.load_from_db()
        assert uc._latest_version is None
        assert uc._update_available is False

    def test_api_endpoint_returns_cached_state(self, client):
        """GET /api/v1/system/update mirrors the in-memory cache + adds release_url."""
        from tests.conftest import HEADERS
        import app.update_cache as uc

        uc._latest_version = "0.4.0"
        uc._update_available = True
        uc._last_run = "2026-06-12T10:00:00"
        try:
            with patch("app.update_cache.APP_VERSION", "0.2.0"), \
                 patch("app.routers.api.APP_VERSION", "0.2.0", create=True):
                resp = client.get("/api/v1/system/update", headers=HEADERS)
            assert resp.status_code == 200, resp.text
            body = resp.json()
            assert body["latest_version"] == "0.4.0"
            assert body["update_available"] is True
            assert body["last_check"] == "2026-06-12T10:00:00"
            assert body["release_url"] == "https://github.com/mdcollins05/kuma-push-agent/releases/tag/v0.4.0"
        finally:
            uc._latest_version = None
            uc._update_available = False
            uc._last_run = None

    def test_api_endpoint_omits_release_url_when_current(self, client):
        from tests.conftest import HEADERS
        import app.update_cache as uc

        uc._latest_version = "0.2.0"
        uc._update_available = False
        try:
            resp = client.get("/api/v1/system/update", headers=HEADERS)
            assert resp.status_code == 200
            body = resp.json()
            assert body["update_available"] is False
            assert body["release_url"] is None
        finally:
            uc._latest_version = None
            uc._update_available = False

    def test_load_from_db_handles_missing_row(self, client):
        """No persisted version → leaves module state untouched."""
        import app.update_cache as uc
        import app.database as database_mod
        from app.models import AppSettings
        from tests.conftest import TestingSessionLocal

        uc._latest_version = None
        uc._update_available = False

        db = TestingSessionLocal()
        try:
            cfg = db.get(AppSettings, 1)
            cfg.latest_version = None
            db.commit()
        finally:
            db.close()

        monkeypatch_target = database_mod.SessionLocal
        database_mod.SessionLocal = TestingSessionLocal
        try:
            with patch("app.update_cache.APP_VERSION", "0.2.0"):
                uc.load_from_db()
        finally:
            database_mod.SessionLocal = monkeypatch_target

        assert uc._latest_version is None
        assert uc._update_available is False
