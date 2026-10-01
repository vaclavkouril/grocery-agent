"""Daily local-wall-clock scheduling with explicit daylight-saving behavior."""

from datetime import UTC, date, datetime, time, timedelta
from zoneinfo import ZoneInfo


def next_collection(
    now: datetime, daily_at: time, timezone: str, last_day: date | None = None
) -> datetime:
    if now.tzinfo is None or now.utcoffset() is None or daily_at.tzinfo is not None:
        raise ValueError("use an aware current time and a naive local collection time")
    zone = ZoneInfo(timezone)
    local_day = now.astimezone(zone).date()
    for offset in range(3):
        day = local_day + timedelta(days=offset)
        if last_day is not None and day <= last_day:
            continue
        # fold=0 selects the first occurrence of an ambiguous autumn time. A nonexistent
        # spring time round-trips forward, e.g. Prague 02:00 becomes 03:00 that day.
        planned = datetime.combine(day, daily_at, zone).replace(fold=0)
        candidate = planned.astimezone(UTC)
        if candidate > now:
            return candidate
    raise ValueError("last collection day is inconsistent with the current clock")
