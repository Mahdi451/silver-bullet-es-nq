from __future__ import annotations

import pandas as pd

from silver_bullet import (
    Direction,
    FairValueGap,
    LiquidityKind,
    LiquidityLevel,
    TradeSignal,
    UnusualWhalesClient,
    UnusualWhalesConfig,
)


def _signal():
    timestamp = pd.Timestamp("2024-01-08 10:00", tz="America/New_York")
    gap = FairValueGap(Direction.LONG, 99, 100, timestamp, timestamp, timestamp)
    target = LiquidityLevel("PDH:test", LiquidityKind.PDH, 110, "test", timestamp)
    return TradeSignal(
        "ES",
        "ny_am",
        Direction.LONG,
        timestamp,
        timestamp + pd.Timedelta(minutes=1),
        target,
        gap,
        gap,
        timestamp,
        95,
        94.75,
        "ESH4",
    )


def test_unusual_whales_client_sends_auth_headers_and_query_params():
    calls = []

    def transport(method, url, headers, params, timeout):
        calls.append((method, url, headers, params, timeout))
        return {"data": []}

    client = UnusualWhalesClient(
        UnusualWhalesConfig("token", base_url="https://example.test", timeout_seconds=3),
        transport=transport,
    )

    payload = client.flow_alerts("spy", min_premium=50_000)

    assert payload == {"data": []}
    method, url, headers, params, timeout = calls[0]
    assert method == "GET"
    assert url == "https://example.test/api/option-trades/flow-alerts"
    assert headers["Authorization"] == "Bearer token"
    assert headers["UW-CLIENT-API-ID"] == "100001"
    assert params == {"ticker_symbol": "SPY", "min_premium": 50_000}
    assert timeout == 3


def test_unusual_whales_context_scores_flow_premium_bias():
    def transport(method, url, headers, params, timeout):
        return {
            "data": [
                {"is_call": True, "is_ask_side": True, "premium": 75_000},
                {"is_put": True, "is_ask_side": True, "premium": 25_000},
                {"option_type": "put", "side": "bid", "premium": 25_000},
            ]
        }

    client = UnusualWhalesClient(UnusualWhalesConfig("token"), transport=transport)
    signal = _signal()

    context = client.context_for_signal(
        signal,
        proxy_symbol="SPY",
        observed_at=signal.signal_time,
        min_premium=10_000,
    )

    assert context.instrument == "ES"
    assert context.proxy_symbol == "SPY"
    assert context.bullish_score == 0.8
    assert context.bearish_score == 0.2
    assert context.metadata["flow_alert_count"] == 3
