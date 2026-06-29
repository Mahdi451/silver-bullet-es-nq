"""Small, deterministic primitives for walk-forward parameter research."""

from __future__ import annotations

from dataclasses import dataclass
from itertools import product
from pathlib import Path
from typing import Any, Callable, Iterable, Mapping

import pandas as pd


@dataclass(frozen=True, slots=True)
class WalkForwardSplit:
    train_start: pd.Timestamp
    train_end: pd.Timestamp
    test_start: pd.Timestamp
    test_end: pd.Timestamp


@dataclass(frozen=True, slots=True)
class OptimizationRecord:
    parameters: Mapping[str, Any]
    split: WalkForwardSplit
    train_score: float
    test_score: float
    test_drawdown: float
    test_trades: int


def parameter_combinations(grid: Mapping[str, Iterable[Any]]):
    names = tuple(grid)
    for values in product(*(tuple(grid[name]) for name in names)):
        yield dict(zip(names, values))


class OptimizationRunner:
    """Evaluate fixed-cost strategy parameters across chronological splits."""

    def run(
        self,
        grid: Mapping[str, Iterable[Any]],
        splits: Iterable[WalkForwardSplit],
        evaluator: Callable[
            [Mapping[str, Any], pd.Timestamp, pd.Timestamp], Mapping[str, float]
        ],
    ) -> list[OptimizationRecord]:
        records = []
        for split in splits:
            for parameters in parameter_combinations(grid):
                train = evaluator(parameters, split.train_start, split.train_end)
                test = evaluator(parameters, split.test_start, split.test_end)
                records.append(
                    OptimizationRecord(
                        parameters,
                        split,
                        float(train["score"]),
                        float(test["score"]),
                        float(test["maximum_drawdown"]),
                        int(test["trade_count"]),
                    )
                )
        return records

    @staticmethod
    def save(records: Iterable[OptimizationRecord], path: str | Path) -> Path:
        rows = []
        for record in records:
            rows.append(
                {
                    **dict(record.parameters),
                    "train_start": record.split.train_start,
                    "train_end": record.split.train_end,
                    "test_start": record.split.test_start,
                    "test_end": record.split.test_end,
                    "train_score": record.train_score,
                    "test_score": record.test_score,
                    "test_drawdown": record.test_drawdown,
                    "test_trades": record.test_trades,
                }
            )
        destination = Path(path)
        destination.parent.mkdir(parents=True, exist_ok=True)
        pd.DataFrame(rows).to_csv(destination, index=False)
        return destination
