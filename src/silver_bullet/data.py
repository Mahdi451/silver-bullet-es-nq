"""Strict, provider-neutral CSV ingestion for one-minute futures bars."""

from __future__ import annotations

from dataclasses import dataclass, field
from hashlib import sha256
from pathlib import Path
from typing import Iterator

import numpy as np
import pandas as pd

from .calendar import CMECalendar, DEFAULT_CME_CALENDAR
from .models import Bar, INSTRUMENTS, NEW_YORK


class DataValidationError(ValueError):
    """Raised when source data cannot safely be used for a backtest."""


@dataclass(frozen=True, slots=True)
class CSVSchema:
    """Mapping from a provider export into the canonical OHLCV schema."""

    instrument: str
    source_timezone: str
    timestamp_col: str | None = "timestamp"
    date_col: str | None = None
    time_col: str | None = None
    timestamp_format: str | None = None
    open_col: str = "open"
    high_col: str = "high"
    low_col: str = "low"
    close_col: str = "close"
    volume_col: str = "volume"
    contract: str | None = None
    contract_col: str | None = None
    provider: str = "custom"
    price_divisor: float | None = 1.0

    def __post_init__(self) -> None:
        symbol = self.instrument.upper()
        if symbol not in INSTRUMENTS:
            raise ValueError("instrument must be ES or NQ")
        object.__setattr__(self, "instrument", symbol)
        if bool(self.timestamp_col) == bool(self.date_col and self.time_col):
            raise ValueError(
                "provide either timestamp_col or both date_col and time_col"
            )
        if not self.source_timezone:
            raise ValueError("source_timezone is required for deterministic parsing")
        if self.contract and self.contract_col:
            raise ValueError("provide contract or contract_col, not both")
        if self.price_divisor is not None and self.price_divisor <= 0:
            raise ValueError("price_divisor must be positive")

    @classmethod
    def databento(cls, instrument: str, *, contract: str | None = None) -> "CSVSchema":
        return cls(
            instrument=instrument,
            source_timezone="UTC",
            timestamp_col="ts_event",
            open_col="open",
            high_col="high",
            low_col="low",
            close_col="close",
            volume_col="volume",
            contract=contract,
            contract_col=None if contract else "symbol",
            provider="databento",
            price_divisor=None,
        )

    @classmethod
    def firstrate(cls, instrument: str, *, contract: str | None = None) -> "CSVSchema":
        return cls(
            instrument=instrument,
            source_timezone=NEW_YORK,
            timestamp_col=None,
            date_col="Date",
            time_col="Time",
            open_col="Open",
            high_col="High",
            low_col="Low",
            close_col="Close",
            volume_col="Volume",
            contract=contract,
            provider="firstrate",
        )


@dataclass(frozen=True, slots=True)
class MissingInterval:
    start: pd.Timestamp
    end: pd.Timestamp
    missing_minutes: int


@dataclass(frozen=True, slots=True)
class DataQualityReport:
    rows: int
    start: pd.Timestamp | None
    end: pd.Timestamp | None
    missing_intervals: tuple[MissingInterval, ...] = field(default_factory=tuple)
    source_sha256: str | None = None
    provider: str = "custom"
    contracts: tuple[str, ...] = field(default_factory=tuple)

    @property
    def missing_minutes(self) -> int:
        return sum(item.missing_minutes for item in self.missing_intervals)


@dataclass(slots=True)
class LoadResult:
    frame: pd.DataFrame
    report: DataQualityReport
    instrument: str
    contract: str | None

    def __iter__(self) -> Iterator[Bar]:
        columns = self.frame.columns
        contract_in_frame = "contract" in columns
        for row in self.frame.itertuples(index=False):
            yield Bar(
                timestamp=row.timestamp,
                instrument=self.instrument,
                open=float(row.open),
                high=float(row.high),
                low=float(row.low),
                close=float(row.close),
                volume=float(row.volume),
                contract=(row.contract if contract_in_frame else self.contract),
            )


