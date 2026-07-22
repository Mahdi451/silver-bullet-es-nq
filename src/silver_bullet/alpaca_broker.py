"""Account-aware Alpaca execution for selected option candidates."""

from __future__ import annotations

import hashlib
import os
from dataclasses import dataclass
from typing import Any, Mapping

import pandas as pd

from .broker import (
    AssetClass,
    BrokerAccount,
    BrokerFill,
    BrokerOrder,
    BrokerOrderStatus,
    BrokerPosition,
    BrokerSnapshot,
    ExecutionReconciler,
    ExecutionStore,
    LiveRiskGate,
    ManagedTradeStatus,
    OrderSubmission,
    TrackedLeg,
    TrackedTrade,
)
from .options import OptionOrderCandidate
from .transport import ExternalServiceError, JsonTransport, UrllibJsonTransport


@dataclass(frozen=True, slots=True)
class AlpacaBrokerConfig:
    api_key: str
    secret_key: str
    paper: bool = True
    enabled: bool = False
    allow_live: bool = False
    base_url: str | None = None

    @property
    def resolved_base_url(self) -> str:
        if self.base_url:
            return self.base_url.rstrip("/")
        return (
            "https://paper-api.alpaca.markets"
            if self.paper
            else "https://api.alpaca.markets"
        )

    @classmethod
    def from_environment(
        cls,
        *,
        enabled: bool = False,
        allow_live: bool = False,
    ) -> "AlpacaBrokerConfig":
        api_key = os.environ.get("ALPACA_API_KEY")
        secret_key = os.environ.get("ALPACA_SECRET_KEY")
        if not api_key or not secret_key:
            raise RuntimeError("ALPACA_API_KEY and ALPACA_SECRET_KEY are required")
        paper = os.environ.get("ALPACA_PAPER", "true").lower() != "false"
        return cls(api_key, secret_key, paper, enabled, allow_live)


