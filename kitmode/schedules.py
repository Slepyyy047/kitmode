"""Pure calendar rules. UTC instants and local personal dates are separate concepts."""

from __future__ import annotations

from datetime import date, datetime, time, timedelta, timezone
from typing import Any
from zoneinfo import ZoneInfo

UTC = timezone.utc


def personal_day(now: datetime, tz: str | None, boundary: str) -> date:
    local = now.astimezone(ZoneInfo(tz or "UTC"))
    return local.date() - timedelta(days=int(local.time().replace(tzinfo=None) < time.fromisoformat(boundary)))


def dates(start: date, end: date) -> list[date]:
    return [start + timedelta(days=n) for n in range(max(0, (end - start).days + 1))]


def monday(day: date) -> date:
    return day - timedelta(days=day.weekday())


def scheduled(spec: dict[str, Any], day: date) -> bool:
    start = date.fromisoformat(spec["start"])
    if day < start or (spec.get("end") and day > date.fromisoformat(spec["end"])):
        return False
    rule = spec["schedule"]
    if rule["type"] in ("daily", "weekly"):
        return True
    if rule["type"] == "weekdays":
        return day.weekday() in rule["days"]
    return (day - start).days % rule["n"] == 0


def in_quiet(local_time: time, start: str, end: str) -> bool:
    a, b = time.fromisoformat(start), time.fromisoformat(end)
    current = local_time.replace(tzinfo=None)
    if a == b:
        return False
    return a <= current < b if a < b else current >= a or current < b


def wall_instant(day: date, hhmm: str, tz: str, boundary: str = "00:00") -> datetime:
    """First fold on autumn repeats; shift spring gaps forward to first valid minute."""
    wall_time = time.fromisoformat(hhmm)
    civil = day + timedelta(days=int(wall_time < time.fromisoformat(boundary)))
    naive = datetime.combine(civil, wall_time)
    zone = ZoneInfo(tz)
    for offset in range(181):
        candidate = (naive + timedelta(minutes=offset)).replace(tzinfo=zone, fold=0)
        instant = candidate.astimezone(UTC)
        if instant.astimezone(zone).replace(tzinfo=None) == candidate.replace(tzinfo=None):
            return instant
    raise ValueError("No valid wall-clock instant")
