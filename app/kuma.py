import logging
import threading
import time
import urllib.parse
from contextlib import contextmanager

import httpx
from engineio import exceptions as eio_exceptions
from socketio import exceptions as sio_exceptions

try:
    from uptime_kuma_api import UptimeKumaApi, MonitorType, UptimeKumaException
except ImportError as e:
    raise ImportError(
        "uptime-kuma-api-v2 is not installed. Run: uv add uptime-kuma-api-v2"
    ) from e

logger = logging.getLogger(__name__)

# Login against a busy Kuma legitimately exceeds a few seconds: Kuma pushes the
# full monitor list to every new client before answering the login call, so the
# handshake cost scales with the size of the Kuma install.
KUMA_TIMEOUT = 15

# How long a pooled session is kept before being recycled. Recycling bounds the
# socketio/engineio client's append-forever heartbeat buffers — without it the
# long-lived client slowly becomes its own leak — and doubles as the reconnect
# mechanism, since the next call after a teardown simply opens a fresh session.
MAX_SESSION_AGE = 600

# How long shutdown waits for an in-flight operation before giving up on closing
# the session. Docker's default stop window is 10s and a single Kuma call can take
# KUMA_TIMEOUT, so waiting is worse than skipping.
SHUTDOWN_LOCK_TIMEOUT = 2.0

_TRANSIENT_MESSAGE_PATTERNS = (
    "unable to connect",
    "connection",
    "timeout",
    "timed out",
    "not connected",
    "disconnected",
)


def is_transient_error(exc: BaseException) -> bool:
    """True for routine connectivity blips that need no stack trace.

    Kuma login timeouts and dropped sockets happen a few times an hour on a busy
    instance and are self-healing — logging a full traceback for each one buries
    the failures that actually need attention. Anything not matched here keeps
    exc_info=True.
    """
    if isinstance(exc, (
        sio_exceptions.TimeoutError,
        sio_exceptions.ConnectionError,
        sio_exceptions.DisconnectedError,
        sio_exceptions.BadNamespaceError,
        eio_exceptions.ConnectionError,
        eio_exceptions.SocketIsClosedError,
    )):
        return True
    # Builtin ConnectionError covers refused/reset/aborted; TimeoutError covers
    # socket timeouts. Deliberately not bare OSError — too broad to stay useful.
    if isinstance(exc, (TimeoutError, ConnectionError)):
        return True
    # Push heartbeats are plain HTTP, so their transport failures land here too.
    if isinstance(exc, httpx.TransportError):
        return True
    if isinstance(exc, UptimeKumaException):
        msg = str(exc).lower()
        return any(pattern in msg for pattern in _TRANSIENT_MESSAGE_PATTERNS)
    return False


def _close_transport(sio) -> None:
    """Force-close a client's transport resources regardless of what the
    socketio/engineio state machines think: both gate their own cleanup on
    connection state and can strand a live websocket (whose read-loop thread
    keeps the client alive) or the polling requests.Session. Closing an
    already-closed resource is a no-op. The attributes are engineio-internal,
    hence the getattrs."""
    eio = getattr(sio, "eio", None)
    for resource in (getattr(eio, "ws", None), getattr(eio, "http", None)):
        if resource is not None:
            try:
                resource.close()
            except Exception as exc:
                logger.warning("Kuma transport close failed: %s: %s", type(exc).__name__, exc)


# UptimeKumaApi.__init__ calls connect() before the caller can obtain a
# reference, so a handshake that fails partway ("unable to connect") strands
# a partially-connected client no caller-side teardown can reach — observed
# in production as ~0.5 leaked threads+sockets per hour, matching the
# "unable to connect" count in the logs. Wrap connect() so its failures
# clean up after themselves.
_orig_connect = UptimeKumaApi.connect


def _connect_with_cleanup(self):
    try:
        _orig_connect(self)
    except Exception:
        try:
            self.sio.shutdown()
        except Exception as exc:
            logger.warning("Kuma post-connect-failure shutdown failed: %s: %s", type(exc).__name__, exc)
        _close_transport(self.sio)
        raise


UptimeKumaApi.connect = _connect_with_cleanup


# Pooled session state. Every Kuma operation in this module borrows the one
# shared connection, serialized by _session_lock: the library's sync client
# shares per-event response buffers, so concurrent operations on one client
# interleave and hand each other's replies back. An RLock (not Lock) so a
# nested borrow degrades into reuse rather than deadlock.
# _state_lock guards _shutting_down only and is never held across I/O, so the
# shutdown gate takes effect immediately even while a Kuma call is in flight.
# Lock order is _state_lock before _session_lock; never the reverse.
_state_lock = threading.Lock()
_session_lock = threading.RLock()
_session: UptimeKumaApi | None = None
_session_creds: tuple[str, str, str] | None = None
_session_opened_at: float = 0.0
_shutting_down: bool = False


def _open_session(kuma_url: str, kuma_username: str, kuma_password: str) -> UptimeKumaApi:
    """Connect and authenticate one client, cleaning up if login fails.

    Auto-reconnection stays disabled: the library reconnects the socket but
    never re-runs login, so a reconnected client is authenticated in name only
    and every subsequent call fails. Recycling is our reconnect instead.
    """
    api = UptimeKumaApi(kuma_url, timeout=KUMA_TIMEOUT)
    api.sio.reconnection = False
    try:
        api.login(kuma_username, kuma_password)
    except Exception:
        _teardown_session(api)
        raise
    return api


