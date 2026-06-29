"""Event-driven implementation of the source-aligned Silver Bullet setup."""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass
from datetime import date, time
from enum import Enum
from typing import Iterable, Iterator

import pandas as pd

from .levels import LiquidityLevelTracker
from .models import (
    Bar,
    Direction,
    FairValueGap,
    INSTRUMENTS,
    LiquidityKind,
    LiquidityLevel,
    NEW_YORK,
    TradeSignal,
)


@dataclass(frozen=True, slots=True)
class SessionWindow:
    name: str
    start: time
    end: time

    def contains(self, timestamp: pd.Timestamp) -> bool:
        current = timestamp.tz_convert(NEW_YORK).time().replace(tzinfo=None)
        return self.start <= current < self.end

    def end_timestamp(self, session_date: date) -> pd.Timestamp:
        return pd.Timestamp.combine(session_date, self.end).tz_localize(NEW_YORK)


DEFAULT_SESSIONS = (
    SessionWindow("london", time(3, 0), time(4, 0)),
    SessionWindow("macro", time(9, 30), time(10, 0)),
    SessionWindow("ny_am", time(10, 0), time(11, 0)),
    SessionWindow("ny_pm", time(14, 0), time(15, 0)),
)


@dataclass(frozen=True, slots=True)
class SilverBulletConfig:
    atr_period: int = 14
    displacement_atr_multiple: float = 1.5
    pivot_width: int = 2
    minimum_delivery_points: float = 10.0
    sessions: tuple[SessionWindow, ...] = DEFAULT_SESSIONS

    def __post_init__(self) -> None:
        if self.atr_period < 1:
            raise ValueError("atr_period must be positive")
        if self.displacement_atr_multiple <= 0:
            raise ValueError("displacement_atr_multiple must be positive")
        if self.pivot_width < 1:
            raise ValueError("pivot_width must be positive")
        if self.minimum_delivery_points <= 0:
            raise ValueError("minimum_delivery_points must be positive")
        names = [session.name for session in self.sessions]
        if len(names) != len(set(names)):
            raise ValueError("session names must be unique")
        if any(session.start >= session.end for session in self.sessions):
            raise ValueError("session windows cannot cross midnight")


class StrategyStage(str, Enum):
    SEEK_FIRST_FVG = "seek_first_fvg"
    WAIT_MITIGATION = "wait_mitigation"
    SEEK_SECOND_FVG = "seek_second_fvg"
    COMPLETE = "complete"


@dataclass(frozen=True, slots=True)
class _SessionKey:
    name: str
    session_date: date


@dataclass(slots=True)
class _FeatureBar:
    bar: Bar
    atr_before: float | None
    swing_high_before: float | None
    swing_low_before: float | None


@dataclass(slots=True)
class _Setup:
    session_key: _SessionKey
    direction: Direction
    target_id: str
    first_fvg: FairValueGap
    displacement_time: pd.Timestamp
    origin_swing_price: float
    mitigation_time: pd.Timestamp | None = None


class _WilderATR:
    def __init__(self, period: int) -> None:
        self.period = period
        self.value: float | None = None
        self._seed: list[float] = []
        self._previous_close: float | None = None

    def update(self, bar: Bar) -> None:
        if self._previous_close is None:
            true_range = bar.high - bar.low
        else:
            true_range = max(
                bar.high - bar.low,
                abs(bar.high - self._previous_close),
                abs(bar.low - self._previous_close),
            )
        if self.value is None:
            self._seed.append(true_range)
            if len(self._seed) == self.period:
                self.value = sum(self._seed) / self.period
        else:
            self.value = (
                (self.value * (self.period - 1)) + true_range
            ) / self.period
        self._previous_close = bar.close


