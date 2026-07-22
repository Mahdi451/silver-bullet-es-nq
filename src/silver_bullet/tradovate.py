"""Guarded Tradovate futures routing with native OSO/OCO protection."""

from __future__ import annotations

import hashlib
import os
from dataclasses import dataclass, field
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
from .models import Direction, INSTRUMENTS, TradeSignal
from .transport import ExternalServiceError, JsonTransport, UrllibJsonTransport


@dataclass(frozen=True, slots=True)
class TradovateConfig:
    account_id: int
    account_spec: str
    contracts: Mapping[str, str]
    quantities: Mapping[str, int] = field(
        default_factory=lambda: {"ES": 1, "NQ": 1}
    )
    contract_ids: Mapping[str, int] = field(default_factory=dict)
    demo: bool = True
    enabled: bool = False
    allow_live: bool = False
    maximum_signal_age_minutes: float = 2.0
    maximum_quantity: int = 2
    access_token: str | None = None
    username: str | None = None
    password: str | None = None
    app_id: str | None = None
    app_version: str = "1.0"
    client_id: str | None = None
    api_secret: str | None = None
    device_id: str | None = None
    base_url: str | None = None

    def __post_init__(self) -> None:
        if self.account_id <= 0 or not self.account_spec:
            raise ValueError("Tradovate account id and account spec are required")
        normalized = {symbol.upper() for symbol in self.contracts}
        if not normalized or not normalized <= {"ES", "NQ"}:
            raise ValueError("Tradovate contracts must map ES and/or NQ")
        if any(not contract for contract in self.contracts.values()):
            raise ValueError("Tradovate contract symbols cannot be empty")
        if any(quantity <= 0 for quantity in self.quantities.values()):
            raise ValueError("Tradovate quantities must be positive")
        if self.maximum_quantity < 1:
            raise ValueError("maximum futures quantity must be positive")
        if self.maximum_signal_age_minutes < 0:
            raise ValueError("maximum signal age cannot be negative")

    @property
    def resolved_base_url(self) -> str:
        if self.base_url:
            return self.base_url.rstrip("/")
        return (
            "https://demo.tradovateapi.com/v1"
            if self.demo
            else "https://live.tradovateapi.com/v1"
        )

    @classmethod
    def from_environment(
        cls,
        *,
        contracts: Mapping[str, str] | None = None,
        enabled: bool = False,
        allow_live: bool = False,
    ) -> "TradovateConfig":
        account_id = os.environ.get("TRADOVATE_ACCOUNT_ID")
        account_spec = os.environ.get("TRADOVATE_ACCOUNT_SPEC")
        if not account_id or not account_spec:
            raise RuntimeError(
                "TRADOVATE_ACCOUNT_ID and TRADOVATE_ACCOUNT_SPEC are required"
            )
        mapped = dict(contracts or {})
        for symbol in ("ES", "NQ"):
            value = os.environ.get(f"TRADOVATE_{symbol}_CONTRACT")
            if value:
                mapped[symbol] = value
        if not mapped:
            raise RuntimeError("explicit Tradovate ES/NQ contract mappings are required")
        contract_ids = {}
        for symbol, contract in mapped.items():
            value = os.environ.get(f"TRADOVATE_{symbol.upper()}_CONTRACT_ID")
            if value:
                contract_ids[contract] = int(value)
        return cls(
            account_id=int(account_id),
            account_spec=account_spec,
            contracts=mapped,
            contract_ids=contract_ids,
            demo=os.environ.get("TRADOVATE_DEMO", "true").lower() != "false",
            enabled=enabled,
            allow_live=allow_live,
            access_token=os.environ.get("TRADOVATE_ACCESS_TOKEN"),
            username=os.environ.get("TRADOVATE_USERNAME"),
            password=os.environ.get("TRADOVATE_PASSWORD"),
            app_id=os.environ.get("TRADOVATE_APP_ID"),
            app_version=os.environ.get("TRADOVATE_APP_VERSION", "1.0"),
            client_id=os.environ.get("TRADOVATE_CID"),
            api_secret=os.environ.get("TRADOVATE_SEC"),
            device_id=os.environ.get("TRADOVATE_DEVICE_ID"),
        )


@dataclass(frozen=True, slots=True)
class FuturesOrderPreview:
    signal: TradeSignal
    contract: str
    quantity: int
    reference_price: float
    estimated_risk: float
    client_order_id: str
    payload: Mapping[str, Any]