def _teardown_session(api: UptimeKumaApi) -> None:
    """Close one client. Teardown goes through sio.shutdown() rather than the
    library's disconnect(): disconnect() is a no-op once the connection has
    already dropped (e.g. after a timeout), which leaves python-socketio's
    reconnect thread alive. Each abandoned client then reconnects on its own and
    buffers Kuma's heartbeat broadcasts forever — a thread and memory leak.
    shutdown() also aborts an in-progress reconnect attempt."""
    try:
        api.sio.shutdown()
    except Exception as exc:
        logger.warning("Kuma session shutdown failed: %s: %s", type(exc).__name__, exc)
    state = getattr(getattr(api.sio, "eio", None), "state", "disconnected")
    if state != "disconnected":
        logger.warning("Kuma session still %r after shutdown — force-closing transport", state)
    _close_transport(api.sio)


def close_session() -> None:
    """Tear down the pooled session, if any. Safe to call when none is open."""
    global _session, _session_creds, _session_opened_at
    with _session_lock:
        if _session is None:
            return
        api, _session = _session, None
        _session_creds = None
        _session_opened_at = 0.0
    _teardown_session(api)


def invalidate_session_if_transient(exc: BaseException) -> bool:
    """Drop the pooled session when a *caught* error looks like a dead connection.

    kuma_session() only recycles on exceptions that escape its yield, so a call
    site that catches and suppresses one inside the block would otherwise leave a
    dead connection pooled for the next borrower. Returns True if the session was
    dropped, which callers use to stop working through a connection that is gone.
    """
    if not is_transient_error(exc):
        return False
    close_session()
    return True


def shutdown_pool() -> None:
    """Refuse further pooled borrows, then close the session if it is free.

    The gate is set first and under its own lock, so it takes effect immediately:
    scheduler.shutdown(wait=False) leaves jobs running, and kuma_session() holds
    _session_lock across its whole block, so an in-flight operation can hold that
    lock for up to KUMA_TIMEOUT per call.

    Closing is then best-effort. Waiting on a busy session would push shutdown past
    Docker's 10s stop window and earn a SIGKILL, and closing the client underneath
    an active borrower is not safe — the sync socketio client does not support
    concurrent emits, and shutdown() sends disconnect packets before closing the
    transport. So if the session is busy we leave it: engineio starts its read and
    write loops with daemon=True, so they do not hold the process open.
    """
    global _shutting_down, _session, _session_creds, _session_opened_at
    with _state_lock:
        _shutting_down = True

    if not _session_lock.acquire(timeout=SHUTDOWN_LOCK_TIMEOUT):
        logger.warning(
            "Kuma session still busy after %.0fs — leaving it to process exit", SHUTDOWN_LOCK_TIMEOUT
        )
        return
    try:
        api, _session = _session, None
        _session_creds = None
        _session_opened_at = 0.0
    finally:
        _session_lock.release()

    if api is not None:
        _teardown_session(api)


def session_status() -> dict:
    """Introspection for the status endpoints: is a session pooled, and how old?"""
    with _session_lock:
        if _session is None:
            return {"open": False, "age_seconds": None}
        return {"open": True, "age_seconds": round(time.monotonic() - _session_opened_at, 1)}


@contextmanager
def kuma_session(kuma_url: str, kuma_username: str, kuma_password: str, fresh: bool = False):
    """Borrow the shared authenticated Kuma connection for one or more operations.

    The session is opened lazily, reused across calls, and recycled when it ages
    past MAX_SESSION_AGE, when the configured credentials change, or when an
    operation raises — a failed call is the signal that the connection may be
    dead, so it is dropped and the next caller reconnects.

    Pass fresh=True to force a dedicated short-lived connection that is never
    pooled. Only "test these credentials" wants this: reusing an already-open
    session would report success without proving anything.
    """
    global _session, _session_creds, _session_opened_at
    creds = (kuma_url, kuma_username, kuma_password)

    if fresh:
        api = _open_session(*creds)
        try:
            yield api
        finally:
            _teardown_session(api)
        return

    # Checked before taking _session_lock so a borrow is rejected immediately
    # rather than queueing behind an in-flight call. A borrow that passes this
    # check just as shutdown begins still opens a session; that is harmless,
    # since engineio's threads are daemon and die with the process.
    with _state_lock:
        if _shutting_down:
            raise RuntimeError("Kuma session pool is shutting down")

    with _session_lock:
        if _session is not None:
            if _session_creds != creds:
                logger.info("Kuma credentials changed — recycling session")
                close_session()
            elif time.monotonic() - _session_opened_at >= MAX_SESSION_AGE:
                logger.debug("Kuma session reached max age — recycling")
                close_session()

        if _session is None:
            _session = _open_session(*creds)
            _session_creds = creds
            _session_opened_at = time.monotonic()
            logger.debug("Kuma session opened")

        try:
            yield _session
        except Exception:
            close_session()
            raise


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
    with kuma_session(kuma_url, kuma_username, kuma_password) as api:
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
                logger.warning("Failed to apply tag %d to monitor %d: %s: %s", tag_id, kuma_monitor_id, type(exc).__name__, exc, exc_info=not is_transient_error(exc))
                # The connection is gone — the remaining tags would fail too, and
                # the dead session must not be left pooled for the next borrower.
                if invalidate_session_if_transient(exc):
                    break
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
    """Test Kuma connectivity and credentials. Blocking — raises on failure.

    Forces a dedicated connection: borrowing the pooled session would report
    success from an already-open connection without testing the credentials
    that were actually passed in.
    """
    with kuma_session(kuma_url, kuma_username, kuma_password, fresh=True):
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