class SilverBulletStrategy:
    """Consume completed bars and emit entry intents without simulating fills."""

    def __init__(
        self,
        instrument: str,
        config: SilverBulletConfig | None = None,
        level_tracker: LiquidityLevelTracker | None = None,
    ) -> None:
        symbol = instrument.upper()
        if symbol not in INSTRUMENTS:
            raise ValueError("instrument must be ES or NQ")
        self.instrument = symbol
        self.spec = INSTRUMENTS[symbol]
        self.config = config or SilverBulletConfig()
        self.level_tracker = level_tracker or LiquidityLevelTracker()
        self._atr = _WilderATR(self.config.atr_period)
        self._features: deque[_FeatureBar] = deque(
            maxlen=max(5, self.config.pivot_width * 2 + 1)
        )
        self._latest_swing_high: float | None = None
        self._latest_swing_low: float | None = None
        self._active_key: _SessionKey | None = None
        self._setup: _Setup | None = None
        self._completed: set[_SessionKey] = set()
        self._last_timestamp: pd.Timestamp | None = None

    @property
    def stage(self) -> StrategyStage:
        if self._active_key in self._completed:
            return StrategyStage.COMPLETE
        if self._setup is None:
            return StrategyStage.SEEK_FIRST_FVG
        if self._setup.mitigation_time is None:
            return StrategyStage.WAIT_MITIGATION
        return StrategyStage.SEEK_SECOND_FVG

    def scan(self, bars: Iterable[Bar]) -> Iterator[TradeSignal]:
        for bar in bars:
            signal = self.on_bar(bar)
            if signal is not None:
                yield signal

    def on_bar(self, bar: Bar) -> TradeSignal | None:
        self._validate_bar(bar)
        self.level_tracker.on_bar(bar)
        feature = _FeatureBar(
            bar=bar,
            atr_before=self._atr.value,
            swing_high_before=self._latest_swing_high,
            swing_low_before=self._latest_swing_low,
        )
        self._features.append(feature)
        self._atr.update(bar)
        self._confirm_pivot()
        self._last_timestamp = bar.timestamp

        session, key = self._session_for(bar.timestamp)
        if session is None or key is None:
            self._active_key = None
            self._setup = None
            return None
        if key != self._active_key:
            self._active_key = key
            self._setup = None
        if key in self._completed:
            return None

        if self._setup is not None and self._invalidated(bar):
            self._setup = None

        if self._setup is None:
            self._setup = self._find_first_setup(key)
            return None

        if self._setup.mitigation_time is None:
            if (
                bar.timestamp > self._setup.first_fvg.formed_at
                and self._setup.first_fvg.overlaps(bar)
            ):
                self._setup.mitigation_time = bar.timestamp
            return None

        second_fvg = self._find_second_fvg(self._setup)
        if second_fvg is None:
            return None
        execution_time = bar.timestamp + pd.Timedelta(minutes=1)
        if execution_time >= session.end_timestamp(key.session_date):
            self._setup = None
            return None
        target = self.level_tracker.get(self._setup.target_id)
        if target is None or target.tapped:
            self._setup = None
            return None
        proposed_stop = (
            self._setup.origin_swing_price - self.spec.tick_size
            if self._setup.direction is Direction.LONG
            else self._setup.origin_swing_price + self.spec.tick_size
        )
        signal = TradeSignal(
            instrument=self.instrument,
            contract=bar.contract,
            session=key.name,
            direction=self._setup.direction,
            signal_time=bar.timestamp,
            execution_time=execution_time,
            target=target,
            first_fvg=self._setup.first_fvg,
            second_fvg=second_fvg,
            displacement_time=self._setup.displacement_time,
            origin_swing_price=self._setup.origin_swing_price,
            proposed_stop=proposed_stop,
        )
        self._completed.add(key)
        self._setup = None
        return signal

    def _validate_bar(self, bar: Bar) -> None:
        if bar.instrument.upper() != self.instrument:
            raise ValueError(
                f"expected {self.instrument} bars, received {bar.instrument}"
            )
        if bar.timestamp.tz is None:
            raise ValueError("bar timestamps must be timezone-aware")
        if self._last_timestamp is not None and bar.timestamp <= self._last_timestamp:
            raise ValueError("bars must be strictly increasing")

    def _confirm_pivot(self) -> None:
        width = self.config.pivot_width
        needed = width * 2 + 1
        if len(self._features) < needed:
            return
        window = list(self._features)[-needed:]
        candidate = window[width].bar
        others = window[:width] + window[width + 1 :]
        if all(candidate.high > item.bar.high for item in others):
            self._latest_swing_high = candidate.high
        if all(candidate.low < item.bar.low for item in others):
            self._latest_swing_low = candidate.low

    def _session_for(
        self, timestamp: pd.Timestamp
    ) -> tuple[SessionWindow | None, _SessionKey | None]:
        local = timestamp.tz_convert(NEW_YORK)
        for session in self.config.sessions:
            if session.contains(local):
                return session, _SessionKey(session.name, local.date())
        return None, None

    def _invalidated(self, bar: Bar) -> bool:
        assert self._setup is not None
        target = self.level_tracker.get(self._setup.target_id)
        if target is None or target.tapped:
            return True
        if self._setup.direction is Direction.LONG:
            return bar.low < self._setup.origin_swing_price
        return bar.high > self._setup.origin_swing_price

    def _find_first_setup(self, key: _SessionKey) -> _Setup | None:
        if len(self._features) < 3:
            return None
        first, middle, current = list(self._features)[-3:]
        middle_session, middle_key = self._session_for(middle.bar.timestamp)
        if middle_session is None or middle_key != key:
            return None
        for direction in (Direction.LONG, Direction.SHORT):
            if not self._is_displacement(middle, direction):
                continue
            gap = self._detect_fvg(first.bar, middle.bar, current.bar, direction)
            if gap is None:
                continue
            origin = (
                middle.swing_low_before
                if direction is Direction.LONG
                else middle.swing_high_before
            )
            if origin is None:
                continue
            target = self._select_target(direction, current.bar, origin)
            if target is None:
                continue
            return _Setup(
                session_key=key,
                direction=direction,
                target_id=target.level_id,
                first_fvg=gap,
                displacement_time=middle.bar.timestamp,
                origin_swing_price=origin,
            )
        return None

    def _is_displacement(
        self, feature: _FeatureBar, direction: Direction
    ) -> bool:
        bar = feature.bar
        if feature.atr_before is None:
            return False
        body = abs(bar.close - bar.open)
        if body <= self.config.displacement_atr_multiple * feature.atr_before:
            return False
        if direction is Direction.LONG:
            return (
                bar.close > bar.open
                and feature.swing_high_before is not None
                and feature.swing_low_before is not None
                and bar.close > feature.swing_high_before
            )
        return (
            bar.close < bar.open
            and feature.swing_low_before is not None
            and feature.swing_high_before is not None
            and bar.close < feature.swing_low_before
        )

    def _detect_fvg(
        self, first: Bar, middle: Bar, current: Bar, direction: Direction
    ) -> FairValueGap | None:
        if direction is Direction.LONG:
            lower, upper = first.high, current.low
        else:
            lower, upper = current.high, first.low
        if upper - lower + 1e-12 < self.spec.tick_size:
            return None
        return FairValueGap(
            direction=direction,
            lower=lower,
            upper=upper,
            formed_at=current.timestamp,
            first_candle_at=first.timestamp,
            middle_candle_at=middle.timestamp,
        )

    def _select_target(
        self, direction: Direction, bar: Bar, origin: float
    ) -> LiquidityLevel | None:
        candidates: list[LiquidityLevel] = []
        for level in self.level_tracker.levels(include_tapped=False):
            if direction is Direction.LONG:
                correct_side = level.price > bar.high
                delivery = level.price - origin
            else:
                correct_side = level.price < bar.low
                delivery = origin - level.price
            if correct_side and delivery >= self.config.minimum_delivery_points:
                candidates.append(level)
        if not candidates:
            return None

        weekly = {LiquidityKind.PWH, LiquidityKind.PWL}
        return min(
            candidates,
            key=lambda level: (
                abs(level.price - bar.close),
                0 if level.kind in weekly else 1,
                level.kind.value,
            ),
        )

    def _find_second_fvg(self, setup: _Setup) -> FairValueGap | None:
        assert setup.mitigation_time is not None
        if len(self._features) < 3:
            return None
        first, middle, current = list(self._features)[-3:]
        if first.bar.timestamp < setup.mitigation_time:
            return None
        _, middle_key = self._session_for(middle.bar.timestamp)
        _, current_key = self._session_for(current.bar.timestamp)
        if middle_key != setup.session_key or current_key != setup.session_key:
            return None
        return self._detect_fvg(
            first.bar, middle.bar, current.bar, setup.direction
        )
