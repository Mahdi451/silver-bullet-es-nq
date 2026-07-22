from __future__ import annotations

import asyncio
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
    ExecutionStore,
    ManagedTradeStatus,
    OrderSubmission,
    TrackedLeg,
    TrackedTrade,
)
from silver_bullet.live_data import FeedHealth, FeedState, ProxyBar
from silver_bullet.models import (
    Bar,
    Direction,
    FairValueGap,
    LiquidityKind,
    LiquidityLevel,
    TradeSignal,
)
from silver_bullet.runtime import (
    EntryTiming,
    ExecutionRoute,
    LiveRuntime,
    LiveRuntimeConfig,
    RuntimeMode,
)


NOW = pd.Timestamp("2024-01-08 10:00", tz="America/New_York")


def _bar(timestamp=NOW, *, open_price=100.0):
    return Bar(
        timestamp,
        "ES",
        open_price,
        open_price + 2,
        open_price - 2,
        open_price + 1,
        100,
        "ESH4",
    )


def _signal(signal_time=NOW):
    gap = FairValueGap(
        Direction.LONG,
        99,
        100,
        signal_time,
        signal_time - pd.Timedelta(minutes=2),
        signal_time - pd.Timedelta(minutes=1),
    )
    target = LiquidityLevel(
        "PDH-test",
        LiquidityKind.PDH,
        110,
        "2024-01-05",
        signal_time - pd.Timedelta(days=1),
    )
    return TradeSignal(
        "ES",
        "ny_am",
        Direction.LONG,
        signal_time,
        signal_time + pd.Timedelta(minutes=1),
        target,
        gap,
        gap,
        signal_time,
        96,
        95,
        "ESH4",
    )


def _account():
    return BrokerAccount(
        "paper-1",
        "ACTIVE",
        100_000,
        50_000,
        50_000,
        options_buying_power=25_000,
        options_level=3,
    )


class FakeFeed:
    def __init__(self, bars=(), *, provider="fake-futures"):
        self.bars = tuple(bars)
        self._health = FeedHealth(
            provider,
            ("ES",),
            state=FeedState.LIVE,
            started_at=NOW,
        )
        self.stopped = False

    @property
    def health(self):
        return self._health

    async def run(self, callback):
        for bar in self.bars:
            if self.stopped:
                break
            await callback(bar)

    def stop(self):
        self.stopped = True


class FakeStrategy:
    instrument = "ES"

    def __init__(self, signals=()):
        self.signals = {signal.signal_time: signal for signal in signals}
        self.seen = []

    def on_bar(self, bar):
        self.seen.append(bar.timestamp)
        return self.signals.get(bar.timestamp)


class FailingStrategy(FakeStrategy):
    def on_bar(self, bar):
        raise RuntimeError("simulated strategy failure")


class FakeFuturesBroker:
    name = "tradovate"

    def __init__(self, snapshot=None):
        self._snapshot = snapshot
        self.submissions = []

    def snapshot(self, *, as_of=None):
        if self._snapshot is not None:
            return self._snapshot
        return BrokerSnapshot(self.name, as_of or NOW, _account(), (), ())

    def submit_order(self, signal, **kwargs):
        self.submissions.append((signal, kwargs))
        return OrderSubmission(
            self.name,
            None if kwargs["dry_run"] else "future-order-1",
            "sb-future-preview",
            not kwargs["dry_run"],
            kwargs["dry_run"],
            {"reference_price": kwargs["reference_price"]},
        )


class FakeOptionChain:
    def __init__(self):
        self.calls = []

    def fetch(self, underlying, **kwargs):
        self.calls.append((underlying, kwargs))
        return object()


class FakeSelector:
    def __init__(self):
        self.calls = []

    def select(self, signal, chain, **kwargs):
        self.calls.append((signal, chain, kwargs))
        return ("selected-option",)


