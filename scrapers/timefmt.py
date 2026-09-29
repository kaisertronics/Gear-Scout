"""Times shown to the user: 12-hour clock in their own time zone (the
schedule's timezone in config.yaml, e.g. America/Los_Angeles), not UTC."""
from datetime import datetime, timezone
from typing import Optional, Union
from zoneinfo import ZoneInfo

DEFAULT_TZ = "America/Los_Angeles"


def local_time(value: Union[str, datetime, None], tz_name: Optional[str] = None,
               with_date: bool = True) -> str:
    """'2026-09-29T03:10:54+00:00' -> 'Sep 28, 8:10 PM PDT'. Times without a
    zone are treated as UTC (that's how Gear Scout stores them)."""
    if not value:
        return ""
    if isinstance(value, str):
        try:
            value = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return value
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    local = value.astimezone(ZoneInfo(tz_name or DEFAULT_TZ))
    clock = local.strftime("%I:%M %p").lstrip("0")
    zone = local.strftime("%Z")
    if not with_date:
        return f"{clock} {zone}"
    return f"{local.strftime('%b')} {local.day}, {clock} {zone}"
