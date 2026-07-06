from __future__ import annotations

import pandas as pd

from silver_bullet import (
    ContextGateConfig,
    Direction,
    ExternalContextSnapshot,
    FairValueGap,
    LiquidityKind,
    LiquidityLevel,
    SignalContextGate,
    TradeSignal,
)


def _signal(direction=Direction.LONG):
    timestamp = pd.Timestamp("2024-01-08 10:00", tz="America/New_York")
    gap = FairValueGap(direction, 99, 100, timestamp, timestamp, timestamp)
    target = LiquidityLevel("PDH:test", LiquidityKind.PDH, 110, "test", timestamp)
    return TradeSignal(
        "ES",
        "ny_am",
        direction,
        timestamp,
        timestamp + pd.Timedelta(minutes=1),
        target,
        gap,
        gap,
        timestamp,
        95,
        94.75,
        "ESH4",
    )


def test_context_gate_accepts_aligned_fresh_proxy_context():
    signal = _signal(Direction.LONG)
    context = ExternalContextSnapshot(
        "ES",
        "SPY",
        signal.signal_time - pd.Timedelta(minutes=2),
        bullish_score=0.7,
        bearish_score=0.2,
        source="unusual_whales",
    )

    decision = SignalContextGate().evaluate(signal, context)

    assert decision.accepted
    assert decision.reason == "accepted"
    assert decision.context_age_minutes == 2


def test_context_gate_rejects_missing_context_by_default():
    decision = SignalContextGate().evaluate(_signal(), None)

    assert not decision.accepted
    assert decision.reason == "missing_context"


def test_context_gate_can_allow_missing_context_for_research_runs():
    gate = SignalContextGate(ContextGateConfig(allow_missing_context=True))

    decision = gate.evaluate(_signal(), None)

    assert decision.accepted
    assert decision.reason == "missing_context_allowed"


def test_context_gate_rejects_stale_or_future_context():
    signal = _signal()
    gate = SignalContextGate(ContextGateConfig(max_context_age_minutes=5))

    stale = ExternalContextSnapshot(
        "ES",
        "SPY",
        signal.signal_time - pd.Timedelta(minutes=6),
        bullish_score=0.8,
    )
    future = ExternalContextSnapshot(
        "ES",
        "SPY",
        signal.signal_time + pd.Timedelta(minutes=1),
        bullish_score=0.8,
    )

    assert gate.evaluate(signal, stale).reason == "stale_context"
    assert gate.evaluate(signal, future).reason == "future_context"


def test_context_gate_rejects_conflicting_or_wrong_proxy_context():
    signal = _signal(Direction.LONG)
    conflicting = ExternalContextSnapshot(
        "ES",
        "SPY",
        signal.signal_time,
        bullish_score=0.56,
        bearish_score=0.75,
    )
    wrong_proxy = ExternalContextSnapshot(
        "ES",
        "QQQ",
        signal.signal_time,
        bullish_score=0.8,
    )

    gate = SignalContextGate()

    assert gate.evaluate(signal, conflicting).reason == "conflicting_context"
    assert gate.evaluate(signal, wrong_proxy).reason == "wrong_proxy_symbol"
