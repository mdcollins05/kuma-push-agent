from datetime import datetime, timezone

from sqlalchemy import Boolean, Column, DateTime, Integer, JSON, String

from .database import Base


class Monitor(Base):
    __tablename__ = "monitors"

    id = Column(Integer, primary_key=True, autoincrement=True)
    name = Column(String, nullable=False, unique=True)

    interval = Column(Integer, default=60)
    config = Column(JSON, nullable=False)  # check-type-specific fields, includes "type" discriminator — callers must populate
    notification_ids = Column(JSON, nullable=True, default=list)
    tag_ids = Column(JSON, nullable=True, default=list)

    kuma_monitor_id = Column(Integer, nullable=True)
    kuma_group_id = Column(Integer, nullable=True)
    push_token = Column(String, nullable=True)
    kuma_synced = Column(Boolean, default=False)
    # Push GET returned 404 → Kuma monitor was deleted server-side. Cleared on push 200
    # or via the manual "Recreate" action which also resets kuma_synced for re-provisioning.
    kuma_missing = Column(Boolean, default=False)

    last_status = Column(String, nullable=True)
    last_check_time = Column(DateTime, nullable=True)
    last_response_ms = Column(Integer, nullable=True)
    last_error = Column(String, nullable=True)

    enabled = Column(Boolean, default=True)
    created_at = Column(DateTime, default=lambda: datetime.now(timezone.utc).replace(tzinfo=None))


class KumaTask(Base):
    __tablename__ = "kuma_tasks"

    id = Column(Integer, primary_key=True, autoincrement=True)
    task_type = Column(String, nullable=False)       # update_monitor | pause_monitor | resume_monitor | delete_monitor | sync_monitor | update_tags | create_tags | create_tag
    monitor_id = Column(Integer, nullable=True)      # for querying per-monitor
    monitor_name = Column(String, nullable=True)     # cached for display
    payload = Column(JSON, nullable=False, default=dict)
    status = Column(String, default="pending")       # pending | done | failed | cancelled
    error = Column(String, nullable=True)
    retry_count = Column(Integer, default=0)
    next_retry_at = Column(DateTime, nullable=True)
    created_at = Column(DateTime, default=lambda: datetime.now(timezone.utc).replace(tzinfo=None))


class KumaTag(Base):
    __tablename__ = "kuma_tags"

    id = Column(Integer, primary_key=True)  # Kuma's tag ID, not autoincrement
    name = Column(String, nullable=False)
    color = Column(String, nullable=False)


class KumaNotification(Base):
    __tablename__ = "kuma_notifications"

    id = Column(Integer, primary_key=True)  # Kuma's notification ID, not autoincrement
    name = Column(String, nullable=False)


class KumaGroup(Base):
    __tablename__ = "kuma_groups"

    id = Column(Integer, primary_key=True)  # Kuma's monitor ID, not autoincrement
    name = Column(String, nullable=False)


class AppSettings(Base):
    __tablename__ = "app_settings"

    id = Column(Integer, primary_key=True, default=1)

    kuma_url = Column(String, nullable=True)
    kuma_username = Column(String, nullable=True)
    kuma_password = Column(String, nullable=True)
    configured = Column(Boolean, default=False)

    ui_username = Column(String, nullable=True)
    ui_password_hash = Column(String, nullable=True)
    api_key = Column(String, nullable=True)
    timezone = Column(String, nullable=True, default="UTC")
    last_update_check = Column(DateTime, nullable=True)
