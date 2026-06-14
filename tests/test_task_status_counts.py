"""Tests for task_status_counts() — the helper that powers the sync-warning banner.

The headline behaviour: a failed task whose intent was later satisfied by a
done task of the same task_type does NOT count toward failed_jobs. This
prevents the banner from staying red forever after a transient failure.
"""
from datetime import datetime, timezone

import pytest

from app.models import KumaTask, Monitor
from app.monitor_status import task_status_counts
from tests.conftest import TestingSessionLocal


def _make_monitor(name: str) -> int:
    db = TestingSessionLocal()
    try:
        m = Monitor(name=name, interval=60, config={"type": "http", "url": "https://x.test"}, enabled=True)
        db.add(m)
        db.commit()
        db.refresh(m)
        return m.id
    finally:
        db.close()


def _make_task(monitor_id: int, task_type: str, status: str) -> int:
    db = TestingSessionLocal()
    try:
        t = KumaTask(
            task_type=task_type, monitor_id=monitor_id, monitor_name="t",
            payload={}, status=status,
            created_at=datetime.now(timezone.utc).replace(tzinfo=None),
        )
        db.add(t)
        db.commit()
        db.refresh(t)
        return t.id
    finally:
        db.close()


def _cleanup(mid: int):
    db = TestingSessionLocal()
    try:
        db.query(KumaTask).filter(KumaTask.monitor_id == mid).delete()
        m = db.get(Monitor, mid)
        if m:
            db.delete(m)
        db.commit()
    finally:
        db.close()


def _counts(mid: int | None = None) -> dict:
    db = TestingSessionLocal()
    try:
        if mid is not None:
            return task_status_counts(db, [mid]).get(mid, {})
        return task_status_counts(db)
    finally:
        db.close()


def test_lone_pending(client):
    mid = _make_monitor("Lone Pending")
    try:
        _make_task(mid, "update_monitor", "pending")
        assert _counts(mid) == {"pending": 1}
    finally:
        _cleanup(mid)


def test_lone_failed(client):
    mid = _make_monitor("Lone Failed")
    try:
        _make_task(mid, "update_monitor", "failed")
        assert _counts(mid) == {"failed": 1}
    finally:
        _cleanup(mid)


def test_failed_then_done_same_type_clears_failed(client):
    """The headline fix: a later success of the same task_type shadows the failure."""
    mid = _make_monitor("Failed Then Done")
    try:
        _make_task(mid, "update_monitor", "failed")
        _make_task(mid, "update_monitor", "done")  # later id
        assert _counts(mid) == {}
    finally:
        _cleanup(mid)


def test_failed_then_done_different_type_does_not_shadow(client):
    """A success of a *different* task_type must NOT clear the failure."""
    mid = _make_monitor("Different Type")
    try:
        _make_task(mid, "update_monitor", "failed")
        _make_task(mid, "pause_monitor", "done")
        assert _counts(mid) == {"failed": 1}
    finally:
        _cleanup(mid)


def test_done_on_other_monitor_does_not_shadow(client):
    mid_a = _make_monitor("A")
    mid_b = _make_monitor("B")
    try:
        _make_task(mid_a, "update_monitor", "failed")
        _make_task(mid_b, "update_monitor", "done")
        assert _counts(mid_a) == {"failed": 1}
    finally:
        _cleanup(mid_a)
        _cleanup(mid_b)


def test_two_failures_one_mid_success_counts_only_the_later_failure(client):
    """failed → done → failed: the first failure is shadowed, the second isn't."""
    mid = _make_monitor("Two Failures Mid Success")
    try:
        _make_task(mid, "update_monitor", "failed")
        _make_task(mid, "update_monitor", "done")
        _make_task(mid, "update_monitor", "failed")
        assert _counts(mid) == {"failed": 1}
    finally:
        _cleanup(mid)


def test_earlier_done_does_not_shadow_later_failure(client):
    """done → failed: success is older than the failure, can't shadow it."""
    mid = _make_monitor("Done Then Failed")
    try:
        _make_task(mid, "update_monitor", "done")
        _make_task(mid, "update_monitor", "failed")
        assert _counts(mid) == {"failed": 1}
    finally:
        _cleanup(mid)


def test_pending_count_unaffected_by_done(client):
    """Pending tasks are always live work and aren't shadowed."""
    mid = _make_monitor("Pending Plus Done")
    try:
        _make_task(mid, "update_monitor", "pending")
        _make_task(mid, "update_monitor", "done")
        assert _counts(mid) == {"pending": 1}
    finally:
        _cleanup(mid)


def test_bulk_query_filters_by_monitor_ids(client):
    """Passing a monitor_ids list restricts the returned dict to those keys."""
    mid_a = _make_monitor("Bulk A")
    mid_b = _make_monitor("Bulk B")
    try:
        _make_task(mid_a, "update_monitor", "pending")
        _make_task(mid_b, "update_monitor", "failed")
        db = TestingSessionLocal()
        try:
            scoped = task_status_counts(db, [mid_a])
        finally:
            db.close()
        assert mid_a in scoped
        assert mid_b not in scoped
    finally:
        _cleanup(mid_a)
        _cleanup(mid_b)


def test_bulk_query_returns_all_monitors_when_unfiltered(client):
    mid_a = _make_monitor("All A")
    mid_b = _make_monitor("All B")
    try:
        _make_task(mid_a, "update_monitor", "pending")
        _make_task(mid_b, "update_monitor", "failed")
        all_counts = _counts(None)
        assert all_counts.get(mid_a, {}).get("pending") == 1
        assert all_counts.get(mid_b, {}).get("failed") == 1
    finally:
        _cleanup(mid_a)
        _cleanup(mid_b)
