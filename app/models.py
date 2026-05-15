from datetime import datetime

from sqlalchemy import Boolean, Column, DateTime, Integer, JSON, String

from .database import Base


class Monitor(Base):
    __tablename__ = "monitors"

    id = Column(Integer, primary_key=True, autoincrement=True)
    name = Column(String, nullable=False, unique=True)

    url = Column(String, nullable=False)
    interval = Column(Integer, default=60)
    expected_codes = Column(JSON, default=lambda: [200])
    keyword = Column(String, nullable=True)
    max_response_ms = Column(Integer, nullable=True)
    notification_ids = Column(JSON, nullable=True, default=list)
    verify_ssl = Column(Boolean, default=True)

    kuma_monitor_id = Column(Integer, nullable=True)
    push_token = Column(String, nullable=True)
    kuma_synced = Column(Boolean, default=False)

    last_status = Column(String, nullable=True)
    last_check_time = Column(DateTime, nullable=True)
    last_response_ms = Column(Integer, nullable=True)
    last_error = Column(String, nullable=True)

    enabled = Column(Boolean, default=True)
    created_at = Column(DateTime, default=datetime.utcnow)


class KumaJob(Base):
    __tablename__ = "kuma_jobs"

    id = Column(Integer, primary_key=True, autoincrement=True)
    job_type = Column(String, nullable=False)        # update | pause | resume | delete
    monitor_id = Column(Integer, nullable=True)      # for querying per-monitor
    monitor_name = Column(String, nullable=True)     # cached for display
    payload = Column(JSON, nullable=False, default=dict)
    status = Column(String, default="pending")       # pending | done | failed | cancelled
    error = Column(String, nullable=True)
    retry_count = Column(Integer, default=0)
    next_retry_at = Column(DateTime, nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow)


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
