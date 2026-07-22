"""Broker-neutral live execution state, reconciliation, and safety controls."""

from __future__ import annotations

import json
import os
import threading
from collections.abc import Mapping
from dataclasses import asdict, dataclass, replace
from datetime import time
from enum import Enum
from pathlib import Path
from typing import Any, Protocol

import pandas as pd

from .models import Bar, Direction, NEW_YORK


class AssetClass(str, Enum):
    EQUITY = "equity"
    OPTION = "option"
    FUTURE = "future"


class BrokerOrderStatus(str, Enum):
    NEW = "new"
    ACCEPTED = "accepted"
    PENDING = "pending"
    PARTIALLY_FILLED = "partially_filled"
    FILLED = "filled"
    CANCELED = "canceled"
    REJECTED = "rejected"
    EXPIRED = "expired"
    UNKNOWN = "unknown"

    @property
    def terminal(self) -> bool:
        return self in {
            BrokerOrderStatus.FILLED,
            BrokerOrderStatus.CANCELED,
            BrokerOrderStatus.REJECTED,
            BrokerOrderStatus.EXPIRED,
        }


class ManagedTradeStatus(str, Enum):
    PENDING_ENTRY = "pending_entry"
    OPEN = "open"
    EXITING = "exiting"
    CLOSED = "closed"
    CANCELED = "canceled"
    ERROR = "error"


@dataclass(frozen=True, slots=True)
class BrokerAccount:
    account_id: str
    status: str
    equity: float
    cash: float
    buying_power: float
    options_buying_power: float | None = None
    trading_blocked: bool = False
    account_blocked: bool = False
    options_level: int | None = None


@dataclass(frozen=True, slots=True)
class BrokerOrder:
    order_id: str
    client_order_id: str | None
    symbol: str
    side: str
    quantity: float
    filled_quantity: float
    status: BrokerOrderStatus
    asset_class: AssetClass
    submitted_at: pd.Timestamp | None = None
    filled_at: pd.Timestamp | None = None
    order_type: str = "market"
    limit_price: float | None = None
    stop_price: float | None = None
    parent_order_id: str | None = None
    raw: Any = None


@dataclass(frozen=True, slots=True)
class BrokerPosition:
    symbol: str
    quantity: float
    average_entry_price: float
    market_price: float | None
    market_value: float | None
    unrealized_pl: float | None
    asset_class: AssetClass
    contract_id: int | None = None


@dataclass(frozen=True, slots=True)
class BrokerFill:
    fill_id: str
    order_id: str
    symbol: str
    side: str
    quantity: float
    price: float
    timestamp: pd.Timestamp


@dataclass(frozen=True, slots=True)
class BrokerSnapshot:
    broker: str
    as_of: pd.Timestamp
    account: BrokerAccount
    orders: tuple[BrokerOrder, ...]
    positions: tuple[BrokerPosition, ...]
    fills: tuple[BrokerFill, ...] = ()


@dataclass(frozen=True, slots=True)
class OrderSubmission:
    broker: str
    order_id: str | None
    client_order_id: str
    submitted: bool
    dry_run: bool
    payload: Mapping[str, Any]
    raw: Any = None


@dataclass(frozen=True, slots=True)
class TrackedLeg:
    symbol: str
    expected_quantity: float
    asset_class: AssetClass
    close_side: str
    close_position_intent: str | None = None
    contract_id: int | None = None


@dataclass(frozen=True, slots=True)
class TrackedTrade:
    trade_id: str
    broker: str
    route: str
    instrument: str
    direction: Direction
    entry_order_id: str | None
    client_order_id: str
    legs: tuple[TrackedLeg, ...]
    baseline_positions: Mapping[str, float]
    stop_price: float
    target_price: float
    created_at: pd.Timestamp
    updated_at: pd.Timestamp
    status: ManagedTradeStatus = ManagedTradeStatus.PENDING_ENTRY
    exit_order_id: str | None = None
    exit_reason: str | None = None
    error: str | None = None


