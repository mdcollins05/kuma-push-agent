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

KUMA_TIMEOUT = 5


@contextmanager
def kuma_session(kuma_url: str, kuma_username: str, kuma_password: str):
    """Open a single authenticated Kuma connection for multiple operations.

    Auto-reconnection is disabled and teardown goes through sio.shutdown()
    rather than the library's disconnect(): disconnect() is a no-op once the
    connection has already dropped (e.g. after a timeout), which leaves
    python-socketio's reconnect thread alive. Each abandoned client then
    reconnects on its own and buffers Kuma's heartbeat broadcasts forever —
    a thread and memory leak. shutdown() also aborts an in-progress
    reconnect attempt.
    """
    api = UptimeKumaApi(kuma_url, timeout=KUMA_TIMEOUT)
    api.sio.reconnection = False
    try:
        api.login(kuma_username, kuma_password)
        yield api
    finally:
        try:
            api.sio.shutdown()
        except Exception as exc:
            logger.warning("Kuma session shutdown failed: %s: %s", type(exc).__name__, exc)


def create_push_monitor(
    name: str,
    interval: int,
    kuma_url: str,
    kuma_username: str,
    kuma_password: str,
    notification_ids: list[int] | None = None,
    parent: int | None = None,
) -> int:
    """Create a Push monitor in Kuma. Blocking — call via run_in_threadpool.
    Returns kuma_monitor_id only. Call get_push_token_and_apply_tags() to retrieve token and apply tags.
    """
    with kuma_session(kuma_url, kuma_username, kuma_password) as api:
        kwargs = {}
        if notification_ids:
            kwargs["notificationIDList"] = {str(nid): True for nid in notification_ids}
        if parent is not None:
            kwargs["parent"] = parent
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


def monitor_exists(
    kuma_monitor_id: int,
    kuma_url: str,
    kuma_username: str,
    kuma_password: str,
) -> bool:
    """Check whether a Kuma monitor still exists. Blocking — call via run_in_threadpool.
    Returns True if found, False if Kuma reports it missing. Raises on connection/auth
    errors so the caller can leave state untouched rather than wrongly resetting."""
    with UptimeKumaApi(kuma_url, timeout=KUMA_TIMEOUT) as api:
        api.login(kuma_username, kuma_password)
        try:
            api.get_monitor(kuma_monitor_id)
            return True
        except Exception as exc:
            msg = str(exc).lower()
            if "does not exist" in msg or "not found" in msg:
                return False
            raise


def get_push_token(
    kuma_monitor_id: int,
    kuma_url: str,
    kuma_username: str,
    kuma_password: str,
) -> str:
    """Fetch the push token for an existing Kuma Push monitor. Blocking."""
    with kuma_session(kuma_url, kuma_username, kuma_password) as api:
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
    with kuma_session(kuma_url, kuma_username, kuma_password) as api:
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
    with kuma_session(kuma_url, kuma_username, kuma_password) as api:
        api.edit_monitor(kuma_monitor_id, **kwargs)


def pause_monitor(
    kuma_monitor_id: int,
    kuma_url: str,
    kuma_username: str,
    kuma_password: str,
) -> None:
    """Pause a monitor in Kuma. Blocking — call via run_in_threadpool."""
    with kuma_session(kuma_url, kuma_username, kuma_password) as api:
        api.pause_monitor(kuma_monitor_id)


def resume_monitor(
    kuma_monitor_id: int,
    kuma_url: str,
    kuma_username: str,
    kuma_password: str,
) -> None:
    """Resume a paused monitor in Kuma. Blocking — call via run_in_threadpool."""
    with kuma_session(kuma_url, kuma_username, kuma_password) as api:
        api.resume_monitor(kuma_monitor_id)


def delete_monitor(
    kuma_monitor_id: int,
    kuma_url: str,
    kuma_username: str,
    kuma_password: str,
) -> None:
    """Delete a monitor from Kuma. Blocking — call via run_in_threadpool."""
    with kuma_session(kuma_url, kuma_username, kuma_password) as api:
        api.delete_monitor(kuma_monitor_id)


def get_notifications(
    kuma_url: str,
    kuma_username: str,
    kuma_password: str,
) -> list[dict]:
    """Fetch all notification channels from Kuma. Blocking."""
    with kuma_session(kuma_url, kuma_username, kuma_password) as api:
        return api.get_notifications()


def get_tags(
    kuma_url: str,
    kuma_username: str,
    kuma_password: str,
) -> list[dict]:
    """Fetch all tags from Kuma. Blocking."""
    with kuma_session(kuma_url, kuma_username, kuma_password) as api:
        return api.get_tags()


def get_groups(
    kuma_url: str,
    kuma_username: str,
    kuma_password: str,
) -> list[dict]:
    """Fetch all group-type monitors from Kuma. Blocking."""
    with kuma_session(kuma_url, kuma_username, kuma_password) as api:
        monitors = api.get_monitors()
    return [
        {"kuma_id": m["id"], "name": m["name"]}
        for m in monitors
        if m.get("type") == "group"
    ]


def create_tag(
    name: str,
    color: str,
    kuma_url: str,
    kuma_username: str,
    kuma_password: str,
) -> dict:
    """Create a new tag in Kuma. Returns the created tag dict (includes `id`). Blocking."""
    with kuma_session(kuma_url, kuma_username, kuma_password) as api:
        return api.add_tag(name=name, color=color)


def add_monitor_tag(
    kuma_monitor_id: int,
    tag_id: int,
    kuma_url: str,
    kuma_username: str,
    kuma_password: str,
) -> None:
    """Associate a tag with a Kuma monitor. Blocking."""
    with kuma_session(kuma_url, kuma_username, kuma_password) as api:
        api.add_monitor_tag(tag_id=tag_id, monitor_id=kuma_monitor_id)


def delete_monitor_tag(
    kuma_monitor_id: int,
    tag_id: int,
    kuma_url: str,
    kuma_username: str,
    kuma_password: str,
) -> None:
    """Remove a tag from a Kuma monitor. Blocking."""
    with kuma_session(kuma_url, kuma_username, kuma_password) as api:
        api.delete_monitor_tag(tag_id=tag_id, monitor_id=kuma_monitor_id)


def test_connection(
    kuma_url: str,
    kuma_username: str,
    kuma_password: str,
) -> None:
    """Test Kuma connectivity and credentials. Blocking — raises on failure."""
    with kuma_session(kuma_url, kuma_username, kuma_password):
        pass


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
