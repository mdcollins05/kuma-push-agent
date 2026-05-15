import pathlib
from datetime import datetime
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from fastapi.templating import Jinja2Templates

templates = Jinja2Templates(directory=str(pathlib.Path(__file__).parent / "templates"))


def _local_dt(dt: datetime | None, tz_str: str | None = "UTC") -> str | None:
    if dt is None:
        return None
    try:
        tz = ZoneInfo(tz_str or "UTC")
    except ZoneInfoNotFoundError:
        tz = ZoneInfo("UTC")
    return dt.replace(tzinfo=ZoneInfo("UTC")).astimezone(tz).strftime("%Y-%m-%d %H:%M:%S")


templates.env.filters["local_dt"] = _local_dt
