"""Provider-neutral option-chain models and deterministic contract selection."""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from datetime import date, timedelta
from enum import Enum
from typing import Any, Mapping

import pandas as pd

from .models import Direction, NEW_YORK, TradeSignal
from .transport import JsonTransport, UrllibJsonTransport


class OptionRight(str, Enum):
    CALL = "call"
    PUT = "put"


class OptionsProfile(str, Enum):
    HIGH_DELTA_LONG = "high_delta_long"
    DOL_DEBIT_SPREAD = "dol_debit_spread"
    MOMENTUM_0DTE = "momentum_0dte"


@dataclass(frozen=True, slots=True)
class OptionContract:
    symbol: str
    underlying: str
    right: OptionRight
    strike: float
    expiration: date
    multiplier: int = 100
    tradable: bool = True
    open_interest: int = 0

    def __post_init__(self) -> None:
        if not self.symbol or not self.underlying:
            raise ValueError("option symbol and underlying are required")
        if self.strike <= 0:
            raise ValueError("option strike must be positive")
        if self.multiplier <= 0:
            raise ValueError("option multiplier must be positive")
        if self.open_interest < 0:
            raise ValueError("option open interest cannot be negative")


@dataclass(frozen=True, slots=True)
class OptionQuote:
    contract: OptionContract
    bid: float
    ask: float
    timestamp: pd.Timestamp
    last: float | None = None
    delta: float | None = None
    gamma: float | None = None
    theta: float | None = None
    vega: float | None = None
    implied_volatility: float | None = None
    volume: int = 0

    def __post_init__(self) -> None:
        if self.timestamp.tz is None:
            raise ValueError("option quote timestamp must be timezone-aware")
        if self.bid < 0 or self.ask <= 0 or self.ask < self.bid:
            raise ValueError("option quote must have a valid bid and ask")
        if self.volume < 0:
            raise ValueError("option volume cannot be negative")

    @property
    def mid(self) -> float:
        return (self.bid + self.ask) / 2

    @property
    def spread(self) -> float:
        return self.ask - self.bid

    @property
    def spread_pct(self) -> float:
        return math.inf if self.mid <= 0 else self.spread / self.mid


@dataclass(frozen=True, slots=True)
class OptionChainSnapshot:
    underlying: str
    underlying_price: float
    as_of: pd.Timestamp
    quotes: tuple[OptionQuote, ...]
    provider: str = "unknown"

    def __post_init__(self) -> None:
        if self.as_of.tz is None:
            raise ValueError("option chain timestamp must be timezone-aware")
        if self.underlying_price <= 0:
            raise ValueError("underlying price must be positive")
        if any(
            quote.contract.underlying.upper() != self.underlying.upper()
            for quote in self.quotes
        ):
            raise ValueError("all option quotes must match the chain underlying")


@dataclass(frozen=True, slots=True)
class OptionLeg:
    contract: OptionContract
    side: str
    position_intent: str
    ratio_quantity: int = 1
    reference_price: float | None = None

    def __post_init__(self) -> None:
        if self.side not in {"buy", "sell"}:
            raise ValueError("option leg side must be buy or sell")
        if self.position_intent not in {
            "buy_to_open",
            "buy_to_close",
            "sell_to_open",
            "sell_to_close",
        }:
            raise ValueError("invalid option position intent")
        if self.ratio_quantity < 1:
            raise ValueError("option leg ratio must be positive")


@dataclass(frozen=True, slots=True)
class OptionOrderCandidate:
    signal: TradeSignal
    profile: OptionsProfile
    underlying: str
    legs: tuple[OptionLeg, ...]
    quantity: int
    limit_price: float
    estimated_max_loss: float
    estimated_max_profit: float | None
    score: float
    rationale: str

    def __post_init__(self) -> None:
        if not self.legs or self.quantity < 1:
            raise ValueError("option candidate needs legs and positive quantity")
        if self.limit_price <= 0 or self.estimated_max_loss <= 0:
            raise ValueError("option candidate prices and risk must be positive")


