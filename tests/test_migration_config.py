"""Test the v0.3.0 monitor-config migration on a legacy schema DB."""
import json

from sqlalchemy import create_engine, text


def test_migrates_legacy_columns_into_config(tmp_path):
    from app.main import _migrate_monitor_config

    db_path = tmp_path / "legacy.db"
    engine = create_engine(f"sqlite:///{db_path}")

    with engine.connect() as conn:
        conn.execute(text("""
            CREATE TABLE monitors (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL,
                url TEXT NOT NULL,
                interval INTEGER DEFAULT 60,
                expected_codes TEXT,
                keyword TEXT,
                max_response_ms INTEGER,
                verify_ssl BOOLEAN DEFAULT 1,
                config TEXT,
                notification_ids TEXT,
                tag_ids TEXT,
                kuma_monitor_id INTEGER,
                kuma_group_id INTEGER,
                push_token TEXT,
                kuma_synced BOOLEAN DEFAULT 0,
                last_status TEXT,
                last_check_time DATETIME,
                last_response_ms INTEGER,
                last_error TEXT,
                enabled BOOLEAN DEFAULT 1,
                created_at DATETIME
            )
        """))
        conn.execute(text("""
            INSERT INTO monitors (name, url, interval, expected_codes, keyword, max_response_ms, verify_ssl)
            VALUES
                ('Legacy A', 'https://a.test', 60, '[200, 204]', 'OK', 2000, 1),
                ('Legacy B', 'https://b.test', 120, '[200]', NULL, NULL, 0)
        """))
        conn.commit()

    _migrate_monitor_config(engine)

    with engine.connect() as conn:
        cols = {row[1] for row in conn.execute(text("PRAGMA table_info(monitors)")).fetchall()}
        for legacy in ("url", "expected_codes", "keyword", "max_response_ms", "verify_ssl"):
            assert legacy not in cols, f"legacy column {legacy} should have been dropped"
        assert "config" in cols

        rows = conn.execute(text("SELECT name, config FROM monitors ORDER BY name")).fetchall()
        assert len(rows) == 2

        cfg_a = json.loads(rows[0][1])
        assert cfg_a["type"] == "http"
        assert cfg_a["url"] == "https://a.test"
        assert cfg_a["method"] == "GET"
        assert cfg_a["expected_codes"] == [200, 204]
        assert cfg_a["keyword"] == "OK"
        assert cfg_a["max_response_ms"] == 2000
        assert cfg_a["verify_ssl"] is True

        cfg_b = json.loads(rows[1][1])
        assert cfg_b["url"] == "https://b.test"
        assert cfg_b["keyword"] is None
        assert cfg_b["max_response_ms"] is None
        assert cfg_b["verify_ssl"] is False


def test_migration_is_idempotent(tmp_path):
    """Running the migration twice on the same DB is safe."""
    from app.main import _migrate_monitor_config

    db_path = tmp_path / "legacy.db"
    engine = create_engine(f"sqlite:///{db_path}")

    with engine.connect() as conn:
        conn.execute(text("""
            CREATE TABLE monitors (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL,
                url TEXT NOT NULL,
                interval INTEGER DEFAULT 60,
                expected_codes TEXT,
                keyword TEXT,
                max_response_ms INTEGER,
                verify_ssl BOOLEAN DEFAULT 1,
                config TEXT
            )
        """))
        conn.execute(text("INSERT INTO monitors (name, url, expected_codes) VALUES ('Mon', 'https://x.test', '[200]')"))
        conn.commit()

    _migrate_monitor_config(engine)
    _migrate_monitor_config(engine)  # second run should be a no-op

    with engine.connect() as conn:
        row = conn.execute(text("SELECT config FROM monitors")).fetchone()
        cfg = json.loads(row[0])
        assert cfg["url"] == "https://x.test"


def test_migration_skips_when_no_legacy_columns(tmp_path):
    """Fresh schema (no legacy columns) is left untouched."""
    from app.main import _migrate_monitor_config

    db_path = tmp_path / "fresh.db"
    engine = create_engine(f"sqlite:///{db_path}")

    with engine.connect() as conn:
        conn.execute(text("""
            CREATE TABLE monitors (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL,
                config TEXT
            )
        """))
        conn.commit()

    _migrate_monitor_config(engine)  # should not raise
