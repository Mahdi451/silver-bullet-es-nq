from __future__ import annotations

import pandas as pd
import pytest

from silver_bullet import (
    AlpacaProxyConfig,
    AlpacaProxyExecutor,
    Direction,
    FairValueGap,
    LiquidityKind,
    LiquidityLevel,
    TradeSignal,
)


def _signal(direction=Direction.LONG, instrument="ES"):
    timestamp = pd.Timestamp("2024-01-08 10:00", tz="America/New_York")
    gap = FairValueGap(direction, 99, 100, timestamp, timestamp, timestamp)
    target = LiquidityLevel("PDH:test", LiquidityKind.PDH, 110, "test", timestamp)
    return TradeSignal(
        instrument,
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


class FakeTradingClient:
    def __init__(self):
        self.orders = []

    def submit_order(self, *, order_data):
        self.orders.append(order_data)
        return {"id": "paper-order-1", "order_data": order_data}


def test_alpaca_proxy_preview_maps_futures_signal_to_proxy_order():
    signal = _signal(Direction.LONG, "ES")
    executor = AlpacaProxyExecutor()

    preview = executor.preview_order(
        signal,
        as_of=signal.signal_time + pd.Timedelta(minutes=1),
        reference_price=500,
    )

    assert preview.proxy_symbol == "SPY"
    assert preview.side == "buy"
    assert preview.quantity == 1
    assert preview.estimated_notional == 500


def test_alpaca_proxy_preview_maps_short_nq_to_qqq_sell():
    signal = _signal(Direction.SHORT, "NQ")
    executor = AlpacaProxyExecutor()

    preview = executor.preview_order(signal, as_of=signal.signal_time)

    assert preview.proxy_symbol == "QQQ"
    assert preview.side == "sell"


def test_alpaca_proxy_rejects_stale_or_oversized_orders():
    signal = _signal()
    executor = AlpacaProxyExecutor(
        AlpacaProxyConfig(max_signal_age_minutes=1, max_notional_per_order=250)
    )

    with pytest.raises(ValueError, match="stale"):
        executor.preview_order(
            signal,
            as_of=signal.signal_time + pd.Timedelta(minutes=2),
        )
    with pytest.raises(ValueError, match="notional"):
        executor.preview_order(signal, as_of=signal.signal_time, reference_price=300)


def test_alpaca_proxy_dry_run_does_not_submit_order():
    signal = _signal()
    client = FakeTradingClient()
    executor = AlpacaProxyExecutor(trading_client=client)

    result = executor.submit_order(signal, as_of=signal.signal_time, dry_run=True)

    assert not result.submitted
    assert result.dry_run
    assert client.orders == []


def test_alpaca_proxy_submission_requires_explicit_enablement():
    signal = _signal()
    client = FakeTradingClient()
    executor = AlpacaProxyExecutor(trading_client=client, order_factory=lambda p: p)

    with pytest.raises(RuntimeError, match="disabled"):
        executor.submit_order(signal, as_of=signal.signal_time, dry_run=False)


def test_alpaca_proxy_submits_with_fake_client_when_enabled():
    signal = _signal()
    client = FakeTradingClient()
    executor = AlpacaProxyExecutor(
        AlpacaProxyConfig(enabled=True),
        trading_client=client,
        order_factory=lambda preview: {"symbol": preview.proxy_symbol},
    )

    result = executor.submit_order(signal, as_of=signal.signal_time, dry_run=False)

    assert result.submitted
    assert result.broker_order["id"] == "paper-order-1"
    assert client.orders == [{"symbol": "SPY"}]