@dataclass(frozen=True, slots=True)
class KillSwitchState:
    engaged: bool = False
    reason: str | None = None
    engaged_at: pd.Timestamp | None = None


class ExecutionStore:
    """Thread-safe execution ledger with optional atomic JSON persistence."""

    def __init__(self, path: Path | None = None) -> None:
        self.path = path
        self._lock = threading.RLock()
        self._trades: dict[str, TrackedTrade] = {}
        self._kill_switch = KillSwitchState()
        if path and path.exists():
            self._load()

    @property
    def kill_switch(self) -> KillSwitchState:
        with self._lock:
            return self._kill_switch

    def trades(self) -> tuple[TrackedTrade, ...]:
        with self._lock:
            return tuple(sorted(self._trades.values(), key=lambda item: item.created_at))

    def get(self, trade_id: str) -> TrackedTrade | None:
        with self._lock:
            return self._trades.get(trade_id)

    def record(self, trade: TrackedTrade) -> None:
        with self._lock:
            if trade.trade_id in self._trades:
                raise ValueError(f"trade already tracked: {trade.trade_id}")
            if any(
                item.client_order_id == trade.client_order_id
                for item in self._trades.values()
            ):
                raise ValueError(
                    f"client order already tracked: {trade.client_order_id}"
                )
            self._trades[trade.trade_id] = trade
            self._save()

    def update(self, trade_id: str, **changes: Any) -> TrackedTrade:
        with self._lock:
            current = self._trades.get(trade_id)
            if current is None:
                raise KeyError(trade_id)
            updated = replace(current, **changes)
            self._trades[trade_id] = updated
            self._save()
            return updated

    def engage_kill_switch(self, reason: str, *, as_of: pd.Timestamp) -> None:
        if as_of.tz is None:
            raise ValueError("kill-switch timestamp must be timezone-aware")
        if not reason.strip():
            raise ValueError("kill-switch reason is required")
        with self._lock:
            self._kill_switch = KillSwitchState(True, reason.strip(), as_of)
            self._save()

    def release_kill_switch(self) -> None:
        with self._lock:
            self._kill_switch = KillSwitchState()
            self._save()

    def _save(self) -> None:
        if self.path is None:
            return
        self.path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "version": 1,
            "kill_switch": _json_value(asdict(self._kill_switch)),
            "trades": [_json_value(asdict(trade)) for trade in self._trades.values()],
        }
        temporary = self.path.with_suffix(f"{self.path.suffix}.tmp")
        temporary.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n")
        os.replace(temporary, self.path)

    def _load(self) -> None:
        payload = json.loads(self.path.read_text())
        if payload.get("version") != 1:
            raise ValueError("unsupported execution-store version")
        kill = payload.get("kill_switch", {})
        self._kill_switch = KillSwitchState(
            bool(kill.get("engaged", False)),
            kill.get("reason"),
            _timestamp(kill.get("engaged_at")),
        )
        for item in payload.get("trades", []):
            trade = _tracked_trade_from_dict(item)
            self._trades[trade.trade_id] = trade


@dataclass(frozen=True, slots=True)
class LiveRiskLimits:
    maximum_order_risk: float = 1_000.0
    minimum_equity: float = 1_000.0
    maximum_open_positions: int = 2
    maximum_open_orders: int = 4
    maximum_daily_loss: float | None = None

    def __post_init__(self) -> None:
        if self.maximum_order_risk <= 0 or self.minimum_equity < 0:
            raise ValueError("risk and equity limits are invalid")
        if self.maximum_open_positions < 1 or self.maximum_open_orders < 1:
            raise ValueError("position and order limits must be positive")
        if self.maximum_daily_loss is not None and self.maximum_daily_loss <= 0:
            raise ValueError("maximum daily loss must be positive")


