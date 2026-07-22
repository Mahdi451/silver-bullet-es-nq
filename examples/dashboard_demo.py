"""Serve the operator dashboard with safe, in-memory demonstration data."""

from __future__ import annotations

import argparse
import signal
import threading

import pandas as pd

from silver_bullet import (
    Bar,
    BrokerAccount,
    BrokerOrder,
    BrokerOrderStatus,
    BrokerPosition,
    BrokerSnapshot,
    DashboardConfig,
    Direction,
    ExecutionStore,
    FeedHealth,
    FeedState,
    LiveOperatorController,
    ManagedTradeStatus,
    OperatorDashboard,
    OperatorState,
    ProxyBar,
    ReconciliationReport,
    TrackedTrade,
)
from silver_bullet.broker import AssetClass, TrackedLeg


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--port", type=int, default=8765)
    return parser.parse_args()


def build_dashboard(port: int) -> OperatorDashboard:
    now = pd.Timestamp.now(tz="America/New_York").floor("s")
    bar_time = now.floor("min")
    store = ExecutionStore()
    store.record(
        TrackedTrade(
            trade_id="sb-demo-001",
            broker="alpaca",
            route="options",
            instrument="ES",
            direction=Direction.LONG,
            entry_order_id="paper-order-1042",
            client_order_id="sb-demo-001",
            legs=(
                TrackedLeg(
                    "SPY-DEMO-CALL",
                    1,
                    AssetClass.OPTION,
                    "sell",
                    "sell_to_close",
                ),
            ),
            baseline_positions={"SPY-DEMO-CALL": 0.0},
            stop_price=6316.75,
            target_price=6332.25,
            created_at=now - pd.Timedelta(minutes=7),
            updated_at=now - pd.Timedelta(seconds=18),
            status=ManagedTradeStatus.OPEN,
        )
    )

    state = OperatorState(store, mode="paper", clock=lambda: now)
    state.price_book.update(
        Bar(bar_time, "ES", 6320.75, 6324.0, 6319.5, 6323.25, 1842, "DEMO")
    )
    state.price_book.update(
        ProxyBar(bar_time, "SPY", 630.85, 631.22, 630.61, 631.08, 428_210)
    )
    state.update_feed(
        "databento_futures",
        FeedHealth(
            "databento",
            ("ES", "NQ"),
            FeedState.LIVE,
            started_at=now - pd.Timedelta(hours=2),
            last_message_at=now - pd.Timedelta(seconds=1),
            last_bar_at=now - pd.Timedelta(seconds=1),
            message_count=18_426,
            bar_count=238,
        ),
    )
    state.update_feed(
        "alpaca_proxy",
        FeedHealth(
            "alpaca",
            ("SPY", "QQQ"),
            FeedState.LIVE,
            started_at=now - pd.Timedelta(hours=2),
            last_message_at=now - pd.Timedelta(seconds=2),
            last_bar_at=now - pd.Timedelta(seconds=2),
            message_count=9_214,
            bar_count=238,
        ),
    )
    state.update_broker(
        BrokerSnapshot(
            "alpaca",
            now,
            BrokerAccount(
                "paper-demo",
                "ACTIVE",
                100_420.0,
                81_300.0,
                162_600.0,
                options_buying_power=74_500.0,
                options_level=3,
                daily_pl=420.0,
            ),
            (
                BrokerOrder(
                    "paper-order-1042",
                    "sb-demo-001",
                    "SPY-DEMO-CALL",
                    "buy",
                    1,
                    1,
                    BrokerOrderStatus.FILLED,
                    AssetClass.OPTION,
                    submitted_at=now - pd.Timedelta(minutes=7),
                    filled_at=now - pd.Timedelta(minutes=6, seconds=58),
                    order_type="limit",
                    limit_price=3.15,
                ),
            ),
            (
                BrokerPosition(
                    "SPY-DEMO-CALL",
                    1,
                    3.12,
                    3.28,
                    328.0,
                    16.0,
                    AssetClass.OPTION,
                ),
            ),
        )
    )
    state.update_reconciliation(ReconciliationReport("alpaca", now, (), ()))
    state.event("info", "strategy", "Warmup completed; historical signals discarded")
    state.event("info", "market-data", "Databento and Alpaca feeds are healthy")
    state.event("info", "broker", "Broker reconciliation completed with no drift")
    state.event("warning", "execution", "Demo option position is under synthetic protection")

    controller = LiveOperatorController(store, state, {}, clock=lambda: now)
    return OperatorDashboard(
        state,
        controller,
        DashboardConfig(host="127.0.0.1", port=port),
    )


def main() -> int:
    args = parse_args()
    dashboard = build_dashboard(args.port)
    stopped = threading.Event()

    def request_stop(*_: object) -> None:
        stopped.set()

    for item in (signal.SIGINT, signal.SIGTERM):
        signal.signal(item, request_stop)

    dashboard.start()
    host, port = dashboard.address
    print(f"Demo dashboard: http://{host}:{port}")
    print("This process has no broker clients and cannot submit an order.")
    try:
        stopped.wait()
    finally:
        dashboard.stop()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