class AlpacaBroker:
    name = "alpaca"

    def __init__(
        self,
        config: AlpacaBrokerConfig,
        *,
        store: ExecutionStore | None = None,
        transport: JsonTransport | None = None,
        clock: Any = None,
    ) -> None:
        if not config.api_key or not config.secret_key:
            raise ValueError("Alpaca API credentials are required")
        self.config = config
        self.store = store or ExecutionStore()
        self.transport = transport or UrllibJsonTransport()
        self._clock = clock or (lambda: pd.Timestamp.now(tz="UTC"))

    def snapshot(
        self,
        *,
        as_of: pd.Timestamp | None = None,
        activities_after: pd.Timestamp | None = None,
    ) -> BrokerSnapshot:
        timestamp = as_of or self._clock()
        if timestamp.tz is None:
            raise ValueError("snapshot time must be timezone-aware")
        account_payload = self._request("GET", "/v2/account")
        orders_payload = self._request(
            "GET",
            "/v2/orders",
            params={"status": "all", "limit": 500, "nested": True},
        )
        positions_payload = self._request("GET", "/v2/positions")
        fill_params: dict[str, Any] = {"direction": "asc", "page_size": 100}
        if activities_after is not None:
            if activities_after.tz is None:
                raise ValueError("activities_after must be timezone-aware")
            fill_params["after"] = activities_after.isoformat()
        fills_payload = self._request(
            "GET", "/v2/account/activities/FILL", params=fill_params
        )
        return BrokerSnapshot(
            self.name,
            timestamp,
            _parse_account(account_payload),
            tuple(_parse_order(item) for item in orders_payload or []),
            tuple(_parse_position(item) for item in positions_payload or []),
            tuple(_parse_fill(item) for item in fills_payload or []),
        )

    def preview_option_order(
        self,
        candidate: OptionOrderCandidate,
    ) -> OrderSubmission:
        client_order_id = _option_client_order_id(candidate, closing=False)
        return OrderSubmission(
            self.name,
            None,
            client_order_id,
            submitted=False,
            dry_run=True,
            payload=_candidate_payload(candidate, client_order_id),
        )

    def submit_option(
        self,
        candidate: OptionOrderCandidate,
        *,
        snapshot: BrokerSnapshot,
        risk_gate: LiveRiskGate,
        dry_run: bool = True,
        daily_pl: float | None = None,
    ) -> OrderSubmission:
        preview = self.preview_option_order(candidate)
        risk_gate.validate(
            snapshot,
            estimated_order_risk=candidate.estimated_max_loss,
            daily_pl=daily_pl,
        )
        if (
            snapshot.account.options_buying_power is not None
            and candidate.estimated_max_loss
            > snapshot.account.options_buying_power
        ):
            raise RuntimeError("order risk exceeds Alpaca options buying power")
        required_level = 3 if len(candidate.legs) > 1 else 2
        level = snapshot.account.options_level
        if level is not None and level < required_level:
            raise RuntimeError(
                f"Alpaca options level {required_level} is required for this order"
            )
        if self.store.get(preview.client_order_id) is not None:
            raise RuntimeError("this option candidate is already tracked")
        if dry_run:
            return preview
        self._validate_submission_enabled()
        payload = dict(preview.payload)
        raw = self._request("POST", "/v2/orders", body=payload)
        order_id = _response_order_id(raw)
        trade = _tracked_option_trade(
            candidate,
            preview.client_order_id,
            order_id,
            snapshot,
            as_of=self._clock(),
        )
        self.store.record(trade)
        return OrderSubmission(
            self.name,
            order_id,
            preview.client_order_id,
            submitted=True,
            dry_run=False,
            payload=payload,
            raw=raw,
        )

    def close_trade(self, trade: TrackedTrade, reason: str) -> OrderSubmission:
        if trade.broker != self.name:
            raise ValueError("tracked trade belongs to a different broker")
        if trade.status not in {
            ManagedTradeStatus.OPEN,
            ManagedTradeStatus.EXITING,
        }:
            raise RuntimeError("only open trades can be closed")
        self._validate_submission_enabled()
        client_order_id = _close_client_order_id(trade, reason)
        payload = _close_payload(trade, client_order_id)
        raw = self._request("POST", "/v2/orders", body=payload)
        order_id = _response_order_id(raw)
        return OrderSubmission(
            self.name,
            order_id,
            client_order_id,
            submitted=True,
            dry_run=False,
            payload=payload,
            raw=raw,
        )

    def cancel_order(self, order_id: str) -> None:
        self._validate_submission_enabled()
        self._request("DELETE", f"/v2/orders/{order_id}")

    def cancel_all_orders(self) -> Any:
        self._validate_submission_enabled()
        return self._request("DELETE", "/v2/orders")

    def close_all_positions(self, *, cancel_orders: bool = True) -> Any:
        self._validate_submission_enabled()
        return self._request(
            "DELETE",
            "/v2/positions",
            params={"cancel_orders": cancel_orders},
        )

    def reconcile(
        self,
        reconciler: ExecutionReconciler | None = None,
        *,
        as_of: pd.Timestamp | None = None,
    ):
        worker = reconciler or ExecutionReconciler(self.store)
        return worker.reconcile(self.snapshot(as_of=as_of))

    def engage_kill_switch(
        self,
        reason: str,
        *,
        flatten: bool = False,
        as_of: pd.Timestamp | None = None,
    ) -> None:
        timestamp = as_of or self._clock()
        self.store.engage_kill_switch(reason, as_of=timestamp)
        if self.config.enabled:
            self.cancel_all_orders()
            if flatten:
                self.close_all_positions(cancel_orders=True)

    def _validate_submission_enabled(self) -> None:
        if not self.config.enabled:
            raise RuntimeError("Alpaca broker submission is disabled")
        if not self.config.paper and not self.config.allow_live:
            raise RuntimeError("live Alpaca submission requires allow_live=True")

    def _request(
        self,
        method: str,
        path: str,
        *,
        params: Mapping[str, Any] | None = None,
        body: Mapping[str, Any] | None = None,
    ) -> Any:
        return self.transport.request(
            method,
            f"{self.config.resolved_base_url}{path}",
            headers={
                "APCA-API-KEY-ID": self.config.api_key,
                "APCA-API-SECRET-KEY": self.config.secret_key,
            },
            params=params,
            body=body,
        )


def _candidate_payload(
    candidate: OptionOrderCandidate, client_order_id: str
) -> dict[str, Any]:
    common: dict[str, Any] = {
        "qty": str(candidate.quantity),
        "type": "limit",
        "limit_price": _price_string(candidate.limit_price),
        "time_in_force": "day",
        "client_order_id": client_order_id,
    }
    if len(candidate.legs) == 1:
        leg = candidate.legs[0]
        return {
            **common,
            "symbol": leg.contract.symbol,
            "side": leg.side,
            "position_intent": leg.position_intent,
        }
    return {
        **common,
        "order_class": "mleg",
        "legs": [
            {
                "symbol": leg.contract.symbol,
                "ratio_qty": str(leg.ratio_quantity),
                "side": leg.side,
                "position_intent": leg.position_intent,
            }
            for leg in candidate.legs
        ],
    }