class LiveRiskGate:
    def __init__(
        self,
        store: ExecutionStore,
        limits: LiveRiskLimits | None = None,
    ) -> None:
        self.store = store
        self.limits = limits or LiveRiskLimits()

    def validate(
        self,
        snapshot: BrokerSnapshot,
        *,
        estimated_order_risk: float,
        daily_pl: float | None = None,
    ) -> None:
        if self.store.kill_switch.engaged:
            raise RuntimeError("kill switch is engaged")
        account = snapshot.account
        if account.trading_blocked or account.account_blocked:
            raise RuntimeError("broker account is blocked")
        if account.status.lower() not in {"active", "open"}:
            raise RuntimeError(f"broker account is not active: {account.status}")
        if account.equity < self.limits.minimum_equity:
            raise RuntimeError("account equity is below the configured minimum")
        if estimated_order_risk > self.limits.maximum_order_risk:
            raise RuntimeError("order risk exceeds the configured maximum")
        if estimated_order_risk > account.buying_power:
            raise RuntimeError("order risk exceeds broker buying power")
        open_positions = [position for position in snapshot.positions if position.quantity]
        if len(open_positions) >= self.limits.maximum_open_positions:
            raise RuntimeError("maximum open positions reached")
        open_orders = [order for order in snapshot.orders if not order.status.terminal]
        if len(open_orders) >= self.limits.maximum_open_orders:
            raise RuntimeError("maximum open orders reached")
        if (
            daily_pl is not None
            and self.limits.maximum_daily_loss is not None
            and daily_pl <= -self.limits.maximum_daily_loss
        ):
            raise RuntimeError("daily loss limit reached")


class ReconciliationSeverity(str, Enum):
    INFO = "info"
    WARNING = "warning"
    CRITICAL = "critical"


@dataclass(frozen=True, slots=True)
class ReconciliationIssue:
    kind: str
    severity: ReconciliationSeverity
    trade_id: str | None
    detail: str


@dataclass(frozen=True, slots=True)
class ReconciliationReport:
    broker: str
    as_of: pd.Timestamp
    issues: tuple[ReconciliationIssue, ...]
    updated_trade_ids: tuple[str, ...]

    @property
    def healthy(self) -> bool:
        return not any(
            issue.severity is ReconciliationSeverity.CRITICAL
            for issue in self.issues
        )