class TradovateBroker:
    name = "tradovate"

    def __init__(
        self,
        config: TradovateConfig,
        *,
        store: ExecutionStore | None = None,
        transport: JsonTransport | None = None,
        clock: Any = None,
    ) -> None:
        self.config = config
        self.store = store or ExecutionStore()
        self.transport = transport or UrllibJsonTransport()
        self._clock = clock or (lambda: pd.Timestamp.now(tz="UTC"))
        self._access_token = config.access_token
        self._token_expiration: pd.Timestamp | None = None
        self._contract_ids = {
            str(symbol).upper(): int(contract_id)
            for symbol, contract_id in config.contract_ids.items()
        }

    def authenticate(self) -> None:
        required = {
            "username": self.config.username,
            "password": self.config.password,
            "app_id": self.config.app_id,
            "client_id": self.config.client_id,
            "api_secret": self.config.api_secret,
        }
        missing = [name for name, value in required.items() if not value]
        if missing:
            raise RuntimeError(
                "Tradovate credentials are incomplete: " + ", ".join(missing)
            )
        body: dict[str, Any] = {
            "name": self.config.username,
            "password": self.config.password,
            "appId": self.config.app_id,
            "appVersion": self.config.app_version,
            "cid": self.config.client_id,
            "sec": self.config.api_secret,
        }
        if self.config.device_id:
            body["deviceId"] = self.config.device_id
        payload = self.transport.request(
            "POST",
            f"{self.config.resolved_base_url}/auth/accesstokenrequest",
            body=body,
        )
        self._accept_token_response(payload)

    def renew_access_token(self) -> None:
        if not self._access_token:
            self.authenticate()
            return
        payload = self.transport.request(
            "GET",
            f"{self.config.resolved_base_url}/auth/renewaccesstoken",
            headers={"Authorization": f"Bearer {self._access_token}"},
        )
        self._accept_token_response(payload)

    def snapshot(self, *, as_of: pd.Timestamp | None = None) -> BrokerSnapshot:
        timestamp = as_of or self._clock()
        if timestamp.tz is None:
            raise ValueError("snapshot time must be timezone-aware")
        account = self._request(
            "GET", "/account/item", params={"id": self.config.account_id}
        )
        balance = self._request(
            "POST",
            "/cashBalance/getcashbalancesnapshot",
            body={"accountId": self.config.account_id},
        )
        self._raise_business_error(balance, "cash balance snapshot")
        orders = self._request(
            "GET", "/order/deps", params={"masterid": self.config.account_id}
        )
        positions = self._request(
            "GET", "/position/deps", params={"masterid": self.config.account_id}
        )
        fills = self._request("GET", "/fill/list")
        return BrokerSnapshot(
            self.name,
            timestamp,
            _tradovate_account(account, balance),
            tuple(self._parse_order(item) for item in orders or []),
            tuple(self._parse_position(item) for item in positions or []),
            tuple(self._parse_fill(item) for item in fills or []),
        )

    def preview_order(
        self,
        signal: TradeSignal,
        *,
        as_of: pd.Timestamp,
        reference_price: float,
    ) -> FuturesOrderPreview:
        if as_of.tz is None:
            raise ValueError("as_of must be timezone-aware")
        age = (as_of - signal.signal_time).total_seconds() / 60
        if age < 0:
            raise ValueError("cannot route a future signal")
        if age > self.config.maximum_signal_age_minutes:
            raise ValueError("signal is too stale for futures routing")
        instrument = signal.instrument.upper()
        contract = self.config.contracts.get(instrument)
        if not contract:
            raise ValueError(f"no Tradovate contract configured for {instrument}")
        if signal.contract and signal.contract.upper() != contract.upper():
            raise ValueError("signal contract does not match the configured live contract")
        quantity = int(self.config.quantities.get(instrument, 0))
        if quantity < 1 or quantity > self.config.maximum_quantity:
            raise ValueError("configured futures quantity is outside safety limits")
        if reference_price <= 0:
            raise ValueError("positive futures reference price is required")
        stop_is_valid = (
            signal.proposed_stop < reference_price
            if signal.direction is Direction.LONG
            else signal.proposed_stop > reference_price
        )
        target_is_valid = (
            signal.target.price > reference_price
            if signal.direction is Direction.LONG
            else signal.target.price < reference_price
        )
        if not stop_is_valid or not target_is_valid:
            raise ValueError("futures stop and target do not bracket the entry price")
        risk = (
            abs(reference_price - signal.proposed_stop)
            * INSTRUMENTS[instrument].point_value
            * quantity
        )
        client_order_id = _futures_client_order_id(signal, contract)
        action = "Buy" if signal.direction is Direction.LONG else "Sell"
        exit_action = "Sell" if action == "Buy" else "Buy"
        payload = {
            "accountSpec": self.config.account_spec,
            "accountId": self.config.account_id,
            "clOrdId": client_order_id,
            "action": action,
            "symbol": contract,
            "orderQty": quantity,
            "orderType": "Market",
            "timeInForce": "Day",
            "isAutomated": True,
            "bracket1": {
                "action": exit_action,
                "clOrdId": f"{client_order_id}-tp",
                "orderType": "Limit",
                "price": signal.target.price,
                "timeInForce": "GTC",
                "text": "Silver Bullet DOL target",
            },
            "bracket2": {
                "action": exit_action,
                "clOrdId": f"{client_order_id}-sl",
                "orderType": "Stop",
                "stopPrice": signal.proposed_stop,
                "timeInForce": "GTC",
                "text": "Silver Bullet protective stop",
            },
        }
        return FuturesOrderPreview(
            signal, contract, quantity, reference_price, risk, client_order_id, payload
        )

    def submit_order(
        self,
        signal: TradeSignal,
        *,
        as_of: pd.Timestamp,
        reference_price: float,
        snapshot: BrokerSnapshot,
        risk_gate: LiveRiskGate,
        dry_run: bool = True,
        daily_pl: float | None = None,
    ) -> OrderSubmission:
        preview = self.preview_order(
            signal, as_of=as_of, reference_price=reference_price
        )
        risk_gate.validate(
            snapshot,
            estimated_order_risk=preview.estimated_risk,
            daily_pl=daily_pl,
        )
        if self.store.get(preview.client_order_id) is not None:
            raise RuntimeError("this futures signal is already tracked")
        if dry_run:
            return OrderSubmission(
                self.name,
                None,
                preview.client_order_id,
                False,
                True,
                preview.payload,
            )
        self._validate_submission_enabled()
        contract_id = self._resolve_contract_id(preview.contract)
        response = self._request("POST", "/order/placeoso", body=preview.payload)
        order_id = self._response_order_id(response, "placeOSO")
        positions = {position.symbol.upper(): position.quantity for position in snapshot.positions}
        signed_quantity = (
            preview.quantity
            if signal.direction is Direction.LONG
            else -preview.quantity
        )
        now = self._clock()
        trade = TrackedTrade(
            trade_id=preview.client_order_id,
            broker=self.name,
            route="futures",
            instrument=signal.instrument,
            direction=signal.direction,
            entry_order_id=order_id,
            client_order_id=preview.client_order_id,
            legs=(
                TrackedLeg(
                    preview.contract,
                    signed_quantity,
                    AssetClass.FUTURE,
                    "Sell" if signed_quantity > 0 else "Buy",
                    contract_id=contract_id,
                ),
            ),
            baseline_positions={
                preview.contract.upper(): positions.get(preview.contract.upper(), 0.0)
            },
            stop_price=signal.proposed_stop,
            target_price=signal.target.price,
            created_at=now,
            updated_at=now,
        )
        self.store.record(trade)
        return OrderSubmission(
            self.name,
            order_id,
            preview.client_order_id,
            True,
            False,
            preview.payload,
            response,
        )

    def close_trade(self, trade: TrackedTrade, reason: str) -> OrderSubmission:
        if trade.broker != self.name or trade.route != "futures":
            raise ValueError("tracked trade is not a Tradovate futures trade")
        if trade.status not in {
            ManagedTradeStatus.OPEN,
            ManagedTradeStatus.EXITING,
        }:
            raise RuntimeError("only open futures trades can be liquidated")
        self._validate_submission_enabled()
        leg = trade.legs[0]
        contract_id = leg.contract_id or self._resolve_contract_id(leg.symbol)
        client_order_id = _liquidation_client_order_id(trade, reason)
        payload = {
            "accountId": self.config.account_id,
            "contractId": contract_id,
            "admin": False,
            "customTag50": client_order_id,
        }
        response = self._request(
            "POST", "/order/liquidateposition", body=payload
        )
        order_id = self._response_order_id(response, "liquidatePosition")
        return OrderSubmission(
            self.name,
            order_id,
            client_order_id,
            True,
            False,
            payload,
            response,
        )

    def cancel_order(self, order_id: str) -> Any:
        self._validate_submission_enabled()
        response = self._request(
            "POST",
            "/order/cancelorder",
            body={"orderId": int(order_id), "isAutomated": True},
        )
        self._raise_business_error(response, "cancelOrder")
        return response

    def cancel_all_orders(self) -> tuple[Any, ...]:
        self._validate_submission_enabled()
        orders = self._request(
            "GET", "/order/deps", params={"masterid": self.config.account_id}
        )
        responses = []
        for item in orders or []:
            order = self._parse_order(item)
            if not order.status.terminal:
                responses.append(self.cancel_order(order.order_id))
        return tuple(responses)

    def close_all_positions(self) -> tuple[Any, ...]:
        self._validate_submission_enabled()
        positions = self._request(
            "GET", "/position/deps", params={"masterid": self.config.account_id}
        )
        responses = []
        for item in positions or []:
            if float(item.get("netPos") or 0) == 0:
                continue
            payload = {
                "accountId": self.config.account_id,
                "contractId": int(item["contractId"]),
                "admin": False,
                "customTag50": "sb-kill-switch",
            }
            response = self._request(
                "POST", "/order/liquidateposition", body=payload
            )
            self._response_order_id(response, "liquidatePosition")
            responses.append(response)
        return tuple(responses)

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
                self.close_all_positions()

    def _request(
        self,
        method: str,
        path: str,
        *,
        params: Mapping[str, Any] | None = None,
        body: Mapping[str, Any] | None = None,
    ) -> Any:
        self._ensure_token()
        url = f"{self.config.resolved_base_url}{path}"
        headers = {"Authorization": f"Bearer {self._access_token}"}
        try:
            return self.transport.request(
                method, url, headers=headers, params=params, body=body
            )
        except ExternalServiceError as exc:
            if exc.status != 401 or not self.config.username:
                raise
            self._access_token = None
            self._token_expiration = None
            self.authenticate()
            return self.transport.request(
                method,
                url,
                headers={"Authorization": f"Bearer {self._access_token}"},
                params=params,
                body=body,
            )

    def _ensure_token(self) -> None:
        if not self._access_token:
            self.authenticate()
            return
        if self._token_expiration is None:
            return
        if self._clock() >= self._token_expiration - pd.Timedelta(minutes=15):
            try:
                self.renew_access_token()
            except Exception:
                if not self.config.username:
                    raise
                self._access_token = None
                self.authenticate()

    def _accept_token_response(self, payload: Any) -> None:
        if not isinstance(payload, dict):
            raise ExternalServiceError("Tradovate returned an invalid token response")
        self._raise_business_error(payload, "authentication")
        token = payload.get("accessToken") or payload.get("access_token")
        if not token:
            raise ExternalServiceError("Tradovate token response is missing accessToken")
        expiration = payload.get("expirationTime")
        self._access_token = str(token)
        self._token_expiration = None if expiration is None else pd.Timestamp(expiration)

    def _resolve_contract_id(self, contract: str) -> int:
        normalized = contract.upper()
        cached = self._contract_ids.get(normalized)
        if cached is not None:
            return cached
        payload = self._request(
            "GET", "/contract/find", params={"name": normalized}
        )
        if not isinstance(payload, dict) or not payload.get("id"):
            raise ExternalServiceError(
                f"Tradovate did not resolve contract id for {normalized}"
            )
        contract_id = int(payload["id"])
        self._contract_ids[normalized] = contract_id
        return contract_id

    def _symbol_for_contract_id(self, contract_id: int) -> str:
        for symbol, identifier in self._contract_ids.items():
            if identifier == contract_id:
                return symbol
        configured = [
            contract.upper()
            for contract in self.config.contracts.values()
            if self.config.contract_ids.get(contract) == contract_id
        ]
        return configured[0] if configured else f"contract:{contract_id}"

    def _parse_order(self, item: Mapping[str, Any]) -> BrokerOrder:
        contract_id = int(item.get("contractId") or 0)
        return BrokerOrder(
            order_id=str(item.get("id") or ""),
            client_order_id=item.get("clOrdId"),
            symbol=self._symbol_for_contract_id(contract_id),
            side=str(item.get("action") or ""),
            quantity=float(item.get("orderQty") or 0),
            filled_quantity=float(item.get("filledQty") or item.get("cumQty") or 0),
            status=_tradovate_order_status(str(item.get("ordStatus") or "unknown")),
            asset_class=AssetClass.FUTURE,
            submitted_at=_optional_timestamp(item.get("timestamp")),
            filled_at=_optional_timestamp(item.get("filledTime")),
            order_type=str(item.get("orderType") or "market"),
            limit_price=_optional_float(item.get("price")),
            stop_price=_optional_float(item.get("stopPrice")),
            parent_order_id=(
                None if item.get("parentId") is None else str(item["parentId"])
            ),
            raw=item,
        )

    def _parse_position(self, item: Mapping[str, Any]) -> BrokerPosition:
        contract_id = int(item.get("contractId") or 0)
        return BrokerPosition(
            symbol=self._symbol_for_contract_id(contract_id),
            quantity=float(item.get("netPos") or 0),
            average_entry_price=float(item.get("netPrice") or 0),
            market_price=_optional_float(item.get("marketPrice")),
            market_value=_optional_float(item.get("marketValue")),
            unrealized_pl=_optional_float(item.get("openPnL")),
            asset_class=AssetClass.FUTURE,
            contract_id=contract_id,
        )

    def _parse_fill(self, item: Mapping[str, Any]) -> BrokerFill:
        timestamp = _optional_timestamp(item.get("timestamp"))
        if timestamp is None:
            raise ValueError("Tradovate fill is missing timestamp")
        contract_id = int(item.get("contractId") or 0)
        return BrokerFill(
            fill_id=str(item.get("id") or ""),
            order_id=str(item.get("orderId") or ""),
            symbol=self._symbol_for_contract_id(contract_id),
            side=str(item.get("action") or ""),
            quantity=float(item.get("qty") or 0),
            price=float(item.get("price") or 0),
            timestamp=timestamp,
        )

    def _response_order_id(self, payload: Any, operation: str) -> str:
        self._raise_business_error(payload, operation)
        if not isinstance(payload, dict) or not payload.get("orderId"):
            raise ExternalServiceError(
                f"Tradovate {operation} response did not include orderId"
            )
        return str(payload["orderId"])

    @staticmethod
    def _raise_business_error(payload: Any, operation: str) -> None:
        if not isinstance(payload, dict):
            return
        message = payload.get("failureText") or payload.get("errorText")
        reason = payload.get("failureReason")
        if message or reason:
            raise ExternalServiceError(
                f"Tradovate {operation} failed: {message or reason}"
            )

    def _validate_submission_enabled(self) -> None:
        if not self.config.enabled:
            raise RuntimeError("Tradovate submission is disabled")
        if not self.config.demo and not self.config.allow_live:
            raise RuntimeError("live Tradovate submission requires allow_live=True")


