from __future__ import annotations

import pandas as pd

from silver_bullet import Bar, LiquidityKind, LiquidityLevelTracker, trading_day


def _bar(timestamp, high, low, *, close=None):
    close = (high + low) / 2 if close is None else close
    return Bar(
        timestamp=pd.Timestamp(timestamp, tz="America/New_York"),
        instrument="ES",
        open=close,
        high=high,
        low=low,
        close=close,
        volume=1,
    )


def test_trading_day_rolls_at_six_pm():
    assert (
        trading_day(pd.Timestamp("2024-01-08 16:59", tz="America/New_York")).isoformat()
        == "2024-01-08"
    )
    assert (
        trading_day(pd.Timestamp("2024-01-08 18:00", tz="America/New_York")).isoformat()
        == "2024-01-09"
    )


def test_previous_day_levels_appear_only_after_day_finishes_and_track_taps():
    tracker = LiquidityLevelTracker(require_complete_periods=False)
    assert tracker.on_bar(_bar("2024-01-07 18:00", 101, 99)) == ()
    assert tracker.on_bar(_bar("2024-01-08 16:59", 105, 95)) == ()

    levels = tracker.on_bar(_bar("2024-01-08 18:00", 100, 98))
    by_kind = {level.kind: level for level in levels}
    assert by_kind[LiquidityKind.PDH].price == 105
    assert by_kind[LiquidityKind.PDL].price == 95
    assert not by_kind[LiquidityKind.PDH].tapped

    levels = tracker.on_bar(_bar("2024-01-08 18:01", 105, 99))
    by_kind = {level.kind: level for level in levels}
    assert by_kind[LiquidityKind.PDH].tapped
    assert not by_kind[LiquidityKind.PDL].tapped


def test_previous_week_levels_finalize_at_next_trading_week():
    tracker = LiquidityLevelTracker(require_complete_periods=False)
    tracker.on_bar(_bar("2024-01-01 18:00", 101, 99))
    tracker.on_bar(_bar("2024-01-05 16:59", 110, 90))
    levels = tracker.on_bar(_bar("2024-01-07 18:00", 100, 98))
    by_kind = {level.kind: level for level in levels}
    assert by_kind[LiquidityKind.PWH].price == 110
    assert by_kind[LiquidityKind.PWL].price == 90
    assert by_kind[LiquidityKind.PWH].source_period == "2024-01-01"


def test_incomplete_day_does_not_publish_levels():
    tracker = LiquidityLevelTracker()
    tracker.on_bar(_bar("2024-01-08 10:00", 101, 99))
    levels = tracker.on_bar(_bar("2024-01-08 18:00", 100, 98))
    assert all(
        level.kind not in {LiquidityKind.PDH, LiquidityKind.PDL} for level in levels
    )
