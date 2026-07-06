"""Alpaca proxy execution boundary for accepted research signals."""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import Any, Callable, Mapping

import pandas as pd

from .models import Direction, NEW_YORK, TradeSignal

OrderFactory = Callable[["ProxyOrderPreview"], Any]


@dataclass(frozen=True, slots=True)
class AlpacaProxyConfig:
    """Execution safety settings for futures-to-proxy order routing."""

    proxy_symbols: Mapping[str, str] = field(
        default_factory=lambda: {"ES": "SPY", "NQ": "QQQ"}
    )
    quantity_by_proxy: Mapping[str, float] = field(
        default_factory=lambda: {"SPY": 1, "QQQ": 1}
    )
    paper: bool = True
    enabled: bool = False
    allow_live: bool = False
    max_signal_age_minutes: int = 2
    max_notional_per_order: float | None = None
    time_in_force: str = "DAY"

    def __post_init__(self) -> None:
        if self.max_signal_age_minutes < 0:
            raise ValueError("max_signal_age_minutes cannot be negative")
        if self.max_notional_per_order is not None and self.max_notional_per_order <= 0:
            raise ValueError("max_notional_per_order must be positive")


@dataclass(frozen=True, slots=True)
class ProxyOrderPreview:
    signal: TradeSignal
    proxy_symbol: str
    side: str
    quantity: float
    time_in_force: str
    estimated_notional: float | None = None


@dataclass(frozen=True, slots=True)
class ProxyOrderResult:
    preview: ProxyOrderPreview
    submitted: bool
    dry_run: bool
    broker_order: Any = None


class AlpacaProxyExecutor:
    """Preview and submit proxy orders through Alpaca after a signal is accepted."""

    def __init__(
        self,
        config: AlpacaProxyConfig | None = None,
        *,
        trading_client: Any = None,
        order_factory: OrderFactory | None = None,
    ) -> None:
        self.config = config or AlpacaProxyConfig()
        self.trading_client = trading_client
        self._order_factory = order_factory or _alpaca_market_order

    @classmethod
    def from_environment(
        cls,
        config: AlpacaProxyConfig | None = None,
    ) -> "AlpacaProxyExecutor":
        api_key = os.environ.get("ALPACA_API_KEY")
        secret_key = os.environ.get("ALPACA_SECRET_KEY")
        if not api_key or not secret_key:
            raise RuntimeError("ALPACA_API_KEY and ALPACA_SECRET_KEY are required")
        try:
            from alpaca.trading.client import TradingClient
        except ImportError as exc:  # pragma: no cover - depends on optional SDK
            raise RuntimeError("install alpaca-py to use Alpaca execution") from exc
        cfg = config or AlpacaProxyConfig(
            paper=os.environ.get("ALPACA_PAPER", "true").lower() != "false"
        )
        client = TradingClient(api_key=api_key, secret_key=secret_key, paper=cfg.paper)
        return cls(cfg, trading_client=client)

    def preview_order(
        self,
        signal: TradeSignal,
        *,
        as_of: pd.Timestamp,
        reference_price: float | None = None,
    ) -> ProxyOrderPreview:
        self._validate_signal_age(signal, as_of)
        symbol = self.config.proxy_symbols.get(signal.instrument.upper())
        if not symbol:
            raise ValueError(f"no proxy symbol configured for {signal.instrument}")
        symbol = symbol.upper()
        quantity = self.config.quantity_by_proxy.get(symbol)
        if quantity is None or quantity <= 0:
            raise ValueError(f"no positive proxy quantity configured for {symbol}")
        notional = None if reference_price is None else reference_price * quantity
        if (
            notional is not None
            and self.config.max_notional_per_order is not None
            and notional > self.config.max_notional_per_order
        ):
            raise ValueError("estimated order notional exceeds configured maximum")
        side = "buy" if signal.direction is Direction.LONG else "sell"
        return ProxyOrderPreview(
            signal,
            symbol,
            side,
            quantity,
            self.config.time_in_force.upper(),
            notional,
        )

    def submit_order(
        self,
        signal: TradeSignal,
        *,
        as_of: pd.Timestamp,
        reference_price: float | None = None,
        dry_run: bool = True,
    ) -> ProxyOrderResult:
        preview = self.preview_order(
            signal, as_of=as_of, reference_price=reference_price
        )
        if dry_run:
            return ProxyOrderResult(preview, submitted=False, dry_run=True)
        self._validate_submission_enabled()
        if self.trading_client is None:
            raise RuntimeError("trading_client is required for order submission")
        order_data = self._order_factory(preview)
        order = self.trading_client.submit_order(order_data=order_data)
        return ProxyOrderResult(preview, submitted=True, dry_run=False, broker_order=order)

    def _validate_signal_age(
        self, signal: TradeSignal, as_of: pd.Timestamp
    ) -> None:
        if as_of.tz is None:
            raise ValueError("as_of must be timezone-aware")
        age = (as_of.tz_convert(NEW_YORK) - signal.signal_time.tz_convert(NEW_YORK)) / (
            pd.Timedelta(minutes=1)
        )
        if age < 0:
            raise ValueError("cannot execute a future signal")
        if age > self.config.max_signal_age_minutes:
            raise ValueError("signal is too stale for proxy execution")

    def _validate_submission_enabled(self) -> None:
        if not self.config.enabled:
            raise RuntimeError("Alpaca submission is disabled")
        if not self.config.paper and not self.config.allow_live:
            raise RuntimeError("live Alpaca submission requires allow_live=True")


def _alpaca_market_order(preview: ProxyOrderPreview) -> Any:
    try:
        from alpaca.trading.enums import OrderSide, TimeInForce
        from alpaca.trading.requests import MarketOrderRequest
    except ImportError as exc:  # pragma: no cover - depends on optional SDK
        raise RuntimeError("install alpaca-py to create Alpaca orders") from exc

    side = OrderSide.BUY if preview.side == "buy" else OrderSide.SELL
    tif = getattr(TimeInForce, preview.time_in_force)
    return MarketOrderRequest(
        symbol=preview.proxy_symbol,
        qty=preview.quantity,
        side=side,
        time_in_force=tif,
    )
