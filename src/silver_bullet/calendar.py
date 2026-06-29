"""Exchange-session helpers used by ingestion, levels, and execution."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, time, timedelta

import pandas as pd

from .models import NEW_YORK


@dataclass(frozen=True, slots=True)
class CMECalendar:
    """CME Globex weekly schedule with configurable holiday exceptions."""

    closed_dates: frozenset[date] = field(default_factory=frozenset)
    early_closes: dict[date, time] = field(default_factory=dict)

    def intervals(self, day: date) -> tuple[tuple[time, time], ...]:
        if day in self.closed_dates:
            return ()
        weekday = day.weekday()
        if weekday == 6:
            return ((time(18), time.max),)
        close = self.early_closes.get(day, time(17))
        if weekday <= 3:
            return ((time.min, close), (time(18), time.max))
        if weekday == 4:
            return ((time.min, close),)
        return ()

    def expected_minutes(
        self, start: pd.Timestamp, end: pd.Timestamp
    ) -> tuple[pd.Timestamp | None, pd.Timestamp | None, int]:
        if start > end:
            return None, None, 0
        current_day, final_day = start.date(), end.date()
        first = last = None
        count = 0
        while current_day <= final_day:
            for open_time, close_time in self.intervals(current_day):
                opened = pd.Timestamp.combine(current_day, open_time).tz_localize(
                    NEW_YORK
                )
                closed = (
                    pd.Timestamp.combine(current_day + timedelta(days=1), time.min)
                    if close_time == time.max
                    else pd.Timestamp.combine(current_day, close_time)
                ).tz_localize(NEW_YORK)
                left = max(start, opened)
                right = min(end + pd.Timedelta(minutes=1), closed)
                if left < right:
                    minutes = int((right - left) / pd.Timedelta(minutes=1))
                    count += minutes
                    first = left if first is None else min(first, left)
                    interval_last = right - pd.Timedelta(minutes=1)
                    last = interval_last if last is None else max(last, interval_last)
            current_day += timedelta(days=1)
        return first, last, count

    def has_unexpected_gap(self, previous: pd.Timestamp, current: pd.Timestamp) -> bool:
        if current - previous <= pd.Timedelta(minutes=1):
            return False
        return (
            self.expected_minutes(
                previous + pd.Timedelta(minutes=1),
                current - pd.Timedelta(minutes=1),
            )[2]
            > 0
        )


DEFAULT_CME_CALENDAR = CMECalendar()