@dataclass(frozen=True, slots=True)
class OptionsSelectionConfig:
    proxy_symbols: Mapping[str, str] = field(
        default_factory=lambda: {"ES": "SPY", "NQ": "QQQ"}
    )
    max_signal_age_minutes: float = 2.0
    max_chain_age_seconds: float = 90.0
    max_spread_pct: float = 0.08
    min_volume: int = 25
    min_open_interest: int = 100
    max_premium_per_contract: float = 750.0
    high_delta_min: float = 0.65
    high_delta_max: float = 0.75
    high_delta_target: float = 0.70
    high_delta_max_dte: int = 1
    momentum_delta_target: float = 0.40
    momentum_delta_tolerance: float = 0.08
    momentum_sessions: frozenset[str] = frozenset({"macro", "ny_am"})
    momentum_min_delivery_points: float = 10.0
    spread_max_dte: int = 7
    max_dol_strike_distance: float = 1.0
    min_spread_reward_to_risk: float = 0.5
    maximum_candidates: int = 5
    maximum_contracts: int = 10

    def __post_init__(self) -> None:
        if self.max_signal_age_minutes < 0 or self.max_chain_age_seconds < 0:
            raise ValueError("freshness limits cannot be negative")
        if not 0 < self.max_spread_pct < 1:
            raise ValueError("max_spread_pct must be between zero and one")
        if self.min_volume < 0 or self.min_open_interest < 0:
            raise ValueError("liquidity minimums cannot be negative")
        if self.max_premium_per_contract <= 0:
            raise ValueError("maximum premium must be positive")
        if not 0 <= self.high_delta_min <= self.high_delta_max <= 1:
            raise ValueError("invalid high-delta range")
        if self.maximum_candidates < 1 or self.maximum_contracts < 1:
            raise ValueError("candidate and contract limits must be positive")