class CSVBarLoader:
    """Load and validate one prepared ES or NQ series per invocation."""

    def __init__(
        self, schema: CSVSchema, calendar: CMECalendar = DEFAULT_CME_CALENDAR
    ) -> None:
        self.schema = schema
        self.calendar = calendar

    def load(self, path: str | Path) -> LoadResult:
        path = Path(path)
        source = pd.read_csv(path)
        timestamp = self._parse_timestamp(source)
        mapping = {
            self.schema.open_col: "open",
            self.schema.high_col: "high",
            self.schema.low_col: "low",
            self.schema.close_col: "close",
            self.schema.volume_col: "volume",
        }
        missing = [name for name in mapping if name not in source.columns]
        if missing:
            raise DataValidationError(f"missing required columns: {missing}")

        frame = source[list(mapping)].rename(columns=mapping).copy()
        frame.insert(0, "timestamp", timestamp)
        if self.schema.contract is not None:
            frame["contract"] = self.schema.contract
        elif self.schema.contract_col:
            if self.schema.contract_col not in source.columns:
                raise DataValidationError(
                    f"missing contract column: {self.schema.contract_col}"
                )
            frame["contract"] = source[self.schema.contract_col].astype(str)
        frame = frame.sort_values("timestamp", kind="stable").reset_index(drop=True)
        self._normalize_prices(frame)
        self._validate(frame)
        intervals = _find_missing_intervals(frame["timestamp"], self.calendar)
        contracts = (
            tuple(frame["contract"].dropna().astype(str).unique())
            if "contract" in frame
            else ()
        )
        report = DataQualityReport(
            rows=len(frame),
            start=None if frame.empty else frame.iloc[0]["timestamp"],
            end=None if frame.empty else frame.iloc[-1]["timestamp"],
            missing_intervals=tuple(intervals),
            source_sha256=sha256(path.read_bytes()).hexdigest(),
            provider=self.schema.provider,
            contracts=contracts,
        )
        return LoadResult(
            frame=frame,
            report=report,
            instrument=self.schema.instrument,
            contract=self.schema.contract,
        )

    def _parse_timestamp(self, source: pd.DataFrame) -> pd.Series:
        if self.schema.timestamp_col:
            column = self.schema.timestamp_col
            if column not in source.columns:
                raise DataValidationError(f"missing timestamp column: {column}")
            raw = source[column]
        else:
            assert self.schema.date_col and self.schema.time_col
            missing = [
                col
                for col in (self.schema.date_col, self.schema.time_col)
                if col not in source.columns
            ]
            if missing:
                raise DataValidationError(f"missing timestamp columns: {missing}")
            raw = (
                source[self.schema.date_col].astype(str).str.strip()
                + " "
                + source[self.schema.time_col].astype(str).str.strip()
            )
        try:
            parsed = pd.to_datetime(
                raw,
                format=self.schema.timestamp_format,
                errors="raise",
            )
            if isinstance(parsed.dtype, pd.DatetimeTZDtype):
                localized = parsed.dt.tz_convert(NEW_YORK)
            else:
                localized = parsed.dt.tz_localize(
                    self.schema.source_timezone,
                    ambiguous="raise",
                    nonexistent="raise",
                ).dt.tz_convert(NEW_YORK)
        except (TypeError, ValueError) as exc:
            raise DataValidationError(f"invalid timestamp data: {exc}") from exc
        return localized.rename("timestamp")

    def _normalize_prices(self, frame: pd.DataFrame) -> None:
        columns = ["open", "high", "low", "close"]
        try:
            frame[columns] = frame[columns].apply(pd.to_numeric, errors="raise")
        except (TypeError, ValueError) as exc:
            raise DataValidationError(f"price columns must be numeric: {exc}") from exc
        divisor = self.schema.price_divisor
        if divisor is None:
            median = float(np.nanmedian(np.abs(frame[columns].to_numpy(dtype=float))))
            divisor = 1_000_000_000.0 if median > 100_000_000 else 1.0
        if divisor != 1.0:
            frame[columns] = frame[columns] / divisor

    @staticmethod
    def _validate(frame: pd.DataFrame) -> None:
        if frame.empty:
            raise DataValidationError("CSV contains no rows")
        timestamps = frame["timestamp"]
        if timestamps.duplicated().any():
            duplicates = timestamps[timestamps.duplicated(keep=False)].unique()
            raise DataValidationError(f"duplicate timestamps: {list(duplicates[:5])}")
        if any(
            (ts.second != 0 or ts.microsecond != 0 or getattr(ts, "nanosecond", 0) != 0)
            for ts in timestamps
        ):
            raise DataValidationError(
                "timestamps must align to exact minute boundaries"
            )

        numeric = ["open", "high", "low", "close", "volume"]
        try:
            frame[numeric] = frame[numeric].apply(pd.to_numeric, errors="raise")
        except (TypeError, ValueError) as exc:
            raise DataValidationError(f"OHLCV columns must be numeric: {exc}") from exc
        if not np.isfinite(frame[numeric].to_numpy(dtype=float)).all():
            raise DataValidationError("OHLCV values must be finite")
        if (frame["volume"] < 0).any():
            raise DataValidationError("volume cannot be negative")
        tick = INSTRUMENTS["ES"].tick_size
        prices = frame[["open", "high", "low", "close"]].to_numpy(dtype=float)
        if not np.allclose(prices / tick, np.round(prices / tick), atol=1e-8):
            raise DataValidationError("prices must align to the 0.25-point tick grid")
        invalid = (frame["high"] < frame[["open", "low", "close"]].max(axis=1)) | (
            frame["low"] > frame[["open", "high", "close"]].min(axis=1)
        )
        if invalid.any():
            rows = list(frame.index[invalid][:5])
            raise DataValidationError(f"invalid OHLC relationships at rows: {rows}")


def _find_missing_intervals(
    timestamps: pd.Series, calendar: CMECalendar = DEFAULT_CME_CALENDAR
) -> list[MissingInterval]:
    result: list[MissingInterval] = []
    values = list(timestamps)
    for previous, current in zip(values, values[1:]):
        if current - previous <= pd.Timedelta(minutes=1):
            continue
        first, last, count = calendar.expected_minutes(
            previous + pd.Timedelta(minutes=1),
            current - pd.Timedelta(minutes=1),
        )
        if count and first is not None and last is not None:
            result.append(MissingInterval(first, last, count))
    return result
