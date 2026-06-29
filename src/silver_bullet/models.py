"""Canonical market events and strategy result types."""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Protocol, runtime_checkable

import pandas as pd


NEW_YORK = "America/New_York"


@dataclass(frozen=True, slots=True)
class InstrumentSpec:
    """Static futures contract information needed by the signal layer."""

    symbol: str
    tick_size: float
    point_value: float


INSTRUMENTS = {
    "ES": InstrumentSpec("ES", tick_size=0.25, point_value=50.0),
    "NQ": InstrumentSpec("NQ", tick_size=0.25, point_value=20.0),
}


@dataclass(frozen=True, slots=True)
class Bar:
    """One completed one-minute OHLCV event in New York local time."""

    timestamp: pd.Timestamp
    instrument: str
    open: float
    high: float
    low: float
    close: float
    volume: float
    contract: str | None = None


@runtime_checkable
class BarSource(Protocol):
    """Input boundary shared by CSV history and future live feeds."""

    def __iter__(self):  # type: ignore[no-untyped-def]
        """Yield canonical bars in ascending timestamp order."""
        ...


class Direction(str, Enum):
    LONG = "long"
    SHORT = "short"


class LiquidityKind(str, Enum):
    PDH = "PDH"
    PDL = "PDL"
    PWH = "PWH"
    PWL = "PWL"


@dataclass(frozen=True, slots=True)
class LiquidityLevel:
    """A completed prior-period level and its current tap status."""

    level_id: str
    kind: LiquidityKind
    price: float
    source_period: str
    available_from: pd.Timestamp
    tapped: bool = False


@dataclass(frozen=True, slots=True)
class FairValueGap:
    """A strict three-candle wick imbalance."""

    direction: Direction
    lower: float
    upper: float
    formed_at: pd.Timestamp
    first_candle_at: pd.Timestamp
    middle_candle_at: pd.Timestamp

    def overlaps(self, bar: Bar) -> bool:
        return bar.high >= self.lower and bar.low <= self.upper


@dataclass(frozen=True, slots=True)
class TradeSignal:
    """An entry intent; the execution layer will later perform the fill."""

    instrument: str
    session: str
    direction: Direction
    signal_time: pd.Timestamp
    execution_time: pd.Timestamp
    target: LiquidityLevel
    first_fvg: FairValueGap
    second_fvg: FairValueGap
    displacement_time: pd.Timestamp
    origin_swing_price: float
    proposed_stop: float
    contract: str | None = None
