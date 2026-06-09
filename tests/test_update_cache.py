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
        # stable release should be considered newer than pre-release of same base
        from app.update_cache import _is_newer
        assert _is_newer("0.2.0", "0.2.0-dev.3") is False  # same base, stable wins on tuple

    def test_invalid_version_does_not_crash(self):
        from app.update_cache import _is_newer
        assert _is_newer("not-a-version", "0.2.0") is False


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