class FakeOptionBroker:
    name = "alpaca"

    def __init__(self):
        self.submissions = []

    def snapshot(self, *, as_of=None):
        return BrokerSnapshot(self.name, as_of or NOW, _account(), (), ())

    def submit_option(self, candidate, **kwargs):
        self.submissions.append((candidate, kwargs))
        return OrderSubmission(
            self.name,
            None if kwargs["dry_run"] else "option-order-1",
            "sb-option-preview",
            not kwargs["dry_run"],
            kwargs["dry_run"],
            {"candidate": candidate},
        )

    def close_trade(self, trade, reason):
        raise AssertionError("no synthetic exit was expected")


def test_live_signal_routes_at_next_minute_boundary_without_waiting_another_bar():
    async def exercise():
        broker = FakeFuturesBroker()
        signal = _signal()
        runtime = LiveRuntime(
            futures_feed=FakeFeed(),
            strategies={"ES": FakeStrategy((signal,))},
            store=ExecutionStore(),
            config=LiveRuntimeConfig(route=ExecutionRoute.FUTURES),
            futures_broker=broker,
            clock=lambda: signal.execution_time,
        )

        result = await runtime.process_futures_bar(_bar())

        assert result is not None and result.dry_run
        assert broker.submissions[0][1]["reference_price"] == 101
        entry_bar = _bar(NOW + pd.Timedelta(minutes=1), open_price=101)
        assert await runtime.process_futures_bar(entry_bar) is None
        assert len(broker.submissions) == 1
        assert runtime.pending_signals == {}

    asyncio.run(exercise())


def test_missed_entry_minute_is_canceled_instead_of_filled_late():
    async def exercise():
        broker = FakeFuturesBroker()
        runtime = LiveRuntime(
            futures_feed=FakeFeed(),
            strategies={"ES": FakeStrategy((_signal(),))},
            store=ExecutionStore(),
            config=LiveRuntimeConfig(
                route=ExecutionRoute.FUTURES,
                entry_timing=EntryTiming.NEXT_BAR_OPEN,
            ),
            futures_broker=broker,
        )
        await runtime.process_futures_bar(_bar())
        await runtime.process_futures_bar(
            _bar(NOW + pd.Timedelta(minutes=2), open_price=102)
        )
        assert broker.submissions == []
        assert runtime.pending_signals == {}

    asyncio.run(exercise())


def test_live_signal_is_canceled_when_completed_bar_arrives_too_late():
    async def exercise():
        broker = FakeFuturesBroker()
        signal = _signal()
        runtime = LiveRuntime(
            futures_feed=FakeFeed(),
            strategies={"ES": FakeStrategy((signal,))},
            store=ExecutionStore(),
            config=LiveRuntimeConfig(
                route=ExecutionRoute.FUTURES,
                maximum_entry_delay_seconds=5,
            ),
            futures_broker=broker,
            clock=lambda: signal.execution_time + pd.Timedelta(seconds=6),
        )

        assert await runtime.process_futures_bar(_bar()) is None
        assert broker.submissions == []

    asyncio.run(exercise())


def test_options_route_uses_proxy_bar_chain_selection_and_paper_broker():
    async def exercise():
        chain = FakeOptionChain()
        selector = FakeSelector()
        broker = FakeOptionBroker()
        runtime = LiveRuntime(
            futures_feed=FakeFeed(),
            proxy_feed=FakeFeed(provider="fake-proxy"),
            strategies={"ES": FakeStrategy((_signal(),))},
            store=ExecutionStore(),
            config=LiveRuntimeConfig(
                route=ExecutionRoute.OPTIONS,
                mode=RuntimeMode.PAPER,
                proxy_wait_seconds=0,
            ),
            option_chain=chain,
            options_selector=selector,
            option_broker=broker,
            clock=lambda: NOW + pd.Timedelta(minutes=1),
        )
        await runtime.process_proxy_bar(
            ProxyBar(NOW, "SPY", 500, 501, 499, 500.5, 1_000)
        )
        result = await runtime.process_futures_bar(_bar())

        assert result is not None and result.submitted
        assert chain.calls[0][0] == "SPY"
        assert chain.calls[0][1]["underlying_price"] == 500.5
        expected_target = 500.5 * _signal().target.price / 101
        assert selector.calls[0][2]["proxy_target"] == pytest.approx(
            expected_target
        )
        assert broker.submissions[0][0] == "selected-option"

    asyncio.run(exercise())


