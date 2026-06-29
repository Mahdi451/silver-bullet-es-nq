"""Causal prior-day and prior-week liquidity-level tracking."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import timedelta

import pandas as pd

from .models import Bar, LiquidityKind, LiquidityLevel, NEW_YORK


def trading_day(timestamp: pd.Timestamp):  # type: ignore[no-untyped-def]
    """Return the CME trading-date label for a New York timestamp."""

    local = timestamp.tz_convert(NEW_YORK)
    label = local.date()
    if local.hour >= 18:
        label += timedelta(days=1)
    return label


def trading_week(timestamp: pd.Timestamp):  # type: ignore[no-untyped-def]
    """Return the Monday label for the timestamp's CME trading week."""

    label = trading_day(timestamp)
    return label - timedelta(days=label.weekday())


@dataclass(slots=True)
class _MutableLevel:
    level_id: str
    kind: LiquidityKind
    price: float
    source_period: str
    available_from: pd.Timestamp
    tapped: bool = False

    def snapshot(self) -> LiquidityLevel:
        return LiquidityLevel(
            level_id=self.level_id,
            kind=self.kind,
            price=self.price,
            source_period=self.source_period,
            available_from=self.available_from,
            tapped=self.tapped,
        )


class LiquidityLevelTracker:
    """Calculate completed PDH/PDL/PWH/PWL levels one bar at a time."""

    def __init__(self) -> None:
        self._last_timestamp: pd.Timestamp | None = None
        self._day_label = None
        self._day_high: float | None = None
        self._day_low: float | None = None
        self._week_label = None
        self._week_high: float | None = None
        self._week_low: float | None = None
        self._daily_levels: list[_MutableLevel] = []
        self._weekly_levels: list[_MutableLevel] = []

    def on_bar(self, bar: Bar) -> tuple[LiquidityLevel, ...]:
        self._validate_order(bar)
        day = trading_day(bar.timestamp)
        week = trading_week(bar.timestamp)

        if self._day_label is None:
            self._start_periods(day, week, bar)
        else:
            if week != self._week_label:
                self._finalize_week(bar.timestamp)
                self._week_label = week
                self._week_high = None
                self._week_low = None
            if day != self._day_label:
                self._finalize_day(bar.timestamp)
                self._day_label = day
                self._day_high = None
                self._day_low = None
            self._accumulate(bar)

        self._mark_tapped(bar)
        self._last_timestamp = bar.timestamp
        return self.levels()

    def levels(self, *, include_tapped: bool = True) -> tuple[LiquidityLevel, ...]:
        levels = self._daily_levels + self._weekly_levels
        return tuple(
            level.snapshot()
            for level in levels
            if include_tapped or not level.tapped
        )

    def get(self, level_id: str) -> LiquidityLevel | None:
        for level in self._daily_levels + self._weekly_levels:
            if level.level_id == level_id:
                return level.snapshot()
        return None

    def _validate_order(self, bar: Bar) -> None:
        if bar.timestamp.tz is None:
            raise ValueError("bar timestamps must be timezone-aware")
        if self._last_timestamp is not None and bar.timestamp <= self._last_timestamp:
            raise ValueError("bars must be strictly increasing")

    def _start_periods(self, day, week, bar: Bar) -> None:  # type: ignore[no-untyped-def]
        self._day_label = day
        self._week_label = week
        self._day_high = bar.high
        self._day_low = bar.low
        self._week_high = bar.high
        self._week_low = bar.low

    def _accumulate(self, bar: Bar) -> None:
        self._day_high = bar.high if self._day_high is None else max(self._day_high, bar.high)
        self._day_low = bar.low if self._day_low is None else min(self._day_low, bar.low)
        self._week_high = (
            bar.high if self._week_high is None else max(self._week_high, bar.high)
        )
        self._week_low = bar.low if self._week_low is None else min(self._week_low, bar.low)

    def _finalize_day(self, available_from: pd.Timestamp) -> None:
        assert self._day_label is not None
        assert self._day_high is not None and self._day_low is not None
        period = self._day_label.isoformat()
        self._daily_levels = [
            _MutableLevel(
                level_id=f"PDH:{period}",
                kind=LiquidityKind.PDH,
                price=self._day_high,
                source_period=period,
                available_from=available_from,
            ),
            _MutableLevel(
                level_id=f"PDL:{period}",
                kind=LiquidityKind.PDL,
                price=self._day_low,
                source_period=period,
                available_from=available_from,
            ),
        ]

    def _finalize_week(self, available_from: pd.Timestamp) -> None:
        assert self._week_label is not None
        assert self._week_high is not None and self._week_low is not None
        period = self._week_label.isoformat()
        self._weekly_levels = [
            _MutableLevel(
                level_id=f"PWH:{period}",
                kind=LiquidityKind.PWH,
                price=self._week_high,
                source_period=period,
                available_from=available_from,
            ),
            _MutableLevel(
                level_id=f"PWL:{period}",
                kind=LiquidityKind.PWL,
                price=self._week_low,
                source_period=period,
                available_from=available_from,
            ),
        ]

    def _mark_tapped(self, bar: Bar) -> None:
        for level in self._daily_levels + self._weekly_levels:
            if bar.low <= level.price <= bar.high:
                level.tapped = True
