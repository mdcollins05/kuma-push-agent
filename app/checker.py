import logging
import time
from datetime import datetime

import httpx

logger = logging.getLogger(__name__)


def run_check(monitor_id: int) -> None:
    """Health-check one monitor and push the result to Kuma. Runs in a thread pool."""
    from .database import SessionLocal
    from .kuma import build_push_url, create_push_monitor
    from .models import AppSettings, Monitor

    db = SessionLocal()
    try:
        monitor = db.get(Monitor, monitor_id)
        if not monitor or not monitor.enabled:
            return

        expected_codes = monitor.expected_codes or [200]
        start = time.monotonic()
        status = "down"
        msg = "Unknown error"
        elapsed_ms = 0

        try:
            with httpx.Client(verify=monitor.verify_ssl, timeout=10.0, follow_redirects=True) as client:
                resp = client.get(monitor.url)
            elapsed_ms = int((time.monotonic() - start) * 1000)

            ok = resp.status_code in expected_codes
            if ok and monitor.keyword:
                ok = monitor.keyword in resp.text
            if ok and monitor.max_response_ms and elapsed_ms > monitor.max_response_ms:
                ok = False
                msg = f"Response time {elapsed_ms} ms exceeded limit of {monitor.max_response_ms} ms"
            else:
                msg = "OK" if ok else f"HTTP {resp.status_code}"
            status = "up" if ok else "down"
        except Exception as exc:
            elapsed_ms = int((time.monotonic() - start) * 1000)
            status = "down"
            msg = str(exc)[:200]

        monitor.last_status = status
        monitor.last_check_time = datetime.utcnow()
        monitor.last_response_ms = elapsed_ms
        monitor.last_error = None if status == "up" else msg
        db.commit()

        app_cfg = db.get(AppSettings, 1)
        if not app_cfg or not app_cfg.configured or not app_cfg.kuma_url:
            return

        if not monitor.kuma_synced:
            try:
                from .kuma import get_push_token
                if not monitor.kuma_monitor_id:
                    kuma_id = create_push_monitor(
                        name=monitor.name,
                        interval=monitor.interval,
                        kuma_url=app_cfg.kuma_url,
                        kuma_username=app_cfg.kuma_username,
                        kuma_password=app_cfg.kuma_password,
                        notification_ids=monitor.notification_ids or None,
                    )
                    monitor.kuma_monitor_id = kuma_id
                    db.commit()  # persist ID before token fetch — prevents duplicate creation on retry

                push_token = get_push_token(
                    kuma_monitor_id=monitor.kuma_monitor_id,
                    kuma_url=app_cfg.kuma_url,
                    kuma_username=app_cfg.kuma_username,
                    kuma_password=app_cfg.kuma_password,
                )
                monitor.push_token = push_token
                monitor.kuma_synced = True
                db.commit()
            except Exception as exc:
                logger.warning("Kuma sync failed for monitor %d: %s", monitor_id, exc)
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
                logger.warning("Kuma push failed for monitor %d: %s", monitor_id, exc)
    finally:
        db.close()
