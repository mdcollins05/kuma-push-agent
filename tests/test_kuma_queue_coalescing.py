"""Tests for kuma_queue.enqueue() coalescing.

When a new task supersedes an earlier one for the same monitor — repeated
update_monitor calls, pause-then-resume, delete-after-anything — the queue
should keep only the latest intent so an offline Kuma window doesn't
replay stale work after reconnect.
"""
import pytest

from app.kuma_queue import enqueue
from app.models import KumaTask, Monitor
from tests.conftest import TestingSessionLocal


# ── Helpers ──────────────────────────────────────────────────────────────────

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


def _pending(mid: int):
    db = TestingSessionLocal()
    try:
        return db.query(KumaTask).filter(
            KumaTask.monitor_id == mid, KumaTask.status == "pending"
        ).order_by(KumaTask.id).all()
    finally:
        db.close()


def _statuses(mid: int) -> list[tuple[str, str]]:
    """Return [(task_type, status)] in creation order for the monitor."""
    db = TestingSessionLocal()
    try:
        rows = db.query(KumaTask).filter(KumaTask.monitor_id == mid).order_by(KumaTask.id).all()
        return [(r.task_type, r.status) for r in rows]
    finally:
        db.close()


# ── update_monitor: latest replaces prior ────────────────────────────────────

def test_repeated_update_monitor_cancels_prior(client):
    mid = _make_monitor("Coalesce Update")
    try:
        db = TestingSessionLocal()
        try:
            enqueue(db, "update_monitor", {"fields": {"name": "v1"}}, monitor_id=mid)
            enqueue(db, "update_monitor", {"fields": {"name": "v2"}}, monitor_id=mid)
            enqueue(db, "update_monitor", {"fields": {"name": "v3"}}, monitor_id=mid)
        finally:
            db.close()

        statuses = _statuses(mid)
        assert statuses == [
            ("update_monitor", "cancelled"),
            ("update_monitor", "cancelled"),
            ("update_monitor", "pending"),
        ]
        # Final payload is the latest one.
        latest = _pending(mid)[-1]
        assert latest.payload["fields"]["name"] == "v3"
    finally:
        _cleanup(mid)


# ── pause / resume: opposite cancels the other ───────────────────────────────

def test_pause_then_resume_cancels_pause(client):
    mid = _make_monitor("Pause Resume")
    try:
        db = TestingSessionLocal()
        try:
            enqueue(db, "pause_monitor", {"kuma_monitor_id": 1}, monitor_id=mid)
            enqueue(db, "resume_monitor", {"kuma_monitor_id": 1}, monitor_id=mid)
        finally:
            db.close()
        assert _statuses(mid) == [("pause_monitor", "cancelled"), ("resume_monitor", "pending")]
    finally:
        _cleanup(mid)


def test_repeated_pause_keeps_only_latest(client):
    mid = _make_monitor("Double Pause")
    try:
        db = TestingSessionLocal()
        try:
            enqueue(db, "pause_monitor", {"kuma_monitor_id": 1}, monitor_id=mid)
            enqueue(db, "pause_monitor", {"kuma_monitor_id": 1}, monitor_id=mid)
        finally:
            db.close()
        assert _statuses(mid) == [("pause_monitor", "cancelled"), ("pause_monitor", "pending")]
    finally:
        _cleanup(mid)


# ── delete_monitor: cancels everything that came before ──────────────────────

def test_delete_cancels_all_prior_tasks(client):
    mid = _make_monitor("Delete Sweeps")
    try:
        db = TestingSessionLocal()
        try:
            enqueue(db, "update_monitor", {"fields": {}}, monitor_id=mid)
            enqueue(db, "pause_monitor", {"kuma_monitor_id": 1}, monitor_id=mid)
            enqueue(db, "update_tags", {"added": [1], "removed": []}, monitor_id=mid)
            enqueue(db, "delete_monitor", {"kuma_monitor_id": 1}, monitor_id=mid)
        finally:
            db.close()
        statuses = _statuses(mid)
        assert statuses[-1] == ("delete_monitor", "pending")
        # Everything else is cancelled.
        assert all(s == "cancelled" for _, s in statuses[:-1])
    finally:
        _cleanup(mid)


