from __future__ import annotations

import pandas as pd
import pytest

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
from silver_bullet.tradovate import TradovateBroker, TradovateConfig


NOW = pd.Timestamp("2024-01-08 10:01", tz="America/New_York")


def _signal(contract="ESH4"):
    formed = NOW - pd.Timedelta(minutes=1)
    gap = FairValueGap(Direction.LONG, 4799, 4800, formed, formed, formed)
    target = LiquidityLevel("PDH", LiquidityKind.PDH, 4810, "test", formed)
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
        4798,
        4795,
        contract,
    )


class FakeTransport:
    def __init__(self):
        self.calls = []
        self.positions = []
        self.orders = []
        self.token_count = 0

    def request(self, method, url, *, headers=None, params=None, body=None):
        self.calls.append((method, url, headers, params, body))
        path = url.split("/v1")[-1]
        if path == "/auth/accesstokenrequest":
            self.token_count += 1
            return {
                "accessToken": f"token-{self.token_count}",
                "expirationTime": "2024-01-08T16:20:00Z",
            }
        if path == "/auth/renewaccesstoken":
            return {
                "accessToken": "renewed-token",
                "expirationTime": "2024-01-08T18:00:00Z",
            }
        if path == "/account/item":
            return {"id": 123, "name": "DEMO123", "active": True}
        if path == "/cashBalance/getcashbalancesnapshot":
            return {
                "errorText": "",
                "totalCashValue": 100_000,
                "netLiq": 101_000,
                "initialMargin": 1_000,
                "totalPnL": 1_000,
            }
        if path == "/order/deps":
            return self.orders
        if path == "/position/deps":
            return self.positions
        if path == "/fill/list":
            return [
                {
                    "id": 7,
                    "orderId": 8,
                    "contractId": 42,
                    "timestamp": "2024-01-08T15:00:30Z",
                    "action": "Buy",
                    "qty": 1,
                    "price": 4800,
                }
            ]
        if path == "/contract/find":
            return {"id": 42, "name": "ESH4"}
        if path == "/order/placeoso":
            return {"orderId": 100, "oso1Id": 101, "oso2Id": 102}
        if path == "/order/liquidateposition":
            return {"orderId": 200}
        if path == "/order/cancelorder":
            return {"commandId": 300, "failureReason": None}
        raise AssertionError((method, path))


def _broker(*, enabled=True, demo=True, token="token"):
    transport = FakeTransport()
    store = ExecutionStore()
    config = TradovateConfig(
        123,
        "DEMO123",
        {"ES": "ESH4"},
        contract_ids={"ESH4": 42},
        demo=demo,
        enabled=enabled,
        allow_live=False,
        access_token=token,
        base_url="https://tradovate.test/v1",
    )
    broker = TradovateBroker(
        config, store=store, transport=transport, clock=lambda: NOW
    )
    return broker, transport, store


def test_tradovate_preview_uses_automated_native_oso_brackets():
    broker, transport, _ = _broker(enabled=False)
    preview = broker.preview_order(_signal(), as_of=NOW, reference_price=4800)
    assert preview.estimated_risk == 250
    assert preview.payload["isAutomated"] is True
    assert preview.payload["bracket1"]["orderType"] == "Limit"
    assert preview.payload["bracket1"]["price"] == 4810
    assert preview.payload["bracket2"]["orderType"] == "Stop"
    assert preview.payload["bracket2"]["stopPrice"] == 4795
    assert transport.calls == []


def test_tradovate_snapshot_normalizes_cash_positions_and_fills():
    broker, transport, _ = _broker()
    transport.positions = [
        {"id": 5, "contractId": 42, "netPos": 1, "netPrice": 4800}
    ]
    snapshot = broker.snapshot(as_of=NOW)
    assert snapshot.account.equity == 101_000
    assert snapshot.account.buying_power == 100_000
    assert snapshot.account.daily_pl == 1_000
    assert snapshot.positions[0].symbol == "ESH4"
    assert snapshot.fills[0].order_id == "8"


def test_tradovate_submit_places_oso_and_tracks_futures_position():
    broker, transport, store = _broker()
    snapshot = broker.snapshot(as_of=NOW)
    gate = LiveRiskGate(store, LiveRiskLimits(maximum_order_risk=500))
    result = broker.submit_order(
        _signal(),
        as_of=NOW,
        reference_price=4800,
        snapshot=snapshot,
        risk_gate=gate,
        dry_run=False,
    )
    assert result.order_id == "100"
    trade = store.get(result.client_order_id)
    assert trade.legs[0].contract_id == 42
    assert trade.legs[0].expected_quantity == 1
    assert any("/order/placeoso" in call[1] for call in transport.calls)


def test_tradovate_liquidates_open_trade_by_contract_id():
    broker, _, store = _broker()
    snapshot = broker.snapshot(as_of=NOW)
    gate = LiveRiskGate(store, LiveRiskLimits(maximum_order_risk=500))
    result = broker.submit_order(
        _signal(),
        as_of=NOW,
        reference_price=4800,
        snapshot=snapshot,
        risk_gate=gate,
        dry_run=False,
    )
    trade = store.update(result.client_order_id, status=ManagedTradeStatus.OPEN)
    closed = broker.close_trade(trade, "operator")
    assert closed.order_id == "200"
    assert closed.payload["contractId"] == 42


def test_tradovate_rejects_contract_mismatch_and_unarmed_live_route():
    broker, _, _ = _broker()
    with pytest.raises(ValueError, match="does not match"):
        broker.preview_order(_signal("ESM4"), as_of=NOW, reference_price=4800)
    live, _, _ = _broker(demo=False)
    with pytest.raises(RuntimeError, match="allow_live"):
        live.cancel_all_orders()


def test_tradovate_authenticates_when_token_is_not_supplied():
    transport = FakeTransport()
    config = TradovateConfig(
        123,
        "DEMO123",
        {"ES": "ESH4"},
        username="user",
        password="password",
        app_id="Silver Bullet",
        client_id="1",
        api_secret="secret",
        base_url="https://tradovate.test/v1",
    )
    broker = TradovateBroker(config, transport=transport, clock=lambda: NOW)
    broker.snapshot(as_of=NOW)
    assert transport.token_count == 1
    authenticated_calls = [
        call for call in transport.calls if "/auth/" not in call[1]
    ]
    assert all(call[2]["Authorization"] == "Bearer token-1" for call in authenticated_calls)
