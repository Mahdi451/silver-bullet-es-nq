from __future__ import annotations

from datetime import date

import pandas as pd
import pytest

from silver_bullet.alpaca_broker import AlpacaBroker, AlpacaBrokerConfig
from silver_bullet.broker import (
    ExecutionStore,
    LiveRiskGate,
    LiveRiskLimits,
    ManagedTradeStatus,
)
from silver_bullet.models import (
    Direction,
    FairValueGap,
    LiquidityKind,
    LiquidityLevel,
    TradeSignal,
)
from silver_bullet.options import (
    OptionContract,
    OptionLeg,
    OptionOrderCandidate,
    OptionRight,
    OptionsProfile,
)


NOW = pd.Timestamp("2024-01-08 10:01", tz="America/New_York")


def _signal():
    formed = NOW - pd.Timedelta(minutes=1)
    gap = FairValueGap(Direction.LONG, 99, 100, formed, formed, formed)
    target = LiquidityLevel("PDH", LiquidityKind.PDH, 110, "test", formed)
    return TradeSignal(
        "ES",
        "ny_am",
        Direction.LONG,
        formed,
        NOW,
        target,
        gap,
        gap,
        formed,
        100,
        95,
        "ESH4",
    )


def _candidate(spread=False):
    long_contract = OptionContract(
        "SPY240108C00470000",
        "SPY",
        OptionRight.CALL,
        470,
        date(2024, 1, 8),
        open_interest=500,
    )
    legs = [OptionLeg(long_contract, "buy", "buy_to_open", reference_price=2.1)]
    if spread:
        short_contract = OptionContract(
            "SPY240108C00472000",
            "SPY",
            OptionRight.CALL,
            472,
            date(2024, 1, 8),
            open_interest=500,
        )
        legs.append(
            OptionLeg(short_contract, "sell", "sell_to_open", reference_price=1.0)
        )
    return OptionOrderCandidate(
        _signal(),
        OptionsProfile.DOL_DEBIT_SPREAD if spread else OptionsProfile.HIGH_DELTA_LONG,
        "SPY",
        tuple(legs),
        1,
        1.1 if spread else 2.1,
        110 if spread else 210,
        90 if spread else None,
        0,
        "test candidate",
    )


class FakeTransport:
    def __init__(self):
        self.calls = []
        self.order_status = "new"
        self.positions = []

    def request(self, method, url, *, headers=None, params=None, body=None):
        self.calls.append((method, url, headers, params, body))
        path = url.split(".test")[-1]
        if path == "/v2/account":
            return {
                "id": "paper-1",
                "status": "ACTIVE",
                "equity": "100000",
                "last_equity": "99500",
                "cash": "50000",
                "buying_power": "50000",
                "options_buying_power": "25000",
                "options_trading_level": 3,
            }
        if path.startswith("/v2/orders") and method == "GET":
            return []
        if path == "/v2/positions" and method == "GET":
            return self.positions
        if path == "/v2/account/activities/FILL":
            return [
                {
                    "id": "fill-1",
                    "order_id": "old-order",
                    "symbol": "SPY240108C00470000",
                    "side": "buy",
                    "qty": "1",
                    "price": "2.05",
                    "transaction_time": "2024-01-08T15:00:30Z",
                }
            ]
        if path == "/v2/orders" and method == "POST":
            return {"id": f"order-{len(self.calls)}", "status": self.order_status}
        if method == "DELETE":
            return []
        raise AssertionError((method, path))


def _broker(enabled=True):
    transport = FakeTransport()
    store = ExecutionStore()
    broker = AlpacaBroker(
        AlpacaBrokerConfig(
            "key", "secret", paper=True, enabled=enabled, base_url="https://broker.test"
        ),
        store=store,
        transport=transport,
        clock=lambda: NOW,
    )
    return broker, transport, store


def test_alpaca_snapshot_normalizes_account_and_fills():
    broker, _, _ = _broker()
    snapshot = broker.snapshot(as_of=NOW)
    assert snapshot.account.options_level == 3
    assert snapshot.account.buying_power == 50_000
    assert snapshot.account.daily_pl == 500
    assert snapshot.fills[0].price == 2.05


def test_alpaca_single_option_preview_is_non_mutating():
    broker, transport, store = _broker(enabled=False)
    preview = broker.preview_option_order(_candidate())
    assert preview.payload["symbol"] == "SPY240108C00470000"
    assert preview.payload["position_intent"] == "buy_to_open"
    assert preview.payload["limit_price"] == "2.10"
    assert not preview.submitted
    assert transport.calls == []
    assert store.trades() == ()


def test_alpaca_submits_multileg_candidate_and_tracks_baseline():
    broker, transport, store = _broker()
    snapshot = broker.snapshot(as_of=NOW)
    gate = LiveRiskGate(store, LiveRiskLimits(maximum_order_risk=500))
    result = broker.submit_option(
        _candidate(spread=True), snapshot=snapshot, risk_gate=gate, dry_run=False
    )

    assert result.submitted
    assert result.payload["order_class"] == "mleg"
    assert [leg["side"] for leg in result.payload["legs"]] == ["buy", "sell"]
    trade = store.get(result.client_order_id)
    assert trade.status is ManagedTradeStatus.PENDING_ENTRY
    assert [leg.expected_quantity for leg in trade.legs] == [1, -1]
    assert any(call[0] == "POST" for call in transport.calls)


def test_alpaca_close_reverses_multileg_position_intents():
    broker, _, store = _broker()
    snapshot = broker.snapshot(as_of=NOW)
    gate = LiveRiskGate(store, LiveRiskLimits(maximum_order_risk=500))
    submitted = broker.submit_option(
        _candidate(spread=True), snapshot=snapshot, risk_gate=gate, dry_run=False
    )
    trade = store.update(
        submitted.client_order_id, status=ManagedTradeStatus.OPEN
    )
    closed = broker.close_trade(trade, "futures_stop")
    assert closed.payload["order_class"] == "mleg"
    assert [leg["side"] for leg in closed.payload["legs"]] == ["sell", "buy"]
    assert [leg["position_intent"] for leg in closed.payload["legs"]] == [
        "sell_to_close",
        "buy_to_close",
    ]


def test_alpaca_live_mode_requires_double_opt_in():
    broker = AlpacaBroker(
        AlpacaBrokerConfig(
            "key",
            "secret",
            paper=False,
            enabled=True,
            allow_live=False,
            base_url="https://broker.test",
        ),
        transport=FakeTransport(),
    )
    with pytest.raises(RuntimeError, match="allow_live"):
        broker.cancel_all_orders()
