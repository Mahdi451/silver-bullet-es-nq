from __future__ import annotations

import pandas as pd
import pytest

from silver_bullet import CSVBarLoader, CSVSchema, DataValidationError


def _write_csv(tmp_path, rows, name="bars.csv"):
    path = tmp_path / name
    pd.DataFrame(rows).to_csv(path, index=False)
    return path


def test_loads_sorts_and_converts_utc_to_new_york(tmp_path):
    path = _write_csv(
        tmp_path,
        [
            {
                "ts_event": "2024-07-01T14:01:00Z",
                "open": 101,
                "high": 102,
                "low": 100,
                "close": 101.5,
                "volume": 10,
            },
            {
                "ts_event": "2024-07-01T14:00:00Z",
                "open": 100,
                "high": 101,
                "low": 99,
                "close": 100.5,
                "volume": 9,
            },
        ],
    )

    result = CSVBarLoader(CSVSchema.databento("ES", contract="ESU4")).load(path)

    assert list(result.frame["timestamp"].dt.strftime("%H:%M")) == ["10:00", "10:01"]
    assert str(result.frame.iloc[0].timestamp.tz) == "America/New_York"
    assert result.report.rows == 2
    assert result.report.missing_minutes == 0
    bars = list(result)
    assert bars[0].instrument == "ES"
    assert bars[0].contract == "ESU4"


def test_supports_separate_firstrate_date_and_time_columns(tmp_path):
    path = _write_csv(
        tmp_path,
        [
            {
                "Date": "2024-01-08",
                "Time": "10:00:00",
                "Open": 100,
                "High": 101,
                "Low": 99,
                "Close": 100.5,
                "Volume": 5,
            }
        ],
    )
    result = CSVBarLoader(CSVSchema.firstrate("NQ")).load(path)
    assert result.frame.iloc[0].timestamp.hour == 10
    assert result.instrument == "NQ"


@pytest.mark.parametrize(
    "timestamp",
    ["2024-03-10 02:30:00", "2024-11-03 01:30:00"],
)
def test_rejects_nonexistent_or_ambiguous_new_york_times(tmp_path, timestamp):
    path = _write_csv(
        tmp_path,
        [
            {
                "timestamp": timestamp,
                "open": 100,
                "high": 101,
                "low": 99,
                "close": 100,
                "volume": 1,
            }
        ],
    )
    schema = CSVSchema(instrument="ES", source_timezone="America/New_York")
    with pytest.raises(DataValidationError, match="timestamp"):
        CSVBarLoader(schema).load(path)


@pytest.mark.parametrize(
    ("rows", "message"),
    [
        (
            [
                ["2024-01-08 10:00", 100, 101, 99, 100, 1],
                ["2024-01-08 10:00", 100, 101, 99, 100, 1],
            ],
            "duplicate",
        ),
        ([["2024-01-08 10:00", 100, 99, 98, 100, 1]], "OHLC"),
        ([["2024-01-08 10:00", 100, 101, 99, 100, -1]], "volume"),
    ],
)
def test_rejects_unsafe_market_data(tmp_path, rows, message):
    path = tmp_path / "bad.csv"
    pd.DataFrame(
        rows, columns=["timestamp", "open", "high", "low", "close", "volume"]
    ).to_csv(path, index=False)
    schema = CSVSchema(instrument="ES", source_timezone="America/New_York")
    with pytest.raises(DataValidationError, match=message):
        CSVBarLoader(schema).load(path)


def test_reports_missing_open_minutes_but_ignores_weekend_closure(tmp_path):
    rows = []
    for timestamp in (
        "2024-01-05 16:59:00",  # Friday close
        "2024-01-07 18:00:00",  # Sunday reopen
        "2024-01-07 18:02:00",  # 18:01 is actually missing
    ):
        rows.append(
            {
                "timestamp": timestamp,
                "open": 100,
                "high": 101,
                "low": 99,
                "close": 100,
                "volume": 1,
            }
        )
    path = _write_csv(tmp_path, rows)
    schema = CSVSchema(instrument="ES", source_timezone="America/New_York")
    report = CSVBarLoader(schema).load(path).report
    assert report.missing_minutes == 1
    assert report.missing_intervals[0].start.strftime("%a %H:%M") == "Sun 18:01"


def test_rejects_off_tick_prices(tmp_path):
    path = _write_csv(
        tmp_path,
        [
            {
                "timestamp": "2024-01-08 10:00",
                "open": 100.10,
                "high": 101.00,
                "low": 99.00,
                "close": 100.50,
                "volume": 1,
            }
        ],
    )
    schema = CSVSchema(instrument="ES", source_timezone="America/New_York")
    with pytest.raises(DataValidationError, match="tick grid"):
        CSVBarLoader(schema).load(path)


def test_contract_column_and_provenance_are_preserved(tmp_path):
    path = _write_csv(
        tmp_path,
        [
            {
                "timestamp": "2024-01-08 10:00",
                "open": 100,
                "high": 101,
                "low": 99,
                "close": 100.5,
                "volume": 1,
                "symbol": "ESH4",
            }
        ],
    )
    schema = CSVSchema(
        instrument="ES",
        source_timezone="America/New_York",
        contract_col="symbol",
        provider="fixture",
    )
    result = CSVBarLoader(schema).load(path)
    assert list(result)[0].contract == "ESH4"
    assert result.report.contracts == ("ESH4",)
    assert len(result.report.source_sha256) == 64


def test_databento_raw_fixed_precision_prices_are_normalized(tmp_path):
    path = _write_csv(
        tmp_path,
        [
            {
                "ts_event": 1_704_727_200_000_000_000,
                "open": 5_000_000_000_000,
                "high": 5_001_000_000_000,
                "low": 4_999_000_000_000,
                "close": 5_000_250_000_000,
                "volume": 10,
                "symbol": "ESH4",
            }
        ],
    )
    result = CSVBarLoader(CSVSchema.databento("ES")).load(path)
    bar = list(result)[0]
    assert bar.open == 5000
    assert bar.close == 5000.25
    assert bar.contract == "ESH4"
