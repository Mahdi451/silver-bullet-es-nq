"""Load a supported historical CSV and print Silver Bullet entry intents."""

from __future__ import annotations

import argparse

from silver_bullet import CSVBarLoader, CSVSchema, SilverBulletStrategy


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("csv", help="Path to a one-minute CSV export")
    parser.add_argument("instrument", choices=("ES", "NQ"))
    parser.add_argument(
        "--provider", choices=("databento", "firstrate"), required=True
    )
    parser.add_argument("--contract", help="Optional source contract label")
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    factory = (
        CSVSchema.databento
        if args.provider == "databento"
        else CSVSchema.firstrate
    )
    schema = factory(args.instrument, contract=args.contract)
    loaded = CSVBarLoader(schema).load(args.csv)
    strategy = SilverBulletStrategy(args.instrument)

    print(
        f"Loaded {loaded.report.rows:,} rows; "
        f"missing open-market minutes: {loaded.report.missing_minutes:,}"
    )
    for signal in strategy.scan(loaded):
        print(
            signal.signal_time,
            signal.session,
            signal.direction.value,
            f"target={signal.target.kind.value}@{signal.target.price}",
            f"execute={signal.execution_time}",
            f"stop={signal.proposed_stop}",
        )


if __name__ == "__main__":
    main()
