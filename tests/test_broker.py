from __future__ import annotations

from pathlib import Path

import pandas as pd
import pytest

from silver_bullet.broker import (
    AssetClass,
    BrokerAccount,
    BrokerOrder,
    BrokerOrderStatus,
    BrokerPosition,
    BrokerSnapshot,
    ExecutionReconciler,
    ExecutionStore,
    LiveRiskGate,
    LiveRiskLimits,
    ManagedTradeStatus,
    OrderSubmission,
    SyntheticExitManager,
    TrackedLeg,
    TrackedTrade,
)
from silver_bullet.models import Bar, Direction


NOW = pd.Timestamp("2024-01-08 10:05", tz="America/New_York")


def _account(**overrides):
    values = {
        "account_id": "paper-1",
        "status": "ACTIVE",
        "equity": 100_000,
        "cash": 50_000,
        "buying_power": 50_000,
        "options_level": 3,
    }
    values.update(overrides)
    return BrokerAccount(**values)


def _trade(status=ManagedTradeStatus.PENDING_ENTRY):
    return TrackedTrade(
        "sb-opt-1",
        "alpaca",
        "options",
        "ES",
        Direction.LONG,
        "order-1",
        "sb-opt-1",
        (
            TrackedLeg(
                "SPY240108C00470000",
                1,
                AssetClass.OPTION,
                "sell",
                "sell_to_close",
            ),
        ),
        {"SPY240108C00470000": 0},
        95,
        110,
        NOW,
        NOW,
        status,
    )


def test_execution_store_persists_trades_and_kill_switch(tmp_path: Path):
    path = tmp_path / "execution-state.json"
    store = ExecutionStore(path)
    store.record(_trade())
    store.engage_kill_switch("operator test", as_of=NOW)

    restored = ExecutionStore(path)
    assert restored.get("sb-opt-1") == _trade()
    assert restored.kill_switch.engaged
    assert restored.kill_switch.reason == "operator test"

    restored.release_kill_switch()
    assert not ExecutionStore(path).kill_switch.engaged


def test_reconciler_opens_filled_trade_when_position_matches():
    store = ExecutionStore()
    store.record(_trade())
    snapshot = BrokerSnapshot(
        "alpaca",
        NOW + pd.Timedelta(seconds=1),
        _account(),
        (
            BrokerOrder(
                "order-1",
                "sb-opt-1",
                "SPY240108C00470000",
                "buy",
                1,
                1,
                BrokerOrderStatus.FILLED,
                AssetClass.OPTION,
            ),
        ),
        (
            BrokerPosition(
                "SPY240108C00470000",
                1,
                5,
                5.1,
                510,
                10,
                AssetClass.OPTION,
            ),
        ),
    )
    report = ExecutionReconciler(store).reconcile(snapshot)
    assert report.healthy
    assert store.get("sb-opt-1").status is ManagedTradeStatus.OPEN


def test_reconciler_engages_kill_switch_on_position_drift():
    store = ExecutionStore()
    store.record(_trade(ManagedTradeStatus.OPEN))
    snapshot = BrokerSnapshot(
        "alpaca",
        NOW,
        _account(),
        (),
        (),
    )
    report = ExecutionReconciler(store).reconcile(snapshot)
    assert not report.healthy
    assert report.issues[0].kind == "position_mismatch"
    assert store.kill_switch.engaged


def test_risk_gate_blocks_kill_switch_and_order_risk():
    store = ExecutionStore()
    gate = LiveRiskGate(store, LiveRiskLimits(maximum_order_risk=500))
    snapshot = BrokerSnapshot("alpaca", NOW, _account(), (), ())
    with pytest.raises(RuntimeError, match="order risk"):
        gate.validate(snapshot, estimated_order_risk=501)
    store.engage_kill_switch("test", as_of=NOW)
    with pytest.raises(RuntimeError, match="kill switch"):
        gate.validate(snapshot, estimated_order_risk=100)


class FakeCloser:
    def __init__(self):
        self.calls = []

    def close_trade(self, trade, reason):
        self.calls.append((trade, reason))
        return OrderSubmission(
            "alpaca", "exit-1", "sb-exit-1", True, False, {"type": "market"}
        )


def test_synthetic_exit_is_stop_first_and_submits_once():
    store = ExecutionStore()
    store.record(_trade(ManagedTradeStatus.OPEN))
    closer = FakeCloser()
    manager = SyntheticExitManager(store, {"alpaca": closer})
    bar = Bar(NOW, "ES", 100, 111, 94, 105, 10, "ESH4")

    result = manager.on_bar(bar)

    assert len(result) == 1
    assert closer.calls[0][1] == "futures_stop"
    assert store.get("sb-opt-1").status is ManagedTradeStatus.EXITING
    assert manager.on_bar(bar) == ()
