"""Command-line entrypoint for deterministic historical backtests."""

from __future__ import annotations

import argparse
import json
from dataclasses import asdict
from pathlib import Path

from .analytics import TearSheetBuilder, calculate_metrics
from .data import CSVBarLoader, CSVSchema
from .execution import BacktestConfig, BacktestEngine, CostModel
from .strategy import SilverBulletStrategy


def _pairs(values: list[str], label: str) -> dict[str, str]:
    result = {}
    for value in values:
        if "=" not in value:
            raise ValueError(f"{label} must use SYMBOL=value")
        symbol, item = value.split("=", 1)
        symbol = symbol.upper()
        if symbol not in {"ES", "NQ"}:
            raise ValueError(f"unsupported instrument: {symbol}")
        result[symbol] = item
    return result


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, help="Optional JSON run configuration")
    parser.add_argument("--data", action="append", default=[], help="ES=path.csv")
    parser.add_argument("--fee", action="append", default=[], help="ES=2.50 per side")
    parser.add_argument("--contract", action="append", default=[], help="ES=ESH5")
    parser.add_argument("--provider", choices=("databento", "firstrate"))
    parser.add_argument("--starting-balance", type=float)
    parser.add_argument("--quantity", type=int)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--mode", choices=("combined", "separate", "both"))
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    file_config = json.loads(args.config.read_text()) if args.config else {}
    data_paths = {
        str(k).upper(): str(v) for k, v in file_config.get("data", {}).items()
    }
    data_paths.update(_pairs(args.data, "data"))
    fees = {str(k).upper(): float(v) for k, v in file_config.get("fees", {}).items()}
    fees.update(
        {symbol: float(value) for symbol, value in _pairs(args.fee, "fee").items()}
    )
    contracts = {
        str(k).upper(): str(v) for k, v in file_config.get("contracts", {}).items()
    }
    contracts.update(_pairs(args.contract, "contract"))
    provider = args.provider or file_config.get("provider", "databento")
    starting_balance = args.starting_balance or file_config.get(
        "starting_balance", 100_000
    )
    quantity = args.quantity or file_config.get("quantity", 2)
    output = args.output or Path(file_config.get("output", "backtest-results"))
    mode = args.mode or file_config.get("mode", "both")
    if not data_paths:
        raise ValueError("at least one data input is required")
    missing_fees = set(data_paths) - set(fees)
    if missing_fees:
        raise ValueError(f"missing fee inputs for: {sorted(missing_fees)}")

    loaded = {}
    for symbol, path in data_paths.items():
        factory = (
            CSVSchema.databento if provider == "databento" else CSVSchema.firstrate
        )
        schema = factory(symbol, contract=contracts.get(symbol))
        loaded[symbol] = CSVBarLoader(schema).load(path)

    config = BacktestConfig(
        starting_balance,
        CostModel(fees),
        {symbol: quantity for symbol in data_paths},
    )
    builder = TearSheetBuilder()

    def execute(symbols: list[str], directory: Path):
        engine = BacktestEngine(config)
        strategies = {symbol: SilverBulletStrategy(symbol) for symbol in symbols}
        sources = {symbol: loaded[symbol] for symbol in symbols}
        result = engine.run_sources(sources, strategies)
        metadata = {
            "provider": provider,
            "data": {symbol: asdict(loaded[symbol].report) for symbol in symbols},
            "costs": dict(fees),
            "quantity": quantity,
        }
        builder.build(result, directory, metadata)
        return calculate_metrics(result)

    if mode in {"separate", "both"}:
        for symbol in data_paths:
            metrics = execute([symbol], output / symbol.lower())
            print(symbol, metrics)
    if mode in {"combined", "both"}:
        metrics = execute(list(data_paths), output / "combined")
        print("COMBINED", metrics)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
