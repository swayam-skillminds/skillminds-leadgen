"""A small, dependency-free cron parser for recurring runs (Phase 3.1 schedules).

Five fields, as in crontab(5)::

    minute  hour  day-of-month  month  day-of-week
    0-59    0-23  1-31          1-12   0-6 (0 = Sunday; 7 is Sunday too)

Each field takes ``*``, numbers, ranges (``1-5``), lists (``1,15``), steps
(``*/15``, ``9-17/2``) and, for months and weekdays, names (``jan``,
``mon-fri``). Shortcuts: ``@hourly``, ``@daily`` (``@midnight``),
``@weekly``, ``@monthly``, ``@yearly`` (``@annually``).

As in Vixie cron, when both day-of-month and day-of-week are restricted, a
day matches if *either* does. Times are read in the schedule's IANA timezone
and returned in UTC.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

SHORTCUTS = {"@hourly": "0 * * * *", "@daily": "0 0 * * *", "@midnight": "0 0 * * *",
             "@weekly": "0 0 * * 0", "@monthly": "0 0 1 * *", "@yearly": "0 0 1 1 *",
             "@annually": "0 0 1 1 *"}
_MONTHS = {name: n for n, name in enumerate(
    ["jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"], 1)}
_DAYS = {name: n for n, name in enumerate(["sun", "mon", "tue", "wed", "thu", "fri", "sat"])}
_FIELDS = (("minute", 0, 59, {}), ("hour", 0, 23, {}), ("day of month", 1, 31, {}),
           ("month", 1, 12, _MONTHS), ("day of week", 0, 7, _DAYS))


class CronError(ValueError):
    """The expression (or its timezone) is not valid."""


def _value(text: str, names: dict[str, int], name: str) -> int:
    text = text.strip().lower()
    if text in names:
        return names[text]
    if not text.isdigit():
        raise CronError(f"{name}: {text!r} is not a number")
    return int(text)


def _field(text: str, low: int, high: int, names: dict[str, int], name: str) -> frozenset[int]:
    values: set[int] = set()
    for part in text.split(","):
        part = part.strip()
        if not part:
            raise CronError(f"{name}: empty list item")
        step = 1
        if "/" in part:
            part, step_text = part.split("/", 1)
            if not step_text.isdigit() or int(step_text) < 1:
                raise CronError(f"{name}: bad step {step_text!r}")
            step = int(step_text)
        if part == "*":
            start, end = low, high
        elif "-" in part:
            a, b = part.split("-", 1)
            start, end = _value(a, names, name), _value(b, names, name)
        else:
            start = _value(part, names, name)
            end = high if step > 1 else start
        if not (low <= start <= high and low <= end <= high) or start > end:
            raise CronError(f"{name}: {part!r} is outside {low}-{high}")
        values.update(range(start, end + 1, step))
    return frozenset(values)


@dataclass(frozen=True)
class Cron:
    expression: str
    minutes: frozenset[int]
    hours: frozenset[int]
    days: frozenset[int]
    months: frozenset[int]
    weekdays: frozenset[int]
    days_restricted: bool
    weekdays_restricted: bool

    @classmethod
    def parse(cls, expression: str) -> "Cron":
        text = (expression or "").strip().lower()
        text = SHORTCUTS.get(text, text)
        parts = text.split()
        if len(parts) != 5:
            raise CronError("a cron expression has 5 fields: minute hour day month weekday "
                            f"(got {len(parts)})")
        parsed = [_field(p, low, high, names, name)
                  for p, (name, low, high, names) in zip(parts, _FIELDS)]
        weekdays = frozenset(0 if d == 7 else d for d in parsed[4])
        return cls(expression=expression.strip(), minutes=parsed[0], hours=parsed[1],
                   days=parsed[2], months=parsed[3], weekdays=weekdays,
                   days_restricted=parts[2] != "*", weekdays_restricted=parts[4] != "*")

    def _day_matches(self, moment: datetime) -> bool:
        if moment.month not in self.months:
            return False
        dom = moment.day in self.days
        dow = (moment.isoweekday() % 7) in self.weekdays
        if self.days_restricted and self.weekdays_restricted:
            return dom or dow
        return dom and dow

    def next_after(self, after: datetime, tz: str = "UTC") -> datetime:
        """The first matching minute strictly after `after`, in UTC."""
        zone = zone_for(tz)
        if after.tzinfo is None:
            after = after.replace(tzinfo=timezone.utc)
        # Walk naive local wall time; attach the zone only to the answer.
        local = (after.astimezone(zone).replace(tzinfo=None, second=0, microsecond=0)
                 + timedelta(minutes=1))
        limit = local + timedelta(days=366 * 5)
        while local < limit:
            if not self._day_matches(local):
                local = (local + timedelta(days=1)).replace(hour=0, minute=0)
                continue
            if local.hour not in self.hours:
                local = (local + timedelta(hours=1)).replace(minute=0)
                continue
            if local.minute not in self.minutes:
                local += timedelta(minutes=1)
                continue
            answer = local.replace(tzinfo=zone).astimezone(timezone.utc)
            if answer > after:
                return answer
            local += timedelta(minutes=1)
        raise CronError(f"{self.expression!r} never matches")


def zone_for(name: str | None) -> ZoneInfo:
    try:
        return ZoneInfo(name or "UTC")
    except (ZoneInfoNotFoundError, ValueError):
        raise CronError(f"unknown timezone {name!r}") from None


def next_run(expression: str, after: datetime, tz: str = "UTC") -> datetime:
    return Cron.parse(expression).next_after(after, tz)