class OptionsSelector:
    """Convert a futures signal and a chain snapshot into ranked order candidates."""

    def __init__(self, config: OptionsSelectionConfig | None = None) -> None:
        self.config = config or OptionsSelectionConfig()

    def select(
        self,
        signal: TradeSignal,
        chain: OptionChainSnapshot,
        *,
        profile: OptionsProfile = OptionsProfile.HIGH_DELTA_LONG,
        as_of: pd.Timestamp,
        risk_budget: float,
        proxy_target: float | None = None,
    ) -> tuple[OptionOrderCandidate, ...]:
        proxy = self._validate_request(signal, chain, profile, as_of, risk_budget)
        right = OptionRight.CALL if signal.direction is Direction.LONG else OptionRight.PUT
        eligible = [
            quote
            for quote in chain.quotes
            if quote.contract.right is right
            and quote.contract.tradable
            and self._liquid(quote)
        ]
        if profile is OptionsProfile.HIGH_DELTA_LONG:
            candidates = self._high_delta(signal, chain, eligible, risk_budget)
        elif profile is OptionsProfile.MOMENTUM_0DTE:
            candidates = self._momentum(signal, chain, eligible, risk_budget)
        else:
            if proxy_target is None:
                raise ValueError("proxy_target is required for a DOL debit spread")
            candidates = self._debit_spreads(
                signal, chain, eligible, risk_budget, proxy_target
            )
        if proxy != chain.underlying.upper():  # defensive; validated above
            return ()
        return tuple(
            sorted(candidates, key=lambda item: (item.score, _leg_key(item)))[:
                self.config.maximum_candidates
            ]
        )

    def _validate_request(
        self,
        signal: TradeSignal,
        chain: OptionChainSnapshot,
        profile: OptionsProfile,
        as_of: pd.Timestamp,
        risk_budget: float,
    ) -> str:
        if as_of.tz is None:
            raise ValueError("as_of must be timezone-aware")
        if risk_budget <= 0:
            raise ValueError("risk budget must be positive")
        proxy = self.config.proxy_symbols.get(signal.instrument.upper())
        if not proxy:
            raise ValueError(f"no options proxy configured for {signal.instrument}")
        if chain.underlying.upper() != proxy.upper():
            raise ValueError("option chain underlying does not match signal proxy")
        age = (as_of - signal.signal_time).total_seconds() / 60
        if age < 0:
            raise ValueError("cannot select options for a future signal")
        if age > self.config.max_signal_age_minutes:
            raise ValueError("signal is too stale for options selection")
        chain_age = (as_of - chain.as_of).total_seconds()
        if chain_age < 0:
            raise ValueError("option chain cannot be from the future")
        if chain_age > self.config.max_chain_age_seconds:
            raise ValueError("option chain is stale")
        if profile is OptionsProfile.MOMENTUM_0DTE:
            delivery = abs(signal.target.price - signal.origin_swing_price)
            if signal.session not in self.config.momentum_sessions:
                raise ValueError("0DTE momentum is not allowed in this session")
            if delivery < self.config.momentum_min_delivery_points:
                raise ValueError("delivery is too small for 0DTE momentum")
        return proxy.upper()

    def _liquid(self, quote: OptionQuote) -> bool:
        contract = quote.contract
        return (
            quote.spread_pct <= self.config.max_spread_pct
            and quote.volume >= self.config.min_volume
            and contract.open_interest >= self.config.min_open_interest
            and quote.ask * contract.multiplier
            <= self.config.max_premium_per_contract
        )

    def _high_delta(
        self,
        signal: TradeSignal,
        chain: OptionChainSnapshot,
        quotes: list[OptionQuote],
        risk_budget: float,
    ) -> list[OptionOrderCandidate]:
        today = chain.as_of.tz_convert(NEW_YORK).date()
        result = []
        for quote in quotes:
            delta = _absolute_delta(quote)
            dte = (quote.contract.expiration - today).days
            if (
                delta is None
                or not self.config.high_delta_min
                <= delta
                <= self.config.high_delta_max
                or not 0 <= dte <= self.config.high_delta_max_dte
            ):
                continue
            candidate = self._single_leg_candidate(
                signal,
                quote,
                OptionsProfile.HIGH_DELTA_LONG,
                risk_budget,
                abs(delta - self.config.high_delta_target) * 100
                + quote.spread_pct * 10
                + dte,
                f"delta {delta:.2f}, {dte}DTE, spread {quote.spread_pct:.1%}",
            )
            if candidate:
                result.append(candidate)
        return result

    def _momentum(
        self,
        signal: TradeSignal,
        chain: OptionChainSnapshot,
        quotes: list[OptionQuote],
        risk_budget: float,
    ) -> list[OptionOrderCandidate]:
        today = chain.as_of.tz_convert(NEW_YORK).date()
        result = []
        for quote in quotes:
            delta = _absolute_delta(quote)
            dte = (quote.contract.expiration - today).days
            if (
                delta is None
                or dte != 0
                or abs(delta - self.config.momentum_delta_target)
                > self.config.momentum_delta_tolerance
            ):
                continue
            gamma_bonus = 0.0 if quote.gamma is None else min(abs(quote.gamma), 1.0)
            candidate = self._single_leg_candidate(
                signal,
                quote,
                OptionsProfile.MOMENTUM_0DTE,
                risk_budget,
                abs(delta - self.config.momentum_delta_target) * 100
                + quote.spread_pct * 10
                - gamma_bonus,
                f"0DTE delta {delta:.2f}, spread {quote.spread_pct:.1%}",
            )
            if candidate:
                result.append(candidate)
        return result

    def _single_leg_candidate(
        self,
        signal: TradeSignal,
        quote: OptionQuote,
        profile: OptionsProfile,
        risk_budget: float,
        score: float,
        rationale: str,
    ) -> OptionOrderCandidate | None:
        cost = quote.ask * quote.contract.multiplier
        quantity = min(
            self.config.maximum_contracts,
            int(risk_budget // cost),
        )
        if quantity < 1:
            return None
        return OptionOrderCandidate(
            signal=signal,
            profile=profile,
            underlying=quote.contract.underlying.upper(),
            legs=(
                OptionLeg(
                    quote.contract,
                    "buy",
                    "buy_to_open",
                    reference_price=quote.ask,
                ),
            ),
            quantity=quantity,
            limit_price=quote.ask,
            estimated_max_loss=cost * quantity,
            estimated_max_profit=None,
            score=score,
            rationale=rationale,
        )

    def _debit_spreads(
        self,
        signal: TradeSignal,
        chain: OptionChainSnapshot,
        quotes: list[OptionQuote],
        risk_budget: float,
        proxy_target: float,
    ) -> list[OptionOrderCandidate]:
        today = chain.as_of.tz_convert(NEW_YORK).date()
        allowed = [
            quote
            for quote in quotes
            if 0 <= (quote.contract.expiration - today).days <= self.config.spread_max_dte
        ]
        if not allowed:
            return []
        nearest_expiry = min(quote.contract.expiration for quote in allowed)
        allowed = [
            quote for quote in allowed if quote.contract.expiration == nearest_expiry
        ]
        result = []
        for long_quote in allowed:
            for short_quote in allowed:
                long_strike = long_quote.contract.strike
                short_strike = short_quote.contract.strike
                correct_order = (
                    short_strike > long_strike
                    if signal.direction is Direction.LONG
                    else short_strike < long_strike
                )
                if not correct_order:
                    continue
                target_distance = abs(short_strike - proxy_target)
                if target_distance > self.config.max_dol_strike_distance:
                    continue
                debit = long_quote.ask - short_quote.bid
                width = abs(short_strike - long_strike)
                if debit <= 0 or debit >= width:
                    continue
                per_spread_loss = debit * long_quote.contract.multiplier
                per_spread_profit = (width - debit) * long_quote.contract.multiplier
                reward_to_risk = per_spread_profit / per_spread_loss
                if reward_to_risk < self.config.min_spread_reward_to_risk:
                    continue
                quantity = min(
                    self.config.maximum_contracts,
                    int(risk_budget // per_spread_loss),
                )
                if quantity < 1:
                    continue
                score = (
                    target_distance * 10
                    + abs(long_strike - chain.underlying_price)
                    + long_quote.spread_pct
                    + short_quote.spread_pct
                    - min(reward_to_risk, 10) / 100
                )
                result.append(
                    OptionOrderCandidate(
                        signal=signal,
                        profile=OptionsProfile.DOL_DEBIT_SPREAD,
                        underlying=chain.underlying.upper(),
                        legs=(
                            OptionLeg(
                                long_quote.contract,
                                "buy",
                                "buy_to_open",
                                reference_price=long_quote.ask,
                            ),
                            OptionLeg(
                                short_quote.contract,
                                "sell",
                                "sell_to_open",
                                reference_price=short_quote.bid,
                            ),
                        ),
                        quantity=quantity,
                        limit_price=round(debit, 2),
                        estimated_max_loss=per_spread_loss * quantity,
                        estimated_max_profit=per_spread_profit * quantity,
                        score=score,
                        rationale=(
                            f"DOL strike distance {target_distance:.2f}, "
                            f"reward/risk {reward_to_risk:.2f}"
                        ),
                    )
                )
        return result


@dataclass(frozen=True, slots=True)
class AlpacaOptionChainConfig:
    api_key: str
    secret_key: str
    paper: bool = True
    option_feed: str = "indicative"
    stock_feed: str = "iex"
    maximum_dte: int = 7
    page_limit: int = 1000
    maximum_pages: int = 20
    trading_base_url: str | None = None
    data_base_url: str = "https://data.alpaca.markets"

    @property
    def resolved_trading_base_url(self) -> str:
        if self.trading_base_url:
            return self.trading_base_url.rstrip("/")
        return (
            "https://paper-api.alpaca.markets"
            if self.paper
            else "https://api.alpaca.markets"
        )


class AlpacaOptionChainClient:
    """Fetch Alpaca contract metadata and snapshots into provider-neutral types."""

    def __init__(
        self,
        config: AlpacaOptionChainConfig,
        *,
        transport: JsonTransport | None = None,
    ) -> None:
        if not config.api_key or not config.secret_key:
            raise ValueError("Alpaca API credentials are required")
        self.config = config
        self.transport = transport or UrllibJsonTransport()

    def fetch(
        self,
        underlying: str,
        *,
        as_of: pd.Timestamp,
        underlying_price: float | None = None,
    ) -> OptionChainSnapshot:
        if as_of.tz is None:
            raise ValueError("as_of must be timezone-aware")
        symbol = underlying.upper()
        local_date = as_of.tz_convert(NEW_YORK).date()
        expiration_lte = local_date + timedelta(days=self.config.maximum_dte)
        contracts_payload = self._paginated(
            f"{self.config.resolved_trading_base_url}/v2/options/contracts",
            {
                "underlying_symbols": symbol,
                "status": "active",
                "expiration_date_gte": local_date.isoformat(),
                "expiration_date_lte": expiration_lte.isoformat(),
                "limit": self.config.page_limit,
            },
            "option_contracts",
        )
        snapshots = self._snapshot_pages(symbol, local_date, expiration_lte)
        contracts = {
            str(item["symbol"]): _contract_from_alpaca(item)
            for item in contracts_payload
            if item.get("symbol")
        }
        quotes = []
        for option_symbol, snapshot in snapshots.items():
            contract = contracts.get(option_symbol)
            if contract is None:
                try:
                    contract = _contract_from_occ_symbol(option_symbol)
                except ValueError:
                    continue
            quote = _quote_from_alpaca(contract, snapshot, as_of)
            if quote is not None:
                quotes.append(quote)
        price = underlying_price or self._underlying_price(symbol)
        return OptionChainSnapshot(
            symbol,
            price,
            as_of,
            tuple(sorted(quotes, key=lambda item: item.contract.symbol)),
            provider="alpaca",
        )

    def _snapshot_pages(
        self, symbol: str, start: date, end: date
    ) -> dict[str, Mapping[str, Any]]:
        result: dict[str, Mapping[str, Any]] = {}
        params: dict[str, Any] = {
            "feed": self.config.option_feed,
            "expiration_date_gte": start.isoformat(),
            "expiration_date_lte": end.isoformat(),
            "limit": self.config.page_limit,
        }
        url = f"{self.config.data_base_url.rstrip('/')}/v1beta1/options/snapshots/{symbol}"
        token = None
        for _ in range(self.config.maximum_pages):
            params["page_token"] = token
            payload = self._get(url, params)
            page = payload.get("snapshots", {}) if isinstance(payload, dict) else {}
            if isinstance(page, dict):
                result.update(page)
            next_token = payload.get("next_page_token") if isinstance(payload, dict) else None
            if not next_token or next_token == token:
                break
            token = next_token
        else:
            raise RuntimeError("Alpaca option snapshot pagination limit exceeded")
        return result

    def _paginated(
        self, url: str, params: dict[str, Any], collection_key: str
    ) -> list[Mapping[str, Any]]:
        result: list[Mapping[str, Any]] = []
        token = None
        for _ in range(self.config.maximum_pages):
            params["page_token"] = token
            payload = self._get(url, params)
            page = payload.get(collection_key, []) if isinstance(payload, dict) else []
            result.extend(item for item in page if isinstance(item, dict))
            next_token = payload.get("next_page_token") if isinstance(payload, dict) else None
            if not next_token or next_token == token:
                break
            token = next_token
        else:
            raise RuntimeError("Alpaca contract pagination limit exceeded")
        return result

    def _underlying_price(self, symbol: str) -> float:
        url = f"{self.config.data_base_url.rstrip('/')}/v2/stocks/{symbol}/snapshot"
        payload = self._get(url, {"feed": self.config.stock_feed})
        for container, field_name in (
            ("latestTrade", "p"),
            ("minuteBar", "c"),
            ("dailyBar", "c"),
        ):
            item = payload.get(container, {}) if isinstance(payload, dict) else {}
            value = item.get(field_name) if isinstance(item, dict) else None
            if value is not None and float(value) > 0:
                return float(value)
        raise RuntimeError(f"Alpaca did not return a price for {symbol}")

    def _get(self, url: str, params: Mapping[str, Any]) -> Any:
        return self.transport.request(
            "GET", url, headers=self._headers, params=params
        )

    @property
    def _headers(self) -> Mapping[str, str]:
        return {
            "APCA-API-KEY-ID": self.config.api_key,
            "APCA-API-SECRET-KEY": self.config.secret_key,
        }


def _absolute_delta(quote: OptionQuote) -> float | None:
    return None if quote.delta is None else abs(float(quote.delta))


def _leg_key(candidate: OptionOrderCandidate) -> tuple[str, ...]:
    return tuple(leg.contract.symbol for leg in candidate.legs)


def _contract_from_alpaca(item: Mapping[str, Any]) -> OptionContract:
    right = OptionRight(str(item.get("type", "")).lower())
    return OptionContract(
        symbol=str(item["symbol"]),
        underlying=str(item.get("underlying_symbol") or item.get("root_symbol")),
        right=right,
        strike=float(item["strike_price"]),
        expiration=date.fromisoformat(str(item["expiration_date"])),
        multiplier=int(item.get("size") or 100),
        tradable=bool(item.get("tradable", True)),
        open_interest=int(float(item.get("open_interest") or 0)),
    )


def _contract_from_occ_symbol(symbol: str) -> OptionContract:
    if len(symbol) < 16:
        raise ValueError("invalid OCC option symbol")
    suffix = symbol[-15:]
    underlying = symbol[:-15]
    expiration = date(
        2000 + int(suffix[0:2]), int(suffix[2:4]), int(suffix[4:6])
    )
    right = OptionRight.CALL if suffix[6] == "C" else OptionRight.PUT
    strike = int(suffix[7:15]) / 1000
    return OptionContract(symbol, underlying, right, strike, expiration)


def _quote_from_alpaca(
    contract: OptionContract,
    snapshot: Mapping[str, Any],
    fallback_time: pd.Timestamp,
) -> OptionQuote | None:
    latest_quote = snapshot.get("latestQuote") or snapshot.get("latest_quote") or {}
    bid = latest_quote.get("bp") or latest_quote.get("bid_price")
    ask = latest_quote.get("ap") or latest_quote.get("ask_price")
    if bid is None or ask is None or float(ask) <= 0 or float(ask) < float(bid):
        return None
    latest_trade = snapshot.get("latestTrade") or snapshot.get("latest_trade") or {}
    greeks = snapshot.get("greeks") or {}
    daily_bar = snapshot.get("dailyBar") or snapshot.get("daily_bar") or {}
    raw_timestamp = (
        latest_quote.get("t")
        or latest_quote.get("timestamp")
        or latest_trade.get("t")
        or latest_trade.get("timestamp")
    )
    timestamp = fallback_time if raw_timestamp is None else pd.Timestamp(raw_timestamp)
    if timestamp.tz is None:
        timestamp = timestamp.tz_localize("UTC")
    return OptionQuote(
        contract=contract,
        bid=float(bid),
        ask=float(ask),
        timestamp=timestamp,
        last=_optional_float(latest_trade.get("p") or latest_trade.get("price")),
        delta=_optional_float(greeks.get("delta")),
        gamma=_optional_float(greeks.get("gamma")),
        theta=_optional_float(greeks.get("theta")),
        vega=_optional_float(greeks.get("vega")),
        implied_volatility=_optional_float(
            snapshot.get("impliedVolatility")
            or snapshot.get("implied_volatility")
        ),
        volume=int(float(daily_bar.get("v") or daily_bar.get("volume") or 0)),
    )


def _optional_float(value: Any) -> float | None:
    return None if value is None else float(value)
