"""Unusual Whales REST boundary for external context snapshots."""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from typing import Any, Callable, Mapping
from urllib.parse import urlencode
from urllib.request import Request, urlopen

import pandas as pd

from .context import ExternalContextSnapshot
from .models import NEW_YORK, TradeSignal

RequestTransport = Callable[
    [str, str, Mapping[str, str], Mapping[str, Any], float], Mapping[str, Any]
]


@dataclass(frozen=True, slots=True)
class UnusualWhalesConfig:
    api_key: str
    base_url: str = "https://api.unusualwhales.com"
    client_api_id: str = "100001"
    timeout_seconds: float = 10.0

    @classmethod
    def from_environment(cls) -> "UnusualWhalesConfig":
        api_key = os.environ.get("UNUSUAL_WHALES_API_KEY")
        if not api_key:
            raise RuntimeError("UNUSUAL_WHALES_API_KEY is required")
        return cls(api_key)


class UnusualWhalesClient:
    """Fetch and normalize Unusual Whales data without coupling strategy code."""

    def __init__(
        self,
        config: UnusualWhalesConfig,
        *,
        transport: RequestTransport | None = None,
    ) -> None:
        self.config = config
        self._transport = transport or _urllib_transport

    def flow_alerts(self, ticker_symbol: str, **params: Any) -> Mapping[str, Any]:
        query = {"ticker_symbol": ticker_symbol.upper(), **params}
        return self._get("/api/option-trades/flow-alerts", query)

    def market_tide(self) -> Mapping[str, Any]:
        return self._get("/api/market/market-tide", {})

    def darkpool(self, ticker_symbol: str) -> Mapping[str, Any]:
        return self._get(f"/api/darkpool/{ticker_symbol.upper()}", {})

    def context_for_signal(
        self,
        signal: TradeSignal,
        *,
        proxy_symbol: str,
        observed_at: pd.Timestamp | None = None,
        min_premium: float | None = None,
        limit: int = 100,
    ) -> ExternalContextSnapshot:
        params: dict[str, Any] = {"limit": limit}
        if min_premium is not None:
            params["min_premium"] = min_premium
        payload = self.flow_alerts(proxy_symbol, **params)
        bullish, bearish, count = _score_flow_alerts(_payload_items(payload))
        return ExternalContextSnapshot(
            signal.instrument,
            proxy_symbol,
            observed_at or pd.Timestamp.now(tz=NEW_YORK),
            bullish_score=bullish,
            bearish_score=bearish,
            source="unusual_whales",
            metadata={
                "flow_alert_count": count,
                "min_premium": min_premium,
                "limit": limit,
            },
        )

    def _get(self, path: str, params: Mapping[str, Any]) -> Mapping[str, Any]:
        headers = {
            "Authorization": f"Bearer {self.config.api_key}",
            "UW-CLIENT-API-ID": self.config.client_api_id,
            "Accept": "application/json",
        }
        url = self.config.base_url.rstrip("/") + path
        return self._transport(
            "GET", url, headers, params, self.config.timeout_seconds
        )


def _urllib_transport(
    method: str,
    url: str,
    headers: Mapping[str, str],
    params: Mapping[str, Any],
    timeout: float,
) -> Mapping[str, Any]:
    query = {key: value for key, value in params.items() if value is not None}
    if query:
        url = f"{url}?{urlencode(query, doseq=True)}"
    request = Request(url, method=method, headers=dict(headers))
    with urlopen(request, timeout=timeout) as response:  # noqa: S310
        return json.loads(response.read().decode("utf-8"))


def _payload_items(payload: Mapping[str, Any]) -> list[Mapping[str, Any]]:
    data = payload.get("data", payload)
    if isinstance(data, Mapping):
        for key in ("items", "results", "alerts"):
            value = data.get(key)
            if isinstance(value, list):
                return [item for item in value if isinstance(item, Mapping)]
        return [data]
    if isinstance(data, list):
        return [item for item in data if isinstance(item, Mapping)]
    return []


def _score_flow_alerts(items: list[Mapping[str, Any]]) -> tuple[float, float, int]:
    bullish_premium = bearish_premium = 0.0
    for item in items:
        premium = _premium(item)
        bias = _flow_bias(item)
        if bias > 0:
            bullish_premium += premium
        elif bias < 0:
            bearish_premium += premium

    total = bullish_premium + bearish_premium
    if total <= 0:
        return 0.0, 0.0, len(items)
    return bullish_premium / total, bearish_premium / total, len(items)


def _premium(item: Mapping[str, Any]) -> float:
    for key in ("premium", "total_premium", "notional", "value"):
        value = item.get(key)
        if value is not None:
            try:
                return abs(float(value))
            except (TypeError, ValueError):
                pass
    try:
        return abs(float(item.get("price", 0)) * float(item.get("size", 0)) * 100)
    except (TypeError, ValueError):
        return 0.0


def _flow_bias(item: Mapping[str, Any]) -> int:
    sentiment = str(item.get("sentiment", item.get("rule_name", ""))).lower()
    if "bull" in sentiment:
        return 1
    if "bear" in sentiment:
        return -1

    is_call = bool(item.get("is_call") or str(item.get("option_type", "")).lower() == "call")
    is_put = bool(item.get("is_put") or str(item.get("option_type", "")).lower() == "put")
    ask_side = bool(item.get("is_ask_side") or str(item.get("side", "")).lower() == "ask")
    bid_side = bool(item.get("is_bid_side") or str(item.get("side", "")).lower() == "bid")
    if is_call and ask_side:
        return 1
    if is_put and ask_side:
        return -1
    if is_call and bid_side:
        return -1
    if is_put and bid_side:
        return 1
    return 0