class ExecutionReconciler:
    """Compare the persistent strategy ledger with broker-authoritative state."""

    def __init__(
        self,
        store: ExecutionStore,
        *,
        client_order_prefix: str = "sb-",
        exclusive_account: bool = False,
        engage_kill_switch_on_critical: bool = True,
    ) -> None:
        self.store = store
        self.client_order_prefix = client_order_prefix
        self.exclusive_account = exclusive_account
        self.engage_kill_switch_on_critical = engage_kill_switch_on_critical

    def reconcile(self, snapshot: BrokerSnapshot) -> ReconciliationReport:
        order_by_id = {order.order_id: order for order in snapshot.orders}
        order_by_client = {
            order.client_order_id: order
            for order in snapshot.orders
            if order.client_order_id
        }
        actual_positions = {
            position.symbol.upper(): position.quantity
            for position in snapshot.positions
            if position.quantity
        }
        issues: list[ReconciliationIssue] = []
        updated: list[str] = []
        tracked_orders: set[str] = set()
        tracked_symbols: set[str] = set()

        for trade in self.store.trades():
            if trade.broker != snapshot.broker:
                continue
            if trade.entry_order_id:
                tracked_orders.add(trade.entry_order_id)
            order = (
                order_by_id.get(trade.entry_order_id or "")
                or order_by_client.get(trade.client_order_id)
            )
            position_matches = True
            any_position = False
            for leg in trade.legs:
                symbol = leg.symbol.upper()
                tracked_symbols.add(symbol)
                expected = trade.baseline_positions.get(symbol, 0.0)
                if trade.status not in {
                    ManagedTradeStatus.CLOSED,
                    ManagedTradeStatus.CANCELED,
                    ManagedTradeStatus.ERROR,
                }:
                    expected += leg.expected_quantity
                actual = actual_positions.get(symbol, 0.0)
                any_position = any_position or actual != trade.baseline_positions.get(
                    symbol, 0.0
                )
                if not math_isclose(actual, expected):
                    position_matches = False

            changes: dict[str, Any] = {"updated_at": snapshot.as_of}
            if order is None and trade.status is ManagedTradeStatus.PENDING_ENTRY:
                issues.append(
                    ReconciliationIssue(
                        "missing_entry_order",
                        ReconciliationSeverity.CRITICAL,
                        trade.trade_id,
                        "tracked entry order is absent from broker history",
                    )
                )
            elif order is not None:
                tracked_orders.add(order.order_id)
                if order.status is BrokerOrderStatus.FILLED:
                    changes["status"] = ManagedTradeStatus.OPEN
                elif order.status in {
                    BrokerOrderStatus.CANCELED,
                    BrokerOrderStatus.EXPIRED,
                }:
                    changes["status"] = ManagedTradeStatus.CANCELED
                elif order.status is BrokerOrderStatus.REJECTED:
                    changes["status"] = ManagedTradeStatus.ERROR
                    changes["error"] = "entry order rejected by broker"

            effective_status = changes.get("status", trade.status)
            if effective_status is ManagedTradeStatus.OPEN and not position_matches:
                issues.append(
                    ReconciliationIssue(
                        "position_mismatch",
                        ReconciliationSeverity.CRITICAL,
                        trade.trade_id,
                        "broker position does not match the tracked strategy position",
                    )
                )
            if effective_status is ManagedTradeStatus.EXITING and not any_position:
                changes["status"] = ManagedTradeStatus.CLOSED
            if changes != {"updated_at": snapshot.as_of} or trade.updated_at != snapshot.as_of:
                self.store.update(trade.trade_id, **changes)
                updated.append(trade.trade_id)

        for order in snapshot.orders:
            if (
                order.client_order_id
                and order.client_order_id.startswith(self.client_order_prefix)
                and order.order_id not in tracked_orders
                and not order.status.terminal
            ):
                issues.append(
                    ReconciliationIssue(
                        "orphan_strategy_order",
                        ReconciliationSeverity.CRITICAL,
                        None,
                        f"untracked open strategy order {order.order_id}",
                    )
                )
        if self.exclusive_account:
            for symbol, quantity in actual_positions.items():
                if symbol not in tracked_symbols:
                    issues.append(
                        ReconciliationIssue(
                            "orphan_position",
                            ReconciliationSeverity.CRITICAL,
                            None,
                            f"untracked broker position {symbol} quantity {quantity}",
                        )
                    )
        if snapshot.account.trading_blocked or snapshot.account.account_blocked:
            issues.append(
                ReconciliationIssue(
                    "account_blocked",
                    ReconciliationSeverity.CRITICAL,
                    None,
                    "broker reports the account is blocked",
                )
            )
        if (
            self.engage_kill_switch_on_critical
            and any(
                issue.severity is ReconciliationSeverity.CRITICAL
                for issue in issues
            )
            and not self.store.kill_switch.engaged
        ):
            self.store.engage_kill_switch(
                "critical broker reconciliation drift", as_of=snapshot.as_of
            )
        return ReconciliationReport(
            snapshot.broker, snapshot.as_of, tuple(issues), tuple(updated)
        )


class ManagedTradeCloser(Protocol):
    def close_trade(self, trade: TrackedTrade, reason: str) -> OrderSubmission: ...


@dataclass(frozen=True, slots=True)
class SyntheticExitConfig:
    force_close_time: time = time(15, 59)


