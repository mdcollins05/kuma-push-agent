from apscheduler.executors.pool import ThreadPoolExecutor
from apscheduler.schedulers.background import BackgroundScheduler

scheduler = BackgroundScheduler(
    executors={"default": ThreadPoolExecutor(10)},
    job_defaults={"max_instances": 1, "coalesce": True},
)


def add_check_job(monitor_id: int, interval: int, last_check_time=None) -> None:
    from datetime import datetime, timedelta
    from .checker import run_check  # lazy import avoids circular dependency

    now = datetime.utcnow()
    if last_check_time is None:
        next_run = now
    else:
        next_run = last_check_time + timedelta(seconds=interval)
        if next_run <= now:
            next_run = now

    scheduler.add_job(
        run_check,
        "interval",
        seconds=interval,
        id=f"monitor_{monitor_id}",
        args=[monitor_id],
        replace_existing=True,
        next_run_time=next_run,
    )


def remove_check_job(monitor_id: int) -> None:
    job_id = f"monitor_{monitor_id}"
    if scheduler.get_job(job_id):
        scheduler.remove_job(job_id)


def pause_check_job(monitor_id: int) -> None:
    job_id = f"monitor_{monitor_id}"
    if scheduler.get_job(job_id):
        scheduler.pause_job(job_id)


def resume_check_job(monitor_id: int) -> None:
    job_id = f"monitor_{monitor_id}"
    if scheduler.get_job(job_id):
        scheduler.resume_job(job_id)


def start_kuma_queue_processor() -> None:
    from .kuma_queue import process_kuma_jobs

    scheduler.add_job(
        process_kuma_jobs,
        "interval",
        seconds=10,
        id="kuma_queue_processor",
        replace_existing=True,
    )


def start_notification_cache_refresher() -> None:
    from .notification_cache import refresh

    scheduler.add_job(
        refresh,
        "interval",
        minutes=5,
        id="notification_cache_refresher",
        replace_existing=True,
    )
    # Populate cache immediately on startup
    scheduler.add_job(refresh, "date", id="notification_cache_initial")
