"""Tests for the schema-migration error classifier in app.main.

The migration blocks must suppress known idempotent errors (duplicate column,
no such table, etc.) but re-raise anything else so real DB problems crash
startup loudly instead of leaving a half-broken schema.
"""
from sqlalchemy.exc import OperationalError

from app.main import _migration_already_applied


def _op_err(msg: str) -> OperationalError:
    """Build an OperationalError whose stringified form contains `msg`."""
    return OperationalError(statement="", params={}, orig=Exception(msg))


# ── Benign / idempotent patterns are suppressed ─────────────────────────────

def test_duplicate_column_name_is_benign():
    """ALTER TABLE ADD COLUMN run twice on SQLite."""
    assert _migration_already_applied(_op_err("duplicate column name: kuma_missing")) is True


def test_no_such_column_is_benign():
    """DROP COLUMN / RENAME COLUMN of an already-dropped column."""
    assert _migration_already_applied(_op_err("no such column: legacy_field")) is True


def test_no_such_table_is_benign():
    """ALTER on a table that doesn't exist yet (fresh install before create_all)."""
    assert _migration_already_applied(_op_err("no such table: kuma_jobs")) is True


def test_already_another_table_or_index_is_benign():
    """SQLite RENAME TO target that already exists."""
    assert _migration_already_applied(
        _op_err("there is already another table or index with this name: kuma_tasks")
    ) is True


def test_generic_already_exists_is_benign():
    """Other 'already exists' variants from CREATE/RENAME."""
    assert _migration_already_applied(_op_err("table kuma_tags already exists")) is True


def test_classifier_is_case_insensitive():
    """SQLite messages are lowercase but other drivers may not be."""
    assert _migration_already_applied(_op_err("DUPLICATE COLUMN NAME: anything")) is True


# ── Real failures must NOT be suppressed ────────────────────────────────────

def test_locked_database_is_not_benign():
    """A locked DB during migration is a hard problem — must crash startup."""
    assert _migration_already_applied(_op_err("database is locked")) is False


def test_syntax_error_is_not_benign():
    """A typo in a DDL string would be caught — must surface."""
    assert _migration_already_applied(_op_err("near \"COLUMNN\": syntax error")) is False


def test_disk_io_error_is_not_benign():
    assert _migration_already_applied(_op_err("disk I/O error")) is False


def test_constraint_failure_is_not_benign():
    """ALTER TABLE ... DEFAULT failing for an existing row is a real schema error."""
    assert _migration_already_applied(_op_err("NOT NULL constraint failed")) is False


def test_empty_message_is_not_benign():
    """Conservative: an empty / opaque error doesn't match any known pattern → must raise."""
    assert _migration_already_applied(_op_err("")) is False


def test_drop_column_syntax_error_is_not_benign():
    """Older SQLite (< 3.35) lacks DROP COLUMN — the v0.3 migration loop handles
    that specific message as a soft warning, NOT via the general classifier.
    The classifier must still reject it so an unexpected occurrence elsewhere
    surfaces."""
    assert _migration_already_applied(_op_err('near "DROP": syntax error')) is False
