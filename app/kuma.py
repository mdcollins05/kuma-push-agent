import urllib.parse

try:
    from uptime_kuma_api import UptimeKumaApi, MonitorType
except ImportError as e:
    raise ImportError(
        "uptime-kuma-api-v2 is not installed. Run: uv add uptime-kuma-api-v2"
    ) from e


def create_push_monitor(
    name: str,
    interval: int,
    kuma_url: str,
    kuma_username: str,
    kuma_password: str,
    notification_ids: list[int] | None = None,
) -> int:
    """Create a Push monitor in Kuma. Blocking — call via run_in_threadpool.
    Returns kuma_monitor_id only. Call get_push_token() separately to retrieve the token.
    """
    import logging
    logger = logging.getLogger(__name__)
    with UptimeKumaApi(kuma_url, timeout=15) as api:
        api.login(kuma_username, kuma_password)
        kwargs = {}
        if notification_ids:
            kwargs["notificationIDList"] = {str(nid): True for nid in notification_ids}
        # Add a grace buffer so timing drift doesn't trigger false pending/down alerts.
        # Kuma interval = check interval + max(30s, 50% of check interval).
        kuma_interval = interval + max(30, interval // 2)
        result = api.add_monitor(
            type=MonitorType.PUSH,
            name=name,
            interval=kuma_interval,
            **kwargs,
        )
    logger.info("add_monitor result: %r", result)
    monitor_id = result.get("monitorID") or result.get("monitorId") or result.get("monitor_id")
    if not monitor_id:
        raise ValueError(f"Kuma add_monitor returned no monitor ID. Response: {result}")
    return monitor_id


def get_push_token(
    kuma_monitor_id: int,
    kuma_url: str,
    kuma_username: str,
    kuma_password: str,
) -> str:
    """Fetch the push token for an existing Kuma Push monitor. Blocking."""
    with UptimeKumaApi(kuma_url, timeout=15) as api:
        api.login(kuma_username, kuma_password)
        monitor_data = api.get_monitor(kuma_monitor_id)

    push_token = monitor_data.get("pushToken") or monitor_data.get("push_token", "")
    if not push_token:
        raise ValueError(f"Kuma returned no pushToken for monitor {kuma_monitor_id}. Response: {monitor_data}")
    return push_token


def update_monitor(
    kuma_monitor_id: int,
    kuma_url: str,
    kuma_username: str,
    kuma_password: str,
    **kwargs,
) -> None:
    """Update fields on an existing Kuma monitor. Blocking — call via run_in_threadpool."""
    with UptimeKumaApi(kuma_url, timeout=15) as api:
        api.login(kuma_username, kuma_password)
        api.edit_monitor(kuma_monitor_id, **kwargs)


def pause_monitor(
    kuma_monitor_id: int,
    kuma_url: str,
    kuma_username: str,
    kuma_password: str,
) -> None:
    """Pause a monitor in Kuma. Blocking — call via run_in_threadpool."""
    with UptimeKumaApi(kuma_url, timeout=15) as api:
        api.login(kuma_username, kuma_password)
        api.pause_monitor(kuma_monitor_id)


def resume_monitor(
    kuma_monitor_id: int,
    kuma_url: str,
    kuma_username: str,
    kuma_password: str,
) -> None:
    """Resume a paused monitor in Kuma. Blocking — call via run_in_threadpool."""
    with UptimeKumaApi(kuma_url, timeout=15) as api:
        api.login(kuma_username, kuma_password)
        api.resume_monitor(kuma_monitor_id)


def delete_monitor(
    kuma_monitor_id: int,
    kuma_url: str,
    kuma_username: str,
    kuma_password: str,
) -> None:
    """Delete a monitor from Kuma. Blocking — call via run_in_threadpool."""
    with UptimeKumaApi(kuma_url, timeout=15) as api:
        api.login(kuma_username, kuma_password)
        api.delete_monitor(kuma_monitor_id)


def get_notifications(
    kuma_url: str,
    kuma_username: str,
    kuma_password: str,
) -> list[dict]:
    """Fetch all notification channels from Kuma. Blocking."""
    with UptimeKumaApi(kuma_url, timeout=15) as api:
        api.login(kuma_username, kuma_password)
        return api.get_notifications()


def test_connection(
    kuma_url: str,
    kuma_username: str,
    kuma_password: str,
) -> None:
    """Test Kuma connectivity and credentials. Blocking — raises on failure."""
    with UptimeKumaApi(kuma_url, timeout=15) as api:
        api.login(kuma_username, kuma_password)


def build_push_url(
    kuma_url: str,
    push_token: str,
    status: str,
    msg: str,
    ping_ms: int,
) -> str:
    """Build the heartbeat push URL for a Push monitor."""
    params = urllib.parse.urlencode({"status": status, "msg": msg, "ping": ping_ms})
    base = kuma_url.rstrip("/")
    return f"{base}/api/push/{push_token}?{params}"
