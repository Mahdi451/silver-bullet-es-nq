"""Deterministic event-driven fills, positions, costs, and account state."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import time, timedelta
from enum import Enum
from heapq import merge
from typing import Iterable, Mapping

import pandas as pd

from .models import Bar, Direction, INSTRUMENTS, TradeSignal
from .strategy import SilverBulletStrategy


class Side(str, Enum):
    BUY = "buy"
    SELL = "sell"


class FillReason(str, Enum):
    ENTRY = "entry"
    SCALE_OUT = "scale_out"
    DOL_TARGET = "dol_target"
    STOP = "stop"
    FORCED_CLOSE = "forced_close"
    END_OF_DATA = "end_of_data"


@dataclass(frozen=True, slots=True)
class CostModel:
    fee_per_side: Mapping[str, float]
    slippage_ticks: int = 1

    def validate(self, instruments: Iterable[str]) -> None:
        if self.slippage_ticks < 0:
            raise ValueError("slippage_ticks cannot be negative")
        missing = [symbol for symbol in instruments if symbol not in self.fee_per_side]
        if missing:
            raise ValueError(f"missing per-side fees for: {missing}")
        if any(self.fee_per_side[symbol] < 0 for symbol in instruments):
            raise ValueError("fees cannot be negative")


@dataclass(frozen=True, slots=True)
class RiskLimits:
    daily_loss_limit: float | None = None
    overall_drawdown_limit: float | None = None
    trailing: bool = True
    enforcement: str = "report_only"
    reset_time: time = time(17)
    basis: str = "equity"

    def __post_init__(self) -> None:
        if self.enforcement not in {"report_only", "block_entries"}:
            raise ValueError("enforcement must be report_only or block_entries")
        if self.basis not in {"equity", "balance"}:
            raise ValueError("basis must be equity or balance")
        for value in (self.daily_loss_limit, self.overall_drawdown_limit):
            if value is not None and value <= 0:
                raise ValueError("drawdown limits must be positive")


@dataclass(frozen=True, slots=True)
class BacktestConfig:
    starting_balance: float
    cost_model: CostModel
    quantities: Mapping[str, int] = field(default_factory=lambda: {"ES": 2, "NQ": 2})
    scale_out_points: float = 5.0
    force_close_time: time = time(15, 59)
    risk_limits: RiskLimits = field(default_factory=RiskLimits)

    def __post_init__(self) -> None:
        if self.starting_balance <= 0:
            raise ValueError("starting_balance must be positive")
        if self.scale_out_points <= 0:
            raise ValueError("scale_out_points must be positive")
        if any(quantity < 1 for quantity in self.quantities.values()):
            raise ValueError("quantities must be positive")


@dataclass(frozen=True, slots=True)
class Fill:
    timestamp: pd.Timestamp
    instrument: str
    side: Side
    quantity: int
    nominal_price: float
    price: float
    fee: float
    reason: FillReason
    contract: str | None = None


@dataclass(frozen=True, slots=True)
class Trade:
    instrument: str
    contract: str | None
    session: str
    direction: Direction
    entry: Fill
    exits: tuple[Fill, ...]
    stop_price: float
    target_price: float
    gross_pnl: float
    fees: float
    net_pnl: float
    risk_points: float
    r_multiple: float
    exit_reason: FillReason


@dataclass(frozen=True, slots=True)
class EquityPoint:
    timestamp: pd.Timestamp
    balance: float
    equity: float


@dataclass(frozen=True, slots=True)
class RiskViolation:
    timestamp: pd.Timestamp
    rule: str
    drawdown: float
    limit: float


@dataclass(slots=True)
class BacktestResult:
    config: BacktestConfig
    fills: list[Fill]
    trades: list[Trade]
    equity: list[EquityPoint]
    violations: list[RiskViolation]
    cancellations: list[tuple[pd.Timestamp, str, str]]
    ending_balance: float
    ending_equity: float


@dataclass(slots=True)
class _Position:
    signal: TradeSignal
    entry: Fill
    initial_quantity: int
    remaining: int
    partial_quantity: int
    partial_target: float
    exits: list[Fill] = field(default_factory=list)
    gross_pnl: float = 0.0
    exit_fees: float = 0.0


class BacktestEngine:
    """Run strategy and execution events with conservative OHLC assumptions."""

    def __init__(self, config: BacktestConfig) -> None:
        self.config = config

    def run_sources(
        self,
        sources: Mapping[str, Iterable[Bar]],
        strategies: Mapping[str, SilverBulletStrategy],
    ) -> BacktestResult:
        streams = [iter(source) for source in sources.values()]
        bars = merge(*streams, key=lambda bar: (bar.timestamp, bar.instrument))
        return self.run(bars, strategies)

    def run(
        self,
        bars: Iterable[Bar],
        strategies: Mapping[str, SilverBulletStrategy],
    ) -> BacktestResult:
        instruments = tuple(strategies)
        self.config.cost_model.validate(instruments)
        fills: list[Fill] = []
        trades: list[Trade] = []
        equity_points: list[EquityPoint] = []
        violations: list[RiskViolation] = []
        cancellations: list[tuple[pd.Timestamp, str, str]] = []
        positions: dict[str, _Position] = {}
        pending: dict[str, TradeSignal] = {}
        last_bars: dict[str, Bar] = {}
        balance = self.config.starting_balance
        daily_date = None
        daily_start = balance
        overall_peak = balance
        blocked_dates: set[object] = set()
        reported: set[tuple[object, str]] = set()

        def mark_equity(timestamp: pd.Timestamp) -> float:
            nonlocal overall_peak, daily_date, daily_start
            equity = balance
            for symbol, position in positions.items():
                if symbol not in last_bars:
                    continue
                close = last_bars[symbol].close
                multiplier = INSTRUMENTS[symbol].point_value
                delta = (
                    close - position.entry.price
                    if position.signal.direction is Direction.LONG
                    else position.entry.price - close
                )
                equity += delta * multiplier * position.remaining
            local = timestamp.tz_convert("America/New_York")
            local_date = local.date() + (
                timedelta(days=1)
                if local.time() >= self.config.risk_limits.reset_time
                else timedelta()
            )
            risk_value = (
                equity if self.config.risk_limits.basis == "equity" else balance
            )
            if daily_date != local_date:
                daily_date, daily_start = local_date, risk_value
            overall_peak = max(overall_peak, risk_value)
            limits = self.config.risk_limits
            checks = []
            if limits.daily_loss_limit is not None:
                checks.append(
                    ("daily_loss", daily_start - risk_value, limits.daily_loss_limit)
                )
            if limits.overall_drawdown_limit is not None:
                reference = (
                    overall_peak if limits.trailing else self.config.starting_balance
                )
                checks.append(
                    (
                        "overall_drawdown",
                        reference - risk_value,
                        limits.overall_drawdown_limit,
                    )
                )
            for rule, drawdown, limit in checks:
                key = (local_date, rule)
                if drawdown >= limit and key not in reported:
                    violations.append(RiskViolation(timestamp, rule, drawdown, limit))
                    reported.add(key)
                    if limits.enforcement == "block_entries":
                        blocked_dates.add(local_date)
            equity_points.append(EquityPoint(timestamp, balance, equity))
            return equity

        previous_timestamp: pd.Timestamp | None = None
        for bar in bars:
            if previous_timestamp is not None and bar.timestamp < previous_timestamp:
                raise ValueError("combined bars must be in chronological order")
            previous_timestamp = bar.timestamp
            symbol = bar.instrument.upper()
            if symbol not in strategies:
                raise ValueError(f"no strategy configured for {symbol}")
            last_bars[symbol] = bar

            signal = pending.get(symbol)
            if signal is not None:
                if bar.timestamp > signal.execution_time:
                    cancellations.append(
                        (bar.timestamp, symbol, "missing_execution_bar")
                    )
                    pending.pop(symbol)
                elif bar.timestamp == signal.execution_time:
                    local_date = bar.timestamp.tz_convert("America/New_York").date()
                    if local_date in blocked_dates:
                        cancellations.append((bar.timestamp, symbol, "risk_limit"))
                    elif symbol in positions:
                        cancellations.append(
                            (bar.timestamp, symbol, "position_already_open")
                        )
                    else:
                        position, entry_fee = self._enter(signal, bar)
                        if position is None:
                            cancellations.append(
                                (bar.timestamp, symbol, "invalid_entry_open")
                            )
                        else:
                            positions[symbol] = position
                            fills.append(position.entry)
                            balance -= entry_fee
                    pending.pop(symbol)

            if symbol in positions:
                position = positions[symbol]
                exit_fills = self._manage(position, bar)
                for fill in exit_fills:
                    fills.append(fill)
                    gross = self._fill_pnl(position, fill)
                    position.gross_pnl += gross
                    position.exit_fees += fill.fee
                    balance += gross - fill.fee
                    position.remaining -= fill.quantity
                if position.remaining == 0:
                    trades.append(self._close_trade(position))
                    del positions[symbol]

            generated = strategies[symbol].on_bar(bar)
            if generated is not None:
                pending[symbol] = generated
            mark_equity(bar.timestamp)

        for symbol, position in tuple(positions.items()):
            bar = last_bars[symbol]
            fill = self._make_fill(
                bar.timestamp,
                symbol,
                self._exit_side(position.signal.direction),
                position.remaining,
                bar.close,
                FillReason.END_OF_DATA,
                bar.contract,
            )
            fills.append(fill)
            gross = self._fill_pnl(position, fill)
            position.gross_pnl += gross
            position.exit_fees += fill.fee
            balance += gross - fill.fee
            position.remaining = 0
            position.exits.append(fill)
            trades.append(self._close_trade(position))
            del positions[symbol]
        for symbol, signal in pending.items():
            cancellations.append((signal.execution_time, symbol, "end_of_data"))

        ending_equity = balance
        if last_bars:
            ending_time = max(bar.timestamp for bar in last_bars.values())
            equity_points.append(EquityPoint(ending_time, balance, balance))
        return BacktestResult(
            self.config,
            fills,
            trades,
            equity_points,
            violations,
            cancellations,
            balance,
            ending_equity,
        )

    def _enter(self, signal: TradeSignal, bar: Bar) -> tuple[_Position | None, float]:
        if signal.contract and bar.contract and signal.contract != bar.contract:
            return None, 0.0
        quantity = self.config.quantities.get(signal.instrument, 2)
        side = Side.BUY if signal.direction is Direction.LONG else Side.SELL
        entry = self._make_fill(
            bar.timestamp,
            signal.instrument,
            side,
            quantity,
            bar.open,
            FillReason.ENTRY,
            bar.contract,
        )
        favorable = (
            signal.target.price - entry.price
            if signal.direction is Direction.LONG
            else entry.price - signal.target.price
        )
        stop_valid = (
            entry.price > signal.proposed_stop
            if signal.direction is Direction.LONG
            else entry.price < signal.proposed_stop
        )
        if favorable < self.config.scale_out_points or not stop_valid:
            return None, 0.0
        partial_target = entry.price + (
            self.config.scale_out_points
            if signal.direction is Direction.LONG
            else -self.config.scale_out_points
        )
        return (
            _Position(
                signal,
                entry,
                quantity,
                quantity,
                quantity // 2 if quantity >= 2 else 0,
                partial_target,
            ),
            entry.fee,
        )

    def _manage(self, position: _Position, bar: Bar) -> list[Fill]:
        direction = position.signal.direction
        stop = position.signal.proposed_stop
        stop_hit = bar.low <= stop if direction is Direction.LONG else bar.high >= stop
        if stop_hit:
            nominal = (
                min(stop, bar.open)
                if direction is Direction.LONG
                else max(stop, bar.open)
            )
            fill = self._make_fill(
                bar.timestamp,
                bar.instrument,
                self._exit_side(direction),
                position.remaining,
                nominal,
                FillReason.STOP,
                bar.contract,
            )
            position.exits.append(fill)
            return [fill]

        result: list[Fill] = []
        partial_hit = (
            bar.high >= position.partial_target
            if direction is Direction.LONG
            else bar.low <= position.partial_target
        )
        if (
            position.partial_quantity
            and not any(fill.reason is FillReason.SCALE_OUT for fill in position.exits)
            and partial_hit
        ):
            fill = self._make_fill(
                bar.timestamp,
                bar.instrument,
                self._exit_side(direction),
                position.partial_quantity,
                position.partial_target,
                FillReason.SCALE_OUT,
                bar.contract,
            )
            position.exits.append(fill)
            result.append(fill)

        remaining_after_partial = position.remaining - sum(
            fill.quantity for fill in result
        )
        target = position.signal.target.price
        target_hit = (
            bar.high >= target if direction is Direction.LONG else bar.low <= target
        )
        if remaining_after_partial and target_hit:
            fill = self._make_fill(
                bar.timestamp,
                bar.instrument,
                self._exit_side(direction),
                remaining_after_partial,
                target,
                FillReason.DOL_TARGET,
                bar.contract,
            )
            position.exits.append(fill)
            result.append(fill)
            return result

        local_time = bar.timestamp.tz_convert("America/New_York").time()
        if remaining_after_partial and local_time >= self.config.force_close_time:
            fill = self._make_fill(
                bar.timestamp,
                bar.instrument,
                self._exit_side(direction),
                remaining_after_partial,
                bar.close,
                FillReason.FORCED_CLOSE,
                bar.contract,
            )
            position.exits.append(fill)
            result.append(fill)
        return result

    def _make_fill(
        self,
        timestamp: pd.Timestamp,
        instrument: str,
        side: Side,
        quantity: int,
        nominal: float,
        reason: FillReason,
        contract: str | None,
    ) -> Fill:
        tick = INSTRUMENTS[instrument].tick_size
        slip = tick * self.config.cost_model.slippage_ticks
        price = nominal + slip if side is Side.BUY else nominal - slip
        fee = self.config.cost_model.fee_per_side[instrument] * quantity
        return Fill(
            timestamp, instrument, side, quantity, nominal, price, fee, reason, contract
        )

    @staticmethod
    def _exit_side(direction: Direction) -> Side:
        return Side.SELL if direction is Direction.LONG else Side.BUY

    @staticmethod
    def _fill_pnl(position: _Position, fill: Fill) -> float:
        multiplier = INSTRUMENTS[position.signal.instrument].point_value
        points = (
            fill.price - position.entry.price
            if position.signal.direction is Direction.LONG
            else position.entry.price - fill.price
        )
        return points * multiplier * fill.quantity

    @staticmethod
    def _close_trade(position: _Position) -> Trade:
        fees = position.entry.fee + position.exit_fees
        net = position.gross_pnl - fees
        risk_points = abs(position.entry.price - position.signal.proposed_stop)
        risk_dollars = (
            risk_points
            * INSTRUMENTS[position.signal.instrument].point_value
            * position.initial_quantity
        )
        return Trade(
            position.signal.instrument,
            position.entry.contract,
            position.signal.session,
            position.signal.direction,
            position.entry,
            tuple(position.exits),
            position.signal.proposed_stop,
            position.signal.target.price,
            position.gross_pnl,
            fees,
            net,
            risk_points,
            net / risk_dollars if risk_dollars else 0.0,
            position.exits[-1].reason,
        )