def _close_payload(trade: TrackedTrade, client_order_id: str) -> dict[str, Any]:
    common: dict[str, Any] = {
        "qty": "1",
        "type": "market",
        "time_in_force": "day",
        "client_order_id": client_order_id,
    }
    quantities = {abs(leg.expected_quantity) for leg in trade.legs}
    if len(quantities) != 1:
        raise RuntimeError("tracked option legs have incompatible quantities")
    quantity = quantities.pop()
    if not float(quantity).is_integer():
        raise RuntimeError("option exit quantity must be a whole number")
    common["qty"] = str(int(quantity))
    if len(trade.legs) == 1:
        leg = trade.legs[0]
        return {
            **common,
            "symbol": leg.symbol,
            "side": leg.close_side,
            "position_intent": leg.close_position_intent,
        }
    return {
        **common,
        "order_class": "mleg",
        "legs": [
            {
                "symbol": leg.symbol,
                "ratio_qty": "1",
                "side": leg.close_side,
                "position_intent": leg.close_position_intent,
            }
            for leg in trade.legs
        ],
    }


def _tracked_option_trade(
    candidate: OptionOrderCandidate,
    client_order_id: str,
    order_id: str,
    snapshot: BrokerSnapshot,
    *,
    as_of: pd.Timestamp,
) -> TrackedTrade:
    positions = {position.symbol.upper(): position.quantity for position in snapshot.positions}
    legs = []
    for leg in candidate.legs:
        signed_quantity = candidate.quantity * leg.ratio_quantity
        if leg.side == "sell":
            signed_quantity *= -1
        legs.append(
            TrackedLeg(
                leg.contract.symbol,
                signed_quantity,
                AssetClass.OPTION,
                "sell" if leg.side == "buy" else "buy",
                "sell_to_close" if leg.side == "buy" else "buy_to_close",
            )
        )
    return TrackedTrade(
        trade_id=client_order_id,
        broker="alpaca",
        route="options",
        instrument=candidate.signal.instrument,
        direction=candidate.signal.direction,
        entry_order_id=order_id,
        client_order_id=client_order_id,
        legs=tuple(legs),
        baseline_positions={
            leg.symbol.upper(): positions.get(leg.symbol.upper(), 0.0) for leg in legs
        },
        stop_price=candidate.signal.proposed_stop,
        target_price=candidate.signal.target.price,
        created_at=as_of,
        updated_at=as_of,
    )


def _option_client_order_id(
    candidate: OptionOrderCandidate, *, closing: bool
) -> str:
    signal = candidate.signal
    raw = "|".join(
        [
            signal.instrument,
            signal.session,
            signal.direction.value,
            signal.signal_time.isoformat(),
            candidate.profile.value,
            *[leg.contract.symbol for leg in candidate.legs],
        ]
    )
    digest = hashlib.sha256(raw.encode()).hexdigest()[:20]
    action = "exit" if closing else "opt"
    return f"sb-{action}-{digest}"


def _close_client_order_id(trade: TrackedTrade, reason: str) -> str:
    digest = hashlib.sha256(f"{trade.trade_id}|{reason}".encode()).hexdigest()[:20]
    return f"sb-exit-{digest}"


def _response_order_id(payload: Any) -> str:
    if not isinstance(payload, dict):
        raise ExternalServiceError("Alpaca returned an invalid order response")
    if payload.get("code") or payload.get("message") and not payload.get("id"):
        raise ExternalServiceError(str(payload.get("message") or payload))
    order_id = payload.get("id")
    if not order_id:
        raise ExternalServiceError("Alpaca order response did not include an id")
    return str(order_id)