def test_warmup_discards_historical_signals_without_scheduling_orders():
    strategy = FakeStrategy((_signal(),))
    broker = FakeFuturesBroker()
    runtime = LiveRuntime(
        futures_feed=FakeFeed(),
        strategies={"ES": strategy},
        store=ExecutionStore(),
        config=LiveRuntimeConfig(route=ExecutionRoute.FUTURES),
        futures_broker=broker,
    )

    counts = runtime.warmup({"ES": [_bar()]})

    assert counts == {"ES": 1}
    assert runtime.pending_signals == {}
    assert broker.submissions == []


def test_restart_loads_store_and_reconciles_filled_trade(tmp_path: Path):
    state_path = tmp_path / "execution-state.json"
    initial_store = ExecutionStore(state_path)
    initial_store.record(
        TrackedTrade(
            "sb-restart-1",
            "tradovate",
            "futures",
            "ES",
            Direction.LONG,
            "order-1",
            "sb-restart-1",
            (TrackedLeg("ESH4", 1, AssetClass.FUTURE, "Sell", contract_id=42),),
            {"ESH4": 0},
            95,
            110,
            NOW,
            NOW,
        )
    )
    restored = ExecutionStore(state_path)
    snapshot_time = NOW + pd.Timedelta(minutes=2)
    broker = FakeFuturesBroker(
        BrokerSnapshot(
            "tradovate",
            snapshot_time,
            _account(),
            (
                BrokerOrder(
                    "order-1",
                    "sb-restart-1",
                    "ESH4",
                    "Buy",
                    1,
                    1,
                    BrokerOrderStatus.FILLED,
                    AssetClass.FUTURE,
                ),
            ),
            (
                BrokerPosition(
                    "ESH4",
                    1,
                    100,
                    101,
                    50,
                    50,
                    AssetClass.FUTURE,
                    contract_id=42,
                ),
            ),
        )
    )
    runtime = LiveRuntime(
        futures_feed=FakeFeed(),
        strategies={"ES": FakeStrategy()},
        store=restored,
        config=LiveRuntimeConfig(route=ExecutionRoute.FUTURES),
        futures_broker=broker,
        clock=lambda: snapshot_time,
    )

    reports = asyncio.run(runtime.reconcile_once())

    assert reports["tradovate"].healthy
    assert restored.get("sb-restart-1").status is ManagedTradeStatus.OPEN


def test_runtime_error_engages_persistent_kill_switch_and_stops():
    store = ExecutionStore()
    runtime = LiveRuntime(
        futures_feed=FakeFeed((_bar(),)),
        strategies={"ES": FailingStrategy()},
        store=store,
        config=LiveRuntimeConfig(
            route=ExecutionRoute.FUTURES,
            reconciliation_interval_seconds=None,
        ),
        futures_broker=FakeFuturesBroker(),
        clock=lambda: NOW,
    )

    with pytest.raises(RuntimeError, match="simulated strategy failure"):
        asyncio.run(runtime.run())

    assert store.kill_switch.engaged
    assert "runtime error" in store.kill_switch.reason
    assert not runtime.running


def test_runtime_config_normalizes_strings_without_bypassing_live_confirmation():
    config = LiveRuntimeConfig(
        route="futures",  # type: ignore[arg-type]
        mode="dry_run",  # type: ignore[arg-type]
        proxy_symbols={"es": "spy"},
    )
    assert config.route is ExecutionRoute.FUTURES
    assert config.mode is RuntimeMode.DRY_RUN
    assert config.proxy_symbols == {"ES": "SPY"}

    with pytest.raises(ValueError, match="explicit live"):
        LiveRuntimeConfig(mode="live")  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="fail-closed"):
        LiveRuntimeConfig(mode=RuntimeMode.PAPER, halt_on_error=False)
