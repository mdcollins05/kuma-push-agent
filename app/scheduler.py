from apscheduler.executors.pool import ThreadPoolExecutor
from apscheduler.schedulers.background import BackgroundScheduler

# Kuma work runs in its own small pool so a slow or unreachable Kuma cannot
# occupy every worker and starve the monitor checks, which are the job this
# service actually exists to do. The three cache refreshers and the queue
# processor all serialize on the one pooled Kuma session anyway, so three
# threads is ample; more would only queue deeper on that lock.
scheduler = BackgroundScheduler(
    executors={"default": ThreadPoolExecutor(10), "kuma": ThreadPoolExecutor(3)},
    job_defaults={"max_instances": 1, "coalesce": True},
    timezone="UTC",
)


def add_check_job(monitor_id: int, interval: int, last_check_time=None) -> None:
    from datetime import datetime, timedelta, timezone
    from .checker import run_check  # lazy import avoids circular dependency

    now = datetime.now(timezone.utc).replace(tzinfo=None)
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


def start_kuma_task_processor() -> None:
    from .kuma_queue import process_kuma_tasks

    scheduler.add_job(
        process_kuma_tasks,
        "interval",
        seconds=10,
        id="kuma_task_processor",
        replace_existing=True,
        executor="kuma",
    )


def start_notification_cache_refresher() -> None:
    from .notification_cache import refresh

    scheduler.add_job(
        refresh,
        "interval",
        minutes=5,
        id="notification_cache_refresher",
        replace_existing=True,
        executor="kuma",
    )
    # Populate cache immediately on startup
    scheduler.add_job(refresh, "date", id="notification_cache_initial", executor="kuma")


def start_tag_cache_refresher() -> None:
    from datetime import datetime, timedelta, timezone
    from .tag_cache import refresh

    scheduler.add_job(
        refresh,
        "interval",
        minutes=5,
        id="tag_cache_refresher",
        replace_existing=True,
        executor="kuma",
    )
    # Stagger 5 s after notification cache so concurrent Socket.IO logins don't race.
    # Subsequent restarts serve tags instantly from DB via load_from_db() in lifespan.
    scheduler.add_job(
        refresh,
        "date",
        id="tag_cache_initial",
        executor="kuma",
        run_date=datetime.now(timezone.utc).replace(tzinfo=None) + timedelta(seconds=5),
    )


def start_group_cache_refresher() -> None:
    from datetime import datetime, timedelta
    from .group_cache import refresh

    scheduler.add_job(
        refresh,
        "interval",
        minutes=5,
        id="group_cache_refresher",
        replace_existing=True,
        executor="kuma",
    )
    # Stagger 10 s after notification cache (tag is at +5 s) so concurrent Socket.IO logins don't race.
    scheduler.add_job(
        refresh,
        "date",
        id="group_cache_initial",
        executor="kuma",
        run_date=datetime.utcnow() + timedelta(seconds=10),
    )


def start_update_checker() -> None:
    from datetime import datetime, timedelta
    from .update_cache import refresh, next_run_time

    scheduler.add_job(
        refresh,
        "interval",
        hours=6,
        id="update_checker",
        replace_existing=True,
    )
    scheduler.add_job(
        refresh,
        "date",
        id="update_checker_initial",
        run_date=next_run_time(),
    )