def _parse_account(item: Mapping[str, Any]) -> BrokerAccount:
    options_level = item.get("options_trading_level")
    equity = float(item.get("equity") or 0)
    last_equity = _optional_float(item.get("last_equity"))
    return BrokerAccount(
        account_id=str(item.get("id") or item.get("account_number") or ""),
        status=str(item.get("status") or "unknown"),
        equity=equity,
        cash=float(item.get("cash") or 0),
        buying_power=float(item.get("buying_power") or 0),
        options_buying_power=_optional_float(item.get("options_buying_power")),
        trading_blocked=bool(item.get("trading_blocked", False)),
        account_blocked=bool(item.get("account_blocked", False)),
        options_level=None if options_level is None else int(options_level),
        daily_pl=None if last_equity is None else equity - last_equity,
    )


def _parse_order(item: Mapping[str, Any]) -> BrokerOrder:
    symbol = str(item.get("symbol") or item.get("underlying_symbol") or "")
    return BrokerOrder(
        order_id=str(item.get("id") or ""),
        client_order_id=item.get("client_order_id"),
        symbol=symbol,
        side=str(item.get("side") or "multi"),
        quantity=float(item.get("qty") or 0),
        filled_quantity=float(item.get("filled_qty") or 0),
        status=_alpaca_order_status(str(item.get("status") or "unknown")),
        asset_class=_asset_class(item.get("asset_class"), symbol),
        submitted_at=_optional_timestamp(item.get("submitted_at")),
        filled_at=_optional_timestamp(item.get("filled_at")),
        order_type=str(item.get("type") or "market"),
        limit_price=_optional_float(item.get("limit_price")),
        stop_price=_optional_float(item.get("stop_price")),
        parent_order_id=item.get("parent_order_id"),
        raw=item,
    )


def _parse_position(item: Mapping[str, Any]) -> BrokerPosition:
    symbol = str(item.get("symbol") or "")
    return BrokerPosition(
        symbol=symbol,
        quantity=float(item.get("qty") or 0),
        average_entry_price=float(item.get("avg_entry_price") or 0),
        market_price=_optional_float(item.get("current_price")),
        market_value=_optional_float(item.get("market_value")),
        unrealized_pl=_optional_float(item.get("unrealized_pl")),
        asset_class=_asset_class(item.get("asset_class"), symbol),
    )


def _parse_fill(item: Mapping[str, Any]) -> BrokerFill:
    timestamp = _optional_timestamp(item.get("transaction_time"))
    if timestamp is None:
        raise ValueError("Alpaca fill is missing transaction_time")
    return BrokerFill(
        fill_id=str(item.get("id") or ""),
        order_id=str(item.get("order_id") or ""),
        symbol=str(item.get("symbol") or ""),
        side=str(item.get("side") or ""),
        quantity=float(item.get("qty") or 0),
        price=float(item.get("price") or 0),
        timestamp=timestamp,
    )


def _asset_class(raw: Any, symbol: str) -> AssetClass:
    value = str(raw or "").lower()
    if value in {"us_option", "option", "options"}:
        return AssetClass.OPTION
    if value in {"future", "futures"}:
        return AssetClass.FUTURE
    if len(symbol) >= 16 and symbol[-9:-8] in {"C", "P"}:
        return AssetClass.OPTION
    return AssetClass.EQUITY


def _alpaca_order_status(value: str) -> BrokerOrderStatus:
    normalized = value.lower()
    mapping = {
        "new": BrokerOrderStatus.NEW,
        "accepted": BrokerOrderStatus.ACCEPTED,
        "pending_new": BrokerOrderStatus.PENDING,
        "pending_cancel": BrokerOrderStatus.PENDING,
        "pending_replace": BrokerOrderStatus.PENDING,
        "partially_filled": BrokerOrderStatus.PARTIALLY_FILLED,
        "filled": BrokerOrderStatus.FILLED,
        "canceled": BrokerOrderStatus.CANCELED,
        "expired": BrokerOrderStatus.EXPIRED,
        "rejected": BrokerOrderStatus.REJECTED,
        "replaced": BrokerOrderStatus.CANCELED,
        "done_for_day": BrokerOrderStatus.EXPIRED,
    }
    return mapping.get(normalized, BrokerOrderStatus.UNKNOWN)


def _optional_float(value: Any) -> float | None:
    return None if value is None else float(value)


def _optional_timestamp(value: Any) -> pd.Timestamp | None:
    if value is None:
        return None
    timestamp = pd.Timestamp(value)
    if timestamp.tz is None:
        timestamp = timestamp.tz_localize("UTC")
    return timestamp


def _price_string(value: float) -> str:
    return f"{value:.2f}"