class SyntheticExitManager:
    """Apply futures stop/target/close rules to proxy positions without brackets."""

    def __init__(
        self,
        store: ExecutionStore,
        brokers: Mapping[str, ManagedTradeCloser],
        config: SyntheticExitConfig | None = None,
    ) -> None:
        self.store = store
        self.brokers = brokers
        self.config = config or SyntheticExitConfig()

    def on_bar(self, bar: Bar) -> tuple[OrderSubmission, ...]:
        results = []
        for trade in self.store.trades():
            if (
                trade.status is not ManagedTradeStatus.OPEN
                or trade.instrument.upper() != bar.instrument.upper()
            ):
                continue
            stop_hit = (
                bar.low <= trade.stop_price
                if trade.direction is Direction.LONG
                else bar.high >= trade.stop_price
            )
            target_hit = (
                bar.high >= trade.target_price
                if trade.direction is Direction.LONG
                else bar.low <= trade.target_price
            )
            local_time = bar.timestamp.tz_convert(NEW_YORK).time()
            reason = None
            if stop_hit:
                reason = "futures_stop"
            elif target_hit:
                reason = "dol_target"
            elif local_time >= self.config.force_close_time:
                reason = "forced_close"
            if reason is None:
                continue
            broker = self.brokers.get(trade.broker)
            if broker is None:
                self.store.update(
                    trade.trade_id,
                    status=ManagedTradeStatus.ERROR,
                    error=f"no close adapter configured for {trade.broker}",
                    updated_at=bar.timestamp,
                )
                continue
            try:
                submission = broker.close_trade(trade, reason)
            except Exception as exc:
                self.store.update(
                    trade.trade_id,
                    status=ManagedTradeStatus.ERROR,
                    error=str(exc),
                    updated_at=bar.timestamp,
                )
                raise
            self.store.update(
                trade.trade_id,
                status=ManagedTradeStatus.EXITING,
                exit_order_id=submission.order_id,
                exit_reason=reason,
                updated_at=bar.timestamp,
            )
            results.append(submission)
        return tuple(results)


def _json_value(value: Any) -> Any:
    if isinstance(value, dict):
        return {str(key): _json_value(item) for key, item in value.items()}
    if isinstance(value, (tuple, list)):
        return [_json_value(item) for item in value]
    if isinstance(value, Enum):
        return value.value
    if isinstance(value, pd.Timestamp):
        return value.isoformat()
    return value


def _tracked_trade_from_dict(item: Mapping[str, Any]) -> TrackedTrade:
    legs = tuple(
        TrackedLeg(
            symbol=str(leg["symbol"]),
            expected_quantity=float(leg["expected_quantity"]),
            asset_class=AssetClass(leg["asset_class"]),
            close_side=str(leg["close_side"]),
            close_position_intent=leg.get("close_position_intent"),
            contract_id=(
                None if leg.get("contract_id") is None else int(leg["contract_id"])
            ),
        )
        for leg in item["legs"]
    )
    return TrackedTrade(
        trade_id=str(item["trade_id"]),
        broker=str(item["broker"]),
        route=str(item["route"]),
        instrument=str(item["instrument"]),
        direction=Direction(item["direction"]),
        entry_order_id=item.get("entry_order_id"),
        client_order_id=str(item["client_order_id"]),
        legs=legs,
        baseline_positions={
            str(key): float(value)
            for key, value in item.get("baseline_positions", {}).items()
        },
        stop_price=float(item["stop_price"]),
        target_price=float(item["target_price"]),
        created_at=_required_timestamp(item.get("created_at")),
        updated_at=_required_timestamp(item.get("updated_at")),
        status=ManagedTradeStatus(item.get("status", "pending_entry")),
        exit_order_id=item.get("exit_order_id"),
        exit_reason=item.get("exit_reason"),
        error=item.get("error"),
    )


def _timestamp(value: Any) -> pd.Timestamp | None:
    return None if value is None else pd.Timestamp(value)


def _required_timestamp(value: Any) -> pd.Timestamp:
    timestamp = _timestamp(value)
    if timestamp is None or timestamp.tz is None:
        raise ValueError("tracked trade timestamp is missing or timezone-naive")
    return timestamp


def math_isclose(left: float, right: float) -> bool:
    return abs(left - right) <= 1e-9
