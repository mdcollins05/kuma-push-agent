"""Tests for _migrate_legacy_renames — the v0.1 kuma_jobs → kuma_tasks rename.

These cover the dangerous half-migrated states the previous broad-except
approach silently accepted. Each test builds a throwaway SQLite DB in a
clean state, runs the migration, and asserts the resulting schema.
"""
import pytest
import sqlalchemy as sa

from app.main import _migrate_legacy_renames


def _engine():
    """Fresh in-memory SQLite engine for each test."""
    return sa.create_engine("sqlite:///:memory:")


def _tables(engine) -> set[str]:
    return set(sa.inspect(engine).get_table_names())


def _columns(engine, table: str) -> set[str]:
    return {c["name"] for c in sa.inspect(engine).get_columns(table)}


# ── Happy paths ─────────────────────────────────────────────────────────────

def test_fresh_install_is_noop():
    """Neither table exists — migration does nothing; create_all handles it later."""
    engine = _engine()
    _migrate_legacy_renames(engine)
    assert _tables(engine) == set()


def test_only_legacy_table_is_renamed():
    """The v0.1 schema (kuma_jobs with job_type) is migrated end to end."""
    engine = _engine()
    with engine.connect() as conn:
        conn.execute(sa.text("CREATE TABLE kuma_jobs (id INTEGER PRIMARY KEY, job_type TEXT)"))
        conn.commit()
    _migrate_legacy_renames(engine)
    assert "kuma_tasks" in _tables(engine)
    assert "kuma_jobs" not in _tables(engine)
    assert "task_type" in _columns(engine, "kuma_tasks")
    assert "job_type" not in _columns(engine, "kuma_tasks")


def test_already_migrated_is_noop():
    """Post-migration shape (kuma_tasks with task_type) — nothing to do."""
    engine = _engine()
    with engine.connect() as conn:
        conn.execute(sa.text("CREATE TABLE kuma_tasks (id INTEGER PRIMARY KEY, task_type TEXT)"))
        conn.commit()
    _migrate_legacy_renames(engine)
    assert _tables(engine) == {"kuma_tasks"}
    assert _columns(engine, "kuma_tasks") == {"id", "task_type"}


def test_table_renamed_but_column_still_legacy_is_completed():
    """Partial state: table renamed earlier but column not yet — finish the job."""
    engine = _engine()
    with engine.connect() as conn:
        conn.execute(sa.text("CREATE TABLE kuma_tasks (id INTEGER PRIMARY KEY, job_type TEXT)"))
        conn.commit()
    _migrate_legacy_renames(engine)
    assert "task_type" in _columns(engine, "kuma_tasks")
    assert "job_type" not in _columns(engine, "kuma_tasks")


# ── Schema conflicts — must crash startup ────────────────────────────────────

def test_both_tables_present_but_old_empty_drops_old():
    """Common upgrade artefact: rename ran but the empty source table was left
    behind by the old broad-except code. Empty → safe to drop."""
    engine = _engine()
    with engine.connect() as conn:
        conn.execute(sa.text("CREATE TABLE kuma_jobs (id INTEGER PRIMARY KEY, job_type TEXT)"))
        conn.execute(sa.text("CREATE TABLE kuma_tasks (id INTEGER PRIMARY KEY, task_type TEXT)"))
        conn.commit()
    _migrate_legacy_renames(engine)
    assert _tables(engine) == {"kuma_tasks"}


def test_both_tables_present_with_data_raises():
    """If kuma_jobs still holds rows, dropping it would lose data — refuse to start."""
    engine = _engine()
    with engine.connect() as conn:
        conn.execute(sa.text("CREATE TABLE kuma_jobs (id INTEGER PRIMARY KEY, job_type TEXT)"))
        conn.execute(sa.text("CREATE TABLE kuma_tasks (id INTEGER PRIMARY KEY, task_type TEXT)"))
        conn.execute(sa.text("INSERT INTO kuma_jobs (job_type) VALUES ('legacy')"))
        conn.commit()
    with pytest.raises(RuntimeError, match="still has 1 row"):
        _migrate_legacy_renames(engine)


def test_both_columns_present_raises():
    """Tasks table somehow has both column names — refuse to guess which is authoritative."""
    engine = _engine()
    with engine.connect() as conn:
        conn.execute(sa.text(
            "CREATE TABLE kuma_tasks (id INTEGER PRIMARY KEY, job_type TEXT, task_type TEXT)"
        ))
        conn.commit()
    with pytest.raises(RuntimeError, match="both 'job_type' and 'task_type'"):
        _migrate_legacy_renames(engine)
