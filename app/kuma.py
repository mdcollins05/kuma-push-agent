import logging
import urllib.parse
from contextlib import contextmanager

try:
    from uptime_kuma_api import UptimeKumaApi, MonitorType
except ImportError as e:
    raise ImportError(
        "uptime-kuma-api-v2 is not installed. Run: uv add uptime-kuma-api-v2"
    ) from e

logger = logging.getLogger(__name__)

KUMA_TIMEOUT = 10


@contextmanager
def kuma_session(kuma_url: str, kuma_username: str, kuma_password: str):
    """Open a single authenticated Kuma connection for multiple operations."""
    with UptimeKumaApi(kuma_url, timeout=KUMA_TIMEOUT) as api:
        api.login(kuma_username, kuma_password)
        yield api


def create_push_monitor(
    name: str,
    interval: int,
    kuma_url: str,
    kuma_username: str,
    kuma_password: str,
    notification_ids: list[int] | None = None,
) -> int:
    """Create a Push monitor in Kuma. Blocking — call via run_in_threadpool.
    Returns kuma_monitor_id only. Call get_push_token_and_apply_tags() to retrieve token and apply tags.
    """
    with UptimeKumaApi(kuma_url, timeout=KUMA_TIMEOUT) as api:
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
    with UptimeKumaApi(kuma_url, timeout=KUMA_TIMEOUT) as api:
        api.login(kuma_username, kuma_password)
        monitor_data = api.get_monitor(kuma_monitor_id)

    push_token = monitor_data.get("pushToken") or monitor_data.get("push_token", "")
    if not push_token:
        raise ValueError(f"Kuma returned no pushToken for monitor {kuma_monitor_id}. Response: {monitor_data}")
    return push_token


def get_push_token_and_apply_tags(
    kuma_monitor_id: int,
    tag_ids: list[int],
    kuma_url: str,
    kuma_username: str,
    kuma_password: str,
) -> str:
    """Fetch the push token and apply tag associations in a single connection. Blocking."""
    with UptimeKumaApi(kuma_url, timeout=KUMA_TIMEOUT) as api:
        api.login(kuma_username, kuma_password)
        monitor_data = api.get_monitor(kuma_monitor_id)
        push_token = monitor_data.get("pushToken") or monitor_data.get("push_token", "")
        if not push_token:
            raise ValueError(f"Kuma returned no pushToken for monitor {kuma_monitor_id}. Response: {monitor_data}")
        for tag_id in tag_ids:
            try:
                api.add_monitor_tag(tag_id=tag_id, monitor_id=kuma_monitor_id)
            except Exception as exc:
                logger.warning("Failed to apply tag %d to monitor %d: %s: %s", tag_id, kuma_monitor_id, type(exc).__name__, exc, exc_info=True)
    return push_token


def update_monitor(
    kuma_monitor_id: int,
    kuma_url: str,
    kuma_username: str,
    kuma_password: str,
    **kwargs,
) -> None:
    """Update fields on an existing Kuma monitor. Blocking — call via run_in_threadpool."""
    with UptimeKumaApi(kuma_url, timeout=KUMA_TIMEOUT) as api:
        api.login(kuma_username, kuma_password)
        api.edit_monitor(kuma_monitor_id, **kwargs)


def pause_monitor(
    kuma_monitor_id: int,
    kuma_url: str,
    kuma_username: str,
    kuma_password: str,
) -> None:
    """Pause a monitor in Kuma. Blocking — call via run_in_threadpool."""
    with UptimeKumaApi(kuma_url, timeout=KUMA_TIMEOUT) as api:
        api.login(kuma_username, kuma_password)
        api.pause_monitor(kuma_monitor_id)


def resume_monitor(
    kuma_monitor_id: int,
    kuma_url: str,
    kuma_username: str,
    kuma_password: str,
) -> None:
    """Resume a paused monitor in Kuma. Blocking — call via run_in_threadpool."""
    with UptimeKumaApi(kuma_url, timeout=KUMA_TIMEOUT) as api:
        api.login(kuma_username, kuma_password)
        api.resume_monitor(kuma_monitor_id)


def delete_monitor(
    kuma_monitor_id: int,
    kuma_url: str,
    kuma_username: str,
    kuma_password: str,
) -> None:
    """Delete a monitor from Kuma. Blocking — call via run_in_threadpool."""
    with UptimeKumaApi(kuma_url, timeout=KUMA_TIMEOUT) as api:
        api.login(kuma_username, kuma_password)
        api.delete_monitor(kuma_monitor_id)


def get_notifications(
    kuma_url: str,
    kuma_username: str,
    kuma_password: str,
) -> list[dict]:
    """Fetch all notification channels from Kuma. Blocking."""
    with UptimeKumaApi(kuma_url, timeout=KUMA_TIMEOUT) as api:
        api.login(kuma_username, kuma_password)
        return api.get_notifications()


def get_tags(
    kuma_url: str,
    kuma_username: str,
    kuma_password: str,
) -> list[dict]:
    """Fetch all tags from Kuma. Blocking."""
    with UptimeKumaApi(kuma_url, timeout=KUMA_TIMEOUT) as api:
        api.login(kuma_username, kuma_password)
        return api.get_tags()


def create_tag(
    name: str,
    color: str,
    kuma_url: str,
    kuma_username: str,
    kuma_password: str,
) -> dict:
    """Create a new tag in Kuma. Returns the created tag dict (includes `id`). Blocking."""
    with UptimeKumaApi(kuma_url, timeout=KUMA_TIMEOUT) as api:
        api.login(kuma_username, kuma_password)
        return api.add_tag(name=name, color=color)


def add_monitor_tag(
    kuma_monitor_id: int,
    tag_id: int,
    kuma_url: str,
    kuma_username: str,
    kuma_password: str,
) -> None:
    """Associate a tag with a Kuma monitor. Blocking."""
    with UptimeKumaApi(kuma_url, timeout=KUMA_TIMEOUT) as api:
        api.login(kuma_username, kuma_password)
        api.add_monitor_tag(tag_id=tag_id, monitor_id=kuma_monitor_id)


def delete_monitor_tag(
    kuma_monitor_id: int,
    tag_id: int,
    kuma_url: str,
    kuma_username: str,
    kuma_password: str,
) -> None:
    """Remove a tag from a Kuma monitor. Blocking."""
    with UptimeKumaApi(kuma_url, timeout=KUMA_TIMEOUT) as api:
        api.login(kuma_username, kuma_password)
        api.delete_monitor_tag(tag_id=tag_id, monitor_id=kuma_monitor_id)


def test_connection(
    kuma_url: str,
    kuma_username: str,
    kuma_password: str,
) -> None:
    """Test Kuma connectivity and credentials. Blocking — raises on failure."""
    with UptimeKumaApi(kuma_url, timeout=KUMA_TIMEOUT) as api:
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
