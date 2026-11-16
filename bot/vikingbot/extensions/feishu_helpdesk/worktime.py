"""China business-time checks for Helpdesk escalation closures."""

from __future__ import annotations

from datetime import datetime, time
from zoneinfo import ZoneInfo

from chinese_calendar import is_workday

CHINA_TZ = ZoneInfo("Asia/Shanghai")


def is_business_time(
    timestamp: float,
    *,
    start: time = time(9, 0),
    end: time = time(18, 30),
) -> bool:
    """Return whether a timestamp is inside a China working day's service window."""
    current = datetime.fromtimestamp(float(timestamp), tz=CHINA_TZ)
    if not start <= current.time() < end:
        return False
    try:
        return bool(is_workday(current.date()))
    except NotImplementedError:
        # The package publishes the official calendar annually. Fall back to
        # weekdays for future years until its holiday data is upgraded.
        return current.weekday() < 5


__all__ = ["CHINA_TZ", "is_business_time"]