def _tradovate_account(
    account: Mapping[str, Any], balance: Mapping[str, Any]
) -> BrokerAccount:
    active = bool(account.get("active", True))
    net_liq = float(balance.get("netLiq") or balance.get("totalCashValue") or 0)
    initial_margin = float(balance.get("initialMargin") or 0)
    available = max(0.0, net_liq - initial_margin)
    return BrokerAccount(
        account_id=str(account.get("id") or ""),
        status="ACTIVE" if active else "INACTIVE",
        equity=net_liq,
        cash=float(balance.get("totalCashValue") or 0),
        buying_power=available,
        trading_blocked=not active,
        account_blocked=not active,
        daily_pl=_optional_float(balance.get("totalPnL")),
    )


def _tradovate_order_status(value: str) -> BrokerOrderStatus:
    normalized = value.lower()
    mapping = {
        "pendingnew": BrokerOrderStatus.PENDING,
        "pendingcancel": BrokerOrderStatus.PENDING,
        "working": BrokerOrderStatus.ACCEPTED,
        "accepted": BrokerOrderStatus.ACCEPTED,
        "new": BrokerOrderStatus.NEW,
        "partiallyfilled": BrokerOrderStatus.PARTIALLY_FILLED,
        "filled": BrokerOrderStatus.FILLED,
        "canceled": BrokerOrderStatus.CANCELED,
        "rejected": BrokerOrderStatus.REJECTED,
        "expired": BrokerOrderStatus.EXPIRED,
        "completed": BrokerOrderStatus.FILLED,
    }
    return mapping.get(normalized, BrokerOrderStatus.UNKNOWN)


def _futures_client_order_id(signal: TradeSignal, contract: str) -> str:
    raw = "|".join(
        (
            signal.instrument,
            contract,
            signal.session,
            signal.direction.value,
            signal.signal_time.isoformat(),
        )
    )
    return f"sb-fut-{hashlib.sha256(raw.encode()).hexdigest()[:20]}"


def _liquidation_client_order_id(trade: TrackedTrade, reason: str) -> str:
    raw = f"{trade.trade_id}|{reason}"
    return f"sb-flat-{hashlib.sha256(raw.encode()).hexdigest()[:20]}"


def _optional_float(value: Any) -> float | None:
    return None if value is None else float(value)


def _optional_timestamp(value: Any) -> pd.Timestamp | None:
    if value is None:
        return None
    timestamp = pd.Timestamp(value)
    if timestamp.tz is None:
        timestamp = timestamp.tz_localize("UTC")
    return timestamp
