import logging
import time
from datetime import datetime, timezone

import httpx

logger = logging.getLogger(__name__)


def _check_http(config: dict) -> tuple[str, str, int]:
    """Run an HTTP check and return (status, message, elapsed_ms)."""
    url = config.get("url") or ""
    method = (config.get("method") or "GET").upper()
    headers = config.get("headers") or None
    body = config.get("body")
    verify = config.get("verify_ssl", True)
    expected_codes = config.get("expected_codes") or [200]
    keyword = config.get("keyword")
    max_response_ms = config.get("max_response_ms")

    start = time.monotonic()
    try:
        with httpx.Client(verify=verify, timeout=10.0, follow_redirects=True) as client:
            resp = client.request(method, url, headers=headers, content=body if body else None)
        elapsed_ms = int((time.monotonic() - start) * 1000)

        code_ok = resp.status_code in expected_codes
        keyword_ok = (keyword in resp.text) if (code_ok and keyword) else True
        ok = code_ok and keyword_ok
        if ok and max_response_ms and elapsed_ms > max_response_ms:
            return "down", f"Response time {elapsed_ms} ms exceeded limit of {max_response_ms} ms", elapsed_ms
        if ok:
            return "up", "OK", elapsed_ms
        if not code_ok:
            return "down", f"HTTP {resp.status_code}", elapsed_ms
        return "down", f"Keyword \"{keyword}\" not found in response", elapsed_ms
    except Exception as exc:
        elapsed_ms = int((time.monotonic() - start) * 1000)
        return "down", str(exc)[:200], elapsed_ms


def run_check(monitor_id: int) -> None:
    """Health-check one monitor and push the result to Kuma. Runs in a thread pool."""
    from .database import SessionLocal
    from .kuma import build_push_url, create_push_monitor
    from .models import AppSettings, Monitor

    logger.info("run_check start: monitor_id=%d", monitor_id)
    db = SessionLocal()
    try:
        monitor = db.get(Monitor, monitor_id)
        if not monitor or not monitor.enabled:
            logger.info("run_check skip: monitor_id=%d not found or disabled", monitor_id)
            return

        config = monitor.config or {}
        check_type = config.get("type", "http")

        if check_type == "http":
            status, msg, elapsed_ms = _check_http(config)
        else:
            status, msg, elapsed_ms = "down", f"Unsupported check type: {check_type}", 0

        monitor.last_status = status
        monitor.last_check_time = datetime.now(timezone.utc).replace(tzinfo=None)
        monitor.last_response_ms = elapsed_ms
        monitor.last_error = None if status == "up" else msg
        db.commit()

        app_cfg = db.get(AppSettings, 1)
        if not app_cfg or not app_cfg.configured or not app_cfg.kuma_url:
            logger.info("run_check skip kuma sync: monitor_id=%d — not configured (configured=%s, kuma_url=%s)",
                        monitor_id, getattr(app_cfg, "configured", None), bool(getattr(app_cfg, "kuma_url", None)))
            return

        if not monitor.kuma_synced:
            logger.info("run_check kuma sync: monitor_id=%d kuma_monitor_id=%s", monitor_id, monitor.kuma_monitor_id)
            from .models import KumaTask
            sync_task = KumaTask(
                task_type="sync_monitor",
                payload={"monitor_id": monitor_id},
                monitor_name=monitor.name,
                monitor_id=monitor_id,
                status="pending",
            )
            db.add(sync_task)
            db.commit()
            try:
                if not monitor.kuma_monitor_id:
                    kuma_id = create_push_monitor(
                        name=monitor.name,
                        interval=monitor.interval,
                        kuma_url=app_cfg.kuma_url,
                        kuma_username=app_cfg.kuma_username,
                        kuma_password=app_cfg.kuma_password,
                        notification_ids=monitor.notification_ids or None,
                        parent=monitor.kuma_group_id,
                    )
                    monitor.kuma_monitor_id = kuma_id
                    db.commit()  # persist ID before token fetch — prevents duplicate creation on retry

                from .kuma import get_push_token_and_apply_tags
                push_token = get_push_token_and_apply_tags(
                    kuma_monitor_id=monitor.kuma_monitor_id,
                    tag_ids=monitor.tag_ids or [],
                    kuma_url=app_cfg.kuma_url,
                    kuma_username=app_cfg.kuma_username,
                    kuma_password=app_cfg.kuma_password,
                )
                monitor.push_token = push_token
                monitor.kuma_synced = True
                sync_task.status = "done"
                db.commit()
            except Exception as exc:
                logger.warning("Kuma sync failed for monitor %d: %s: %s", monitor_id, type(exc).__name__, exc, exc_info=True)
                sync_task.status = "failed"
                sync_task.error = f"{type(exc).__name__}: {exc}"[:1000]
                db.commit()
                return

        if monitor.push_token:
            try:
                push_url = build_push_url(
                    kuma_url=app_cfg.kuma_url,
                    push_token=monitor.push_token,
                    status=status,
                    msg=msg,
                    ping_ms=elapsed_ms,
                )
                with httpx.Client(timeout=5.0) as client:
                    client.get(push_url)
            except Exception as exc:
                logger.warning("Kuma push failed for monitor %d: %s: %s", monitor_id, type(exc).__name__, exc, exc_info=True)
    finally:
        db.close()
