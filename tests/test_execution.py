from __future__ import annotations

import json

import pandas as pd

from silver_bullet import (
    BacktestConfig,
    BacktestEngine,
    Bar,
    CostModel,
    Direction,
    FairValueGap,
    FillReason,
    LiquidityKind,
    LiquidityLevel,
    OptimizationRunner,
    RiskLimits,
    TearSheetBuilder,
    TradeSignal,
    WalkForwardSplit,
    calculate_metrics,
)


def _bar(minute, open_, high, low, close):
    return Bar(
        pd.Timestamp(f"2024-01-08 {minute}", tz="America/New_York"),
        "ES",
        open_,
        high,
        low,
        close,
        1,
        "ESH4",
    )


def _signal(timestamp="2024-01-08 10:00"):
    formed = pd.Timestamp(timestamp, tz="America/New_York")
    gap = FairValueGap(Direction.LONG, 98, 99, formed, formed, formed)
    target = LiquidityLevel("PDH:test", LiquidityKind.PDH, 110, "test", formed, False)
    return TradeSignal(
        "ES",
        "ny_am",
        Direction.LONG,
        formed,
        formed + pd.Timedelta(minutes=1),
        target,
        gap,
        gap,
        formed,
        95.25,
        95,
        "ESH4",
    )


class OneSignalStrategy:
    def __init__(self, signal):
        self.signal = signal
        self.sent = False

    def on_bar(self, bar):
        if not self.sent and bar.timestamp == self.signal.signal_time:
            self.sent = True
            return self.signal
        return None


def _engine():
    return BacktestEngine(BacktestConfig(100_000, CostModel({"ES": 2.50}), {"ES": 2}))


def test_scale_out_target_costs_and_account_reconcile():
    bars = [
        _bar("10:00", 99, 100, 98, 99.5),
        _bar("10:01", 100, 106, 99, 105),
        _bar("10:02", 106, 111, 105, 110),
    ]
    result = _engine().run(bars, {"ES": OneSignalStrategy(_signal())})
    trade = result.trades[0]
    assert [fill.reason for fill in trade.exits] == [
        FillReason.SCALE_OUT,
        FillReason.DOL_TARGET,
    ]
    assert trade.gross_pnl == 712.5
    assert trade.fees == 10
    assert trade.net_pnl == 702.5
    assert result.ending_balance == 100_702.5
    assert result.ending_balance == result.config.starting_balance + sum(
        trade.net_pnl for trade in result.trades
    )


def test_stop_wins_when_stop_and_targets_share_entry_bar():
    bars = [
        _bar("10:00", 99, 100, 98, 99.5),
        _bar("10:01", 100, 111, 94, 100),
    ]
    result = _engine().run(bars, {"ES": OneSignalStrategy(_signal())})
    assert len(result.trades) == 1
    assert result.trades[0].exit_reason is FillReason.STOP
    assert len(result.trades[0].exits) == 1


def test_missing_execution_bar_cancels_order():
    bars = [
        _bar("10:00", 99, 100, 98, 99.5),
        _bar("10:02", 100, 101, 99, 100),
    ]
    result = _engine().run(bars, {"ES": OneSignalStrategy(_signal())})
    assert result.trades == []
    assert result.cancellations[0][2] == "missing_execution_bar"


def test_daily_risk_violation_is_recorded():
    bars = [
        _bar("10:00", 99, 100, 98, 99.5),
        _bar("10:01", 100, 101, 94, 95),
    ]
    engine = BacktestEngine(
        BacktestConfig(
            100_000,
            CostModel({"ES": 2.50}),
            {"ES": 2},
            risk_limits=RiskLimits(daily_loss_limit=100),
        )
    )
    result = engine.run(bars, {"ES": OneSignalStrategy(_signal())})
    assert result.violations[0].rule == "daily_loss"
    assert result.violations[0].drawdown >= 100


def test_forced_close_and_empty_safe_analytics(tmp_path):
    bars = [
        _bar("10:00", 99, 100, 98, 99.5),
        _bar("10:01", 100, 102, 99, 101),
        _bar("15:59", 102, 103, 101, 102),
    ]
    result = _engine().run(bars, {"ES": OneSignalStrategy(_signal())})
    assert result.trades[0].exit_reason is FillReason.FORCED_CLOSE
    paths = TearSheetBuilder().build(result, tmp_path, {"fixture": True})
    assert all(path.exists() for path in paths.values())
    payload = json.loads(paths["metrics"].read_text())
    assert payload["metrics"]["trade_count"] == 1
    assert calculate_metrics(result)["total_net_profit"] == result.trades[0].net_pnl


def test_walk_forward_parameter_grid_is_deterministic(tmp_path):
    split = WalkForwardSplit(
        pd.Timestamp("2024-01-01"),
        pd.Timestamp("2024-01-31"),
        pd.Timestamp("2024-02-01"),
        pd.Timestamp("2024-02-29"),
    )

    def evaluator(params, start, end):
        return {
            "score": params["atr"],
            "maximum_drawdown": 10,
            "trade_count": 5,
        }

    records = OptimizationRunner().run({"atr": [10, 14]}, [split], evaluator)
    assert [record.test_score for record in records] == [10, 14]
    path = OptimizationRunner.save(records, tmp_path / "optimization.csv")
    assert path.exists()
