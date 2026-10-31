"""A small, dependency-free five-field cron parser.

Schedules are evaluated in the UTC offset captured when the schedule is saved. DST changes are
therefore not re-interpreted silently: a schedule keeps the offset it recorded, and `monitor
status` prints that offset so a stale schedule is visible rather than guessed.

Only the standard five fields are accepted (minute, hour, day-of-month, month, day-of-week),
with lists, ranges, steps and month/weekday names. `@daily` and the other conventional aliases
are accepted as shorthand. The parser refuses an expression that produces no occurrence within
six years, so `0 0 30 2 *` is rejected at creation instead of silently never running.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta

MONTH_NAMES = {
    name: index for index, name in enumerate(
        ("jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"), 1)
}
DAY_NAMES = {
    name: index for index, name in enumerate(
        ("sun", "mon", "tue", "wed", "thu", "fri", "sat"), 0)
}
ALIASES = {
    "@yearly": "0 0 1 1 *",
    "@annually": "0 0 1 1 *",
    "@monthly": "0 0 1 * *",
    "@weekly": "0 0 * * 0",
    "@daily": "0 0 * * *",
    "@midnight": "0 0 * * *",
    "@hourly": "0 * * * *",
}
SEARCH_DAYS = 366 * 6


class CronError(ValueError):
    """A schedule expression that cannot be parsed or has no occurrence in six years."""


@dataclass(frozen=True)
class Field:
    values: frozenset[int]
    wildcard: bool

    def matches(self, value: int) -> bool:
        return value in self.values


def _value(token: str, low: int, high: int, names: dict[str, int], label: str) -> int:
    text = token.strip().lower()
    if text in names:
        return names[text]
    try:
        number = int(text)
    except ValueError as exc:
        raise CronError(f"{label}: '{token}' is not a number or a known name") from exc
    if number < low or number > high:
        raise CronError(f"{label}: {number} is outside {low}-{high}")
    return number


def _field(text: str, low: int, high: int, label: str,
           names: dict[str, int] | None = None) -> Field:
    names = names or {}
    raw = (text or "").strip().lower()
    if not raw:
        raise CronError(f"{label}: the field is empty")
    values: set[int] = set()
    for part in raw.split(","):
        part = part.strip()
        if not part:
            raise CronError(f"{label}: an empty list item is not allowed")
        base, slash, step_text = part.partition("/")
        if slash:
            try:
                step = int(step_text)
            except ValueError as exc:
                raise CronError(f"{label}: step '{step_text}' is not an integer") from exc
            if step <= 0:
                raise CronError(f"{label}: the step must be positive")
        else:
            step = 1
        if base == "*":
            start, end = low, high
        elif "-" in base:
            first, _, second = base.partition("-")
            start = _value(first, low, high, names, label)
            end = _value(second, low, high, names, label)
            if start > end:
                raise CronError(f"{label}: range '{base}' runs backwards")
        else:
            start = _value(base, low, high, names, label)
            end = high if slash else start
        values.update(range(start, end + 1, step))
    return Field(frozenset(values), wildcard=raw == "*")


@dataclass(frozen=True)
class CronExpression:
    expression: str
    minute: Field
    hour: Field
    dom: Field
    month: Field
    dow: Field

    @classmethod
    def parse(cls, expression: str, *, validate: bool = True) -> "CronExpression":
        text = (expression or "").strip()
        if not text:
            raise CronError("the schedule expression is empty")
        text = ALIASES.get(text.lower(), text)
        fields = text.split()
        if len(fields) != 5:
            raise CronError("a schedule needs five fields: minute hour day-of-month month "
                            "day-of-week")
        record = cls(
            expression=" ".join(fields),
            minute=_field(fields[0], 0, 59, "minute"),
            hour=_field(fields[1], 0, 23, "hour"),
            dom=_field(fields[2], 1, 31, "day-of-month"),
            month=_field(fields[3], 1, 12, "month", MONTH_NAMES),
            dow=_field(fields[4], 0, 7, "day-of-week", DAY_NAMES),
        )
        # Cron numbers Sunday both 0 and 7; storing 7 would make the set disagree with Python's
        # weekday numbering, so normalise it once here.
        if 7 in record.dow.values:
            record = cls(record.expression, record.minute, record.hour, record.dom,
                         record.month, Field(record.dow.values - {7} | {0}, record.dow.wildcard))
        if validate:
            record.next_after(datetime.now().astimezone())
        return record

    def _day_matches(self, moment: datetime) -> bool:
        dom_ok = moment.day in self.dom.values
        # Python: Monday=0..Sunday=6. Cron: Sunday=0, Monday=1..Saturday=6.
        dow_ok = ((moment.weekday() + 1) % 7) in self.dow.values
        if self.dom.wildcard and self.dow.wildcard:
            return True
        if self.dom.wildcard:
            return dow_ok
        if self.dow.wildcard:
            return dom_ok
        # Standard cron: when both day fields are restricted, either one may match.
        return dom_ok or dow_ok

    def matches(self, moment: datetime) -> bool:
        """True when `moment` (to the minute) is exactly a scheduled occurrence."""
        if moment.tzinfo is None:
            raise CronError("schedule arithmetic needs a timezone-aware datetime")
        local = moment.replace(second=0, microsecond=0)
        return (local.month in self.month.values and self._day_matches(local)
                and local.hour in self.hour.values and local.minute in self.minute.values)

    def next_after(self, moment: datetime, *, inclusive: bool = False) -> datetime:
        """First matching minute strictly after `moment` (or at it when inclusive)."""
        if moment.tzinfo is None:
            raise CronError("schedule arithmetic needs a timezone-aware datetime")
        candidate = moment.replace(second=0, microsecond=0)
        if not inclusive or moment.second or moment.microsecond:
            candidate += timedelta(minutes=1)
        limit = candidate + timedelta(days=SEARCH_DAYS)
        while candidate <= limit:
            if candidate.month not in self.month.values:
                year = candidate.year + (1 if candidate.month == 12 else 0)
                month = 1 if candidate.month == 12 else candidate.month + 1
                candidate = candidate.replace(year=year, month=month, day=1, hour=0, minute=0)
                continue
            if not self._day_matches(candidate):
                candidate = (candidate + timedelta(days=1)).replace(hour=0, minute=0)
                continue
            if candidate.hour not in self.hour.values:
                later = [hour for hour in self.hour.values if hour > candidate.hour]
                if later:
                    candidate = candidate.replace(hour=min(later), minute=0)
                else:
                    candidate = (candidate + timedelta(days=1)).replace(hour=0, minute=0)
                continue
            if candidate.minute not in self.minute.values:
                later = [minute for minute in self.minute.values if minute > candidate.minute]
                if later:
                    candidate = candidate.replace(minute=min(later))
                else:
                    candidate = (candidate + timedelta(hours=1)).replace(minute=0)
                continue
            return candidate
        raise CronError("no occurrence within six years; the expression may be impossible")

    def occurrences(self, start: datetime, end: datetime, *, limit: int = 1000) -> list[datetime]:
        """Matching minutes in (start, end], capped so a runaway expression cannot hang a run."""
        found: list[datetime] = []
        cursor = start
        while len(found) < limit:
            candidate = self.next_after(cursor)
            if candidate > end:
                break
            found.append(candidate)
            cursor = candidate
        return found
