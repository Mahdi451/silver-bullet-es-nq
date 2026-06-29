from __future__ import annotations

from dataclasses import replace

import pandas as pd
import pytest

from silver_bullet import (
    Bar,
    Direction,
    LiquidityKind,
    LiquidityLevel,
    SilverBulletConfig,
    SilverBulletStrategy,
    StrategyStage,
)


class FixedLevels:
    def __init__(self, *levels):
        self._levels = {level.level_id: level for level in levels}

    def on_bar(self, bar):
        for level_id, level in tuple(self._levels.items()):
            if bar.low <= level.price <= bar.high:
                self._levels[level_id] = replace(level, tapped=True)
        return self.levels()

    def levels(self, *, include_tapped=True):
        return tuple(
            level
            for level in self._levels.values()
            if include_tapped or not level.tapped
        )

    def get(self, level_id):
        return self._levels.get(level_id)


def _level(kind, price):
    return LiquidityLevel(
        level_id=f"{kind.value}:fixture",
        kind=kind,
        price=price,
        source_period="fixture",
        available_from=pd.Timestamp("2024-01-01", tz="America/New_York"),
    )


def _bar(timestamp, open_, high, low, close):
    return Bar(
        timestamp=pd.Timestamp(timestamp, tz="America/New_York"),
        instrument="ES",
        open=open_,
        high=high,
        low=low,
        close=close,
        volume=1,
        contract="ESH4",
    )


def _long_bars(session_start="2024-01-08 10:00"):
    start = pd.Timestamp(session_start)
    values = [
        (-5, 100, 101, 99, 100),
        (-4, 100, 103, 99.5, 102),
        (-3, 102, 102.5, 100, 101),
        (-2, 101, 102, 98, 99),
        (-1, 99, 101, 99, 100),
        (0, 100, 101, 99.5, 100.5),
        (1, 100.5, 106.5, 100.25, 106),
        (2, 106, 107, 102, 106.5),
        (3, 102, 103, 101.5, 102.5),
        (4, 102.5, 108, 101.75, 107),
        (5, 107, 109, 104, 108),
    ]
    return [
        _bar(start + pd.Timedelta(minutes=offset), open_, high, low, close)
        for offset, open_, high, low, close in values
    ]


def _short_bars(session_start="2024-01-08 10:00"):
    start = pd.Timestamp(session_start)
    values = [
        (-5, 100, 101, 99, 100),
        (-4, 100, 100.5, 97, 98),
        (-3, 98, 101, 98, 100),
        (-2, 100, 102, 99, 101),
        (-1, 101, 101, 99.5, 100),
        (0, 100, 100.5, 99, 99.5),
        (1, 99.5, 99.75, 93.5, 94),
        (2, 94, 98, 92, 93.5),
        (3, 97, 98.5, 96, 97),
        (4, 97, 97.5, 91, 92),
        (5, 92, 95, 90, 91),
    ]
    return [
        _bar(start + pd.Timedelta(minutes=offset), open_, high, low, close)
        for offset, open_, high, low, close in values
    ]


def _strategy(levels, **overrides):
    config = SilverBulletConfig(
        atr_period=3,
        displacement_atr_multiple=1.5,
        pivot_width=1,
        minimum_delivery_points=10,
        **overrides,
    )
    return SilverBulletStrategy("ES", config=config, level_tracker=FixedLevels(*levels))


@pytest.mark.parametrize(
    ("session_start", "session_name"),
    [
        ("2024-01-08 03:00", "london"),
        ("2024-01-08 09:30", "macro"),
        ("2024-01-08 10:00", "ny_am"),
        ("2024-01-08 14:00", "ny_pm"),
    ],
)
def test_bullish_source_sequence_emits_one_next_bar_signal(session_start, session_name):
    strategy = _strategy([_level(LiquidityKind.PDH, 120)])
    signals = list(strategy.scan(_long_bars(session_start)))

    assert len(signals) == 1
    signal = signals[0]
    assert signal.session == session_name
    assert signal.direction is Direction.LONG
    assert signal.target.price == 120
    assert signal.first_fvg.lower == 101
    assert signal.first_fvg.upper == 102
    assert signal.second_fvg.lower == 103
    assert signal.second_fvg.upper == 104
    assert signal.execution_time == signal.signal_time + pd.Timedelta(minutes=1)
    assert signal.origin_swing_price == 98
    assert signal.proposed_stop == 97.75
    assert strategy.stage is StrategyStage.COMPLETE


def test_bearish_source_sequence_emits_short_signal():
    strategy = _strategy([_level(LiquidityKind.PDL, 80)])
    signals = list(strategy.scan(_short_bars()))
    assert len(signals) == 1
    signal = signals[0]
    assert signal.direction is Direction.SHORT
    assert signal.first_fvg.lower == 98
    assert signal.first_fvg.upper == 99
    assert signal.second_fvg.lower == 95
    assert signal.second_fvg.upper == 96
    assert signal.proposed_stop == 102.25


def test_weekly_level_wins_an_exact_distance_tie():
    strategy = _strategy(
        [
            _level(LiquidityKind.PDH, 120),
            _level(LiquidityKind.PWH, 120),
        ]
    )
    signal = list(strategy.scan(_long_bars()))[0]
    assert signal.target.kind is LiquidityKind.PWH


def test_setup_is_invalidated_when_origin_swing_is_crossed():
    bars = _long_bars()[:8]
    bars.append(_bar("2024-01-08 10:03", 99, 100, 97.75, 98.5))
    strategy = _strategy([_level(LiquidityKind.PDH, 120)])
    assert list(strategy.scan(bars)) == []
    assert strategy.stage is StrategyStage.SEEK_FIRST_FVG


def test_rejects_second_fvg_when_next_open_is_outside_session():
    bars = _long_bars("2024-01-08 10:54")
    strategy = _strategy([_level(LiquidityKind.PDH, 120)])
    assert list(strategy.scan(bars)) == []
    assert strategy.stage is StrategyStage.SEEK_FIRST_FVG


def test_minimum_delivery_filter_rejects_too_close_target():
    strategy = _strategy([_level(LiquidityKind.PDH, 107.5)])
    assert list(strategy.scan(_long_bars())) == []


def test_missing_minute_breaks_pattern_sequence():
    bars = _long_bars()
    bars[6] = _bar("2024-01-08 10:02", 100.5, 106.5, 100.25, 106)
    for index in range(7, len(bars)):
        bars[index] = replace(
            bars[index], timestamp=bars[index].timestamp + pd.Timedelta(minutes=1)
        )
    strategy = _strategy([_level(LiquidityKind.PDH, 120)])
    assert list(strategy.scan(bars)) == []
