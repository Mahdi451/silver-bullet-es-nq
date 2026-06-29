from __future__ import annotations

import pandas as pd

from silver_bullet import (
    BacktestConfig,
    BacktestEngine,
    CSVBarLoader,
    CSVSchema,
    CostModel,
    FillReason,
    LiquidityLevelTracker,
    SilverBulletConfig,
    SilverBulletStrategy,
)


def test_csv_levels_strategy_execution_and_accounting_end_to_end(tmp_path):
    rows = [
        ("2024-01-07 18:00", 100, 101, 99, 100),
        ("2024-01-08 16:59", 100, 120, 80, 100),
        ("2024-01-08 18:00", 100, 101, 99, 100),
    ]
    pattern = [
        ("2024-01-09 09:55", 100, 101, 99, 100),
        ("2024-01-09 09:56", 100, 103, 99.5, 102),
        ("2024-01-09 09:57", 102, 102.5, 100, 101),
        ("2024-01-09 09:58", 101, 102, 98, 99),
        ("2024-01-09 09:59", 99, 101, 99, 100),
        ("2024-01-09 10:00", 100, 101, 99.5, 100.5),
        ("2024-01-09 10:01", 100.5, 106.5, 100.25, 106),
        ("2024-01-09 10:02", 106, 107, 102, 106.5),
        ("2024-01-09 10:03", 102, 103, 101.5, 102.5),
        ("2024-01-09 10:04", 102.5, 108, 101.75, 107),
        ("2024-01-09 10:05", 107, 109, 104, 108),
        ("2024-01-09 10:06", 108, 121, 107, 120),
    ]
    rows.extend(pattern)
    frame = pd.DataFrame(rows, columns=["timestamp", "open", "high", "low", "close"])
    frame["volume"] = 1
    path = tmp_path / "es.csv"
    frame.to_csv(path, index=False)

    loaded = CSVBarLoader(CSVSchema("ES", "America/New_York", contract="ESH4")).load(
        path
    )
    strategy = SilverBulletStrategy(
        "ES",
        SilverBulletConfig(atr_period=3, pivot_width=1),
        LiquidityLevelTracker(require_complete_periods=False),
    )
    result = BacktestEngine(
        BacktestConfig(100_000, CostModel({"ES": 2.5}), {"ES": 2})
    ).run(loaded, {"ES": strategy})

    assert len(result.trades) == 1
    assert result.trades[0].exit_reason is FillReason.DOL_TARGET
    assert result.trades[0].target_price == 120
    assert result.ending_balance == 100_802.5