# ── update_tags: merge added/removed sets ────────────────────────────────────

def test_update_tags_merges_two_additive(client):
    """Two pending tag-adds for the same monitor merge into one."""
    mid = _make_monitor("Tags Add Add")
    try:
        db = TestingSessionLocal()
        try:
            enqueue(db, "update_tags", {"added": [1], "removed": []}, monitor_id=mid)
            enqueue(db, "update_tags", {"added": [2], "removed": []}, monitor_id=mid)
        finally:
            db.close()
        pending = _pending(mid)
        assert len(pending) == 1
        assert pending[0].payload["added"] == [1, 2]
        assert pending[0].payload["removed"] == []
    finally:
        _cleanup(mid)


def test_update_tags_later_remove_cancels_earlier_add(client):
    """The headline net-zero case: add T1, then remove T1 → merged to no-op,
    nothing left in the queue."""
    mid = _make_monitor("Tags Net Zero")
    try:
        db = TestingSessionLocal()
        try:
            enqueue(db, "update_tags", {"added": [1], "removed": []}, monitor_id=mid)
            enqueue(db, "update_tags", {"added": [], "removed": [1]}, monitor_id=mid)
        finally:
            db.close()
        statuses = _statuses(mid)
        assert all(s == "cancelled" for _, s in statuses)
        assert _pending(mid) == []
    finally:
        _cleanup(mid)


def test_update_tags_swap_collapses_to_noop(client):
    """Swap case (add T1+remove T2, then add T2+remove T1) is ambiguous without
    knowing Kuma's original state — coalescing conservatively merges to no-op.
    Documented limitation; the next user action will re-establish the right delta."""
    mid = _make_monitor("Tags Swap")
    try:
        db = TestingSessionLocal()
        try:
            enqueue(db, "update_tags", {"added": [1], "removed": [2]}, monitor_id=mid)
            enqueue(db, "update_tags", {"added": [2], "removed": [1]}, monitor_id=mid)
        finally:
            db.close()
        statuses = _statuses(mid)
        assert all(s == "cancelled" for _, s in statuses)
        assert _pending(mid) == []
    finally:
        _cleanup(mid)


# ── Independent task types are not coalesced ─────────────────────────────────

def test_create_tag_does_not_coalesce(client):
    """create_tag has no monitor_id, so it never coalesces — every call queues."""
    db = TestingSessionLocal()
    try:
        before = db.query(KumaTask).filter(
            KumaTask.task_type == "create_tag", KumaTask.status == "pending"
        ).count()
        enqueue(db, "create_tag", {"name": "a", "color": "#fff"})
        enqueue(db, "create_tag", {"name": "b", "color": "#fff"})
        after = db.query(KumaTask).filter(
            KumaTask.task_type == "create_tag", KumaTask.status == "pending"
        ).count()
        assert after == before + 2
    finally:
        # Clean up the two we inserted
        db.query(KumaTask).filter(
            KumaTask.task_type == "create_tag",
            KumaTask.status == "pending",
            KumaTask.payload["name"].as_string().in_(["a", "b"]),
        ).delete(synchronize_session=False)
        db.commit()
        db.close()


def test_pause_does_not_cancel_other_monitor(client):
    """Coalescing is monitor-scoped — a pause on monitor A doesn't touch monitor B."""
    mid_a = _make_monitor("Scope A")
    mid_b = _make_monitor("Scope B")
    try:
        db = TestingSessionLocal()
        try:
            enqueue(db, "pause_monitor", {"kuma_monitor_id": 10}, monitor_id=mid_a)
            enqueue(db, "pause_monitor", {"kuma_monitor_id": 20}, monitor_id=mid_b)
        finally:
            db.close()
        assert len(_pending(mid_a)) == 1
        assert len(_pending(mid_b)) == 1
    finally:
        _cleanup(mid_a)
        _cleanup(mid_b)
