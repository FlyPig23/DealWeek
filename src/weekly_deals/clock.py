"""Injected clock.

The plan forbids scattered ``datetime.now()`` calls: every temporal decision has
to be reproducible in tests with a frozen instant. Everything that needs "now"
takes a :class:`Clock`.
"""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from typing import Protocol
from zoneinfo import ZoneInfo


class Clock(Protocol):
    """Minimal time source."""

    @property
    def tz(self) -> ZoneInfo:  # pragma: no cover - protocol
        ...

    def now(self) -> datetime:  # pragma: no cover - protocol
        """Timezone-aware instant in the display timezone."""
        ...


class SystemClock:
    """Real wall-clock time rendered into a fixed display timezone."""

    def __init__(self, tz: str = "America/Chicago") -> None:
        self._tz = ZoneInfo(tz)

    @property
    def tz(self) -> ZoneInfo:
        return self._tz

    def now(self) -> datetime:
        return datetime.now(UTC).astimezone(self._tz)


class FrozenClock:
    """Deterministic clock for tests and reproducible reports."""

    def __init__(self, instant: datetime, tz: str = "America/Chicago") -> None:
        self._tz = ZoneInfo(tz)
        if instant.tzinfo is None:
            instant = instant.replace(tzinfo=self._tz)
        self._instant = instant.astimezone(self._tz)

    @property
    def tz(self) -> ZoneInfo:
        return self._tz

    def now(self) -> datetime:
        return self._instant

    def advance(self, **kwargs: float) -> None:
        self._instant = self._instant + timedelta(**kwargs)


def today(clock: Clock) -> date:
    return clock.now().date()


def week_bounds(clock: Clock, offset_weeks: int = 0) -> tuple[date, date]:
    """Monday..Sunday of the current week in the clock's timezone.

    ``offset_weeks=1`` gives next calendar week. The plan is explicit that "next
    week" means the next *calendar* week, never a rolling 14-day window.
    """
    current = today(clock)
    monday = current - timedelta(days=current.weekday()) + timedelta(weeks=offset_weeks)
    return monday, monday + timedelta(days=6)


def month_bounds(clock: Clock) -> tuple[date, date]:
    """First..last day of the current calendar month."""
    current = today(clock)
    first = current.replace(day=1)
    if first.month == 12:
        next_first = first.replace(year=first.year + 1, month=1)
    else:
        next_first = first.replace(month=first.month + 1)
    return first, next_first - timedelta(days=1)


def rolling_bounds(clock: Clock, days: int) -> tuple[date, date]:
    """Rolling horizon starting today. Kept distinct from week/month views."""
    current = today(clock)
    return current, current + timedelta(days=days - 1)
