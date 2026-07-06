"""Provider-neutral context gating for external market signals."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Mapping

import pandas as pd

from .models import Direction, NEW_YORK, TradeSignal


@dataclass(frozen=True, slots=True)
class ExternalContextSnapshot:
    """Normalized external context available at signal decision time."""

    instrument: str
    proxy_symbol: str
    observed_at: pd.Timestamp
    bullish_score: float = 0.0
    bearish_score: float = 0.0
    source: str = "external"
    metadata: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if self.observed_at.tz is None:
            raise ValueError("observed_at must be timezone-aware")
        if self.bullish_score < 0 or self.bearish_score < 0:
            raise ValueError("context scores cannot be negative")
        object.__setattr__(self, "instrument", self.instrument.upper())
        object.__setattr__(self, "proxy_symbol", self.proxy_symbol.upper())

    def aligned_score(self, direction: Direction) -> float:
        return (
            self.bullish_score
            if direction is Direction.LONG
            else self.bearish_score
        )

    def opposing_score(self, direction: Direction) -> float:
        return (
            self.bearish_score
            if direction is Direction.LONG
            else self.bullish_score
        )


@dataclass(frozen=True, slots=True)
class ContextGateConfig:
    """Rules for accepting or rejecting a strategy signal using external context."""

    proxy_symbols: Mapping[str, str] = field(
        default_factory=lambda: {"ES": "SPY", "NQ": "QQQ"}
    )
    allow_missing_context: bool = False
    max_context_age_minutes: int = 15
    min_aligned_score: float = 0.55
    min_score_edge: float = 0.05

    def __post_init__(self) -> None:
        if self.max_context_age_minutes < 0:
            raise ValueError("max_context_age_minutes cannot be negative")
        if self.min_aligned_score < 0 or self.min_score_edge < 0:
            raise ValueError("score thresholds cannot be negative")


@dataclass(frozen=True, slots=True)
class ContextGateDecision:
    signal: TradeSignal
    accepted: bool
    reason: str
    context: ExternalContextSnapshot | None = None
    context_age_minutes: float | None = None


class SignalContextGate:
    """Apply external context as a post-strategy risk gate."""

    def __init__(self, config: ContextGateConfig | None = None) -> None:
        self.config = config or ContextGateConfig()

    def evaluate(
        self,
        signal: TradeSignal,
        context: ExternalContextSnapshot | None,
        *,
        as_of: pd.Timestamp | None = None,
    ) -> ContextGateDecision:
        reference = as_of or signal.signal_time
        if reference.tz is None:
            raise ValueError("as_of must be timezone-aware")

        if context is None:
            return ContextGateDecision(
                signal,
                self.config.allow_missing_context,
                "missing_context_allowed"
                if self.config.allow_missing_context
                else "missing_context",
            )

        expected_proxy = self.config.proxy_symbols.get(signal.instrument.upper())
        if expected_proxy and context.proxy_symbol != expected_proxy.upper():
            return ContextGateDecision(signal, False, "wrong_proxy_symbol", context)
        if context.instrument != signal.instrument.upper():
            return ContextGateDecision(signal, False, "wrong_instrument", context)

        observed = context.observed_at.tz_convert(NEW_YORK)
        reference = reference.tz_convert(NEW_YORK)
        age = (reference - observed) / pd.Timedelta(minutes=1)
        if age < 0:
            return ContextGateDecision(signal, False, "future_context", context, age)
        if age > self.config.max_context_age_minutes:
            return ContextGateDecision(signal, False, "stale_context", context, age)

        aligned = context.aligned_score(signal.direction)
        opposing = context.opposing_score(signal.direction)
        if aligned < self.config.min_aligned_score:
            return ContextGateDecision(
                signal, False, "insufficient_aligned_score", context, age
            )
        if aligned + self.config.min_score_edge < opposing:
            return ContextGateDecision(signal, False, "conflicting_context", context, age)
        return ContextGateDecision(signal, True, "accepted", context, age)
