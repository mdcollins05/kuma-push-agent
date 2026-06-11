"""Tests for seed_from_yaml — config validation, legacy flat-key fallback, and rejection of malformed entries."""
import textwrap
from unittest.mock import patch

import pytest

from app.models import Monitor
from app.seed import seed_from_yaml
from tests.conftest import TestingSessionLocal, testing_session_factory


@pytest.fixture(autouse=True)
def _clean_monitors(client):
    """Each test gets an empty monitors table — seed_from_yaml is a no-op when rows exist."""
    db = TestingSessionLocal()
    try:
        db.query(Monitor).delete()
        db.commit()
        yield
        db.query(Monitor).delete()
        db.commit()
    finally:
        db.close()


def _write(tmp_path, content: str) -> str:
    path = tmp_path / "seed.yaml"
    path.write_text(textwrap.dedent(content))
    return str(path)


def _seed(seed_file: str) -> list[Monitor]:
    db = TestingSessionLocal()
    try:
        seed_from_yaml(db, seed_file)
        return db.query(Monitor).order_by(Monitor.name).all()
    finally:
        db.close()


def test_legacy_flat_keys_are_wrapped_in_config(tmp_path):
    seed_file = _write(tmp_path, """
        monitors:
          - name: Flat A
            url: https://a.test
            interval: 30
            expected_codes: [200, 204]
            keyword: ok
    """)
    monitors = _seed(seed_file)
    assert len(monitors) == 1
    cfg = monitors[0].config
    assert cfg["type"] == "http"
    assert cfg["url"] == "https://a.test"
    assert cfg["expected_codes"] == [200, 204]
    assert cfg["keyword"] == "ok"


def test_nested_config_is_validated(tmp_path):
    seed_file = _write(tmp_path, """
        monitors:
          - name: Nested
            interval: 60
            config:
              type: http
              url: https://b.test
              method: POST
              headers:
                X-Test: "1"
              expected_codes: [200]
    """)
    monitors = _seed(seed_file)
    assert len(monitors) == 1
    assert monitors[0].config["method"] == "POST"
    assert monitors[0].config["headers"] == {"X-Test": "1"}


def test_blank_url_entry_is_skipped(tmp_path):
    """Entries that fail HttpConfig validation must NOT be persisted."""
    seed_file = _write(tmp_path, """
        monitors:
          - name: Blank URL
            url: ""
          - name: Good One
            url: https://good.test
    """)
    monitors = _seed(seed_file)
    names = [m.name for m in monitors]
    assert "Blank URL" not in names
    assert "Good One" in names


def test_empty_expected_codes_entry_is_skipped(tmp_path):
    seed_file = _write(tmp_path, """
        monitors:
          - name: No Codes
            url: https://a.test
            expected_codes: []
    """)
    monitors = _seed(seed_file)
    assert monitors == []


def test_non_positive_max_response_ms_is_skipped(tmp_path):
    seed_file = _write(tmp_path, """
        monitors:
          - name: Zero MRT
            url: https://a.test
            max_response_ms: 0
    """)
    monitors = _seed(seed_file)
    assert monitors == []
