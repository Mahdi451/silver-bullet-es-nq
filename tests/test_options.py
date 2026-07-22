from __future__ import annotations

from datetime import date

import pandas as pd
import pytest

from silver_bullet.models import (
    Direction,
    FairValueGap,
    LiquidityKind,
    LiquidityLevel,
    TradeSignal,
)
from silver_bullet.options import (
    AlpacaOptionChainClient,
    AlpacaOptionChainConfig,
    OptionChainSnapshot,
    OptionContract,
    OptionQuote,
    OptionRight,
    OptionsProfile,
    OptionsSelectionConfig,
    OptionsSelector,
)


NOW = pd.Timestamp("2024-01-08 10:01", tz="America/New_York")


def _signal(
    direction: Direction = Direction.LONG,
    *,
    session: str = "ny_am",
    target: float = 111,
    origin: float = 100,
) -> TradeSignal:
    timestamp = NOW - pd.Timedelta(minutes=1)
    gap = FairValueGap(direction, 99, 100, timestamp, timestamp, timestamp)
    kind = LiquidityKind.PDH if direction is Direction.LONG else LiquidityKind.PDL
    level = LiquidityLevel("target", kind, target, "test", timestamp)
    return TradeSignal(
        "ES",
        session,
        direction,
        timestamp,
        timestamp + pd.Timedelta(minutes=1),
        level,
        gap,
        gap,
        timestamp,
        origin,
        95 if direction is Direction.LONG else 115,
        "ESH4",
    )


def _quote(
    symbol: str,
    right: OptionRight,
    strike: float,
    *,
    expiration: date = date(2024, 1, 8),
    bid: float = 4.8,
    ask: float = 5.0,
    delta: float = 0.70,
    gamma: float = 0.05,
    volume: int = 100,
    open_interest: int = 500,
) -> OptionQuote:
    contract = OptionContract(
        symbol,
        "SPY",
        right,
        strike,
        expiration,
        open_interest=open_interest,
    )
    return OptionQuote(
        contract,
        bid,
        ask,
        NOW,
        delta=delta,
        gamma=gamma,
        volume=volume,
    )


def _chain(*quotes: OptionQuote) -> OptionChainSnapshot:
    return OptionChainSnapshot("SPY", 470, NOW, tuple(quotes), "fake")


def test_high_delta_selector_maps_direction_filters_and_sizes_risk():
    good = _quote("SPY240108C00465000", OptionRight.CALL, 465)
    wrong_direction = _quote(
        "SPY240108P00475000", OptionRight.PUT, 475, delta=-0.70
    )
    wide = _quote(
        "SPY240108C00466000", OptionRight.CALL, 466, bid=3, ask=5
    )
    selector = OptionsSelector()

    candidates = selector.select(
        _signal(),
        _chain(good, wrong_direction, wide),
        as_of=NOW,
        risk_budget=1_200,
    )

    assert len(candidates) == 1
    candidate = candidates[0]
    assert candidate.profile is OptionsProfile.HIGH_DELTA_LONG
    assert candidate.legs[0].contract.symbol == good.contract.symbol
    assert candidate.quantity == 2
    assert candidate.estimated_max_loss == 1_000


def test_short_signal_selects_put_using_absolute_delta():
    put = _quote(
        "SPY240108P00475000", OptionRight.PUT, 475, delta=-0.69
    )
    candidates = OptionsSelector().select(
        _signal(Direction.SHORT, target=89, origin=100),
        _chain(put),
        as_of=NOW,
        risk_budget=500,
    )
    assert candidates[0].legs[0].contract.right is OptionRight.PUT


def test_debit_spread_uses_atm_long_and_dol_short_strike():
    long_leg = _quote(
        "SPY240108C00470000",
        OptionRight.CALL,
        470,
        bid=2.0,
        ask=2.1,
    )
    short_leg = _quote(
        "SPY240108C00472000",
        OptionRight.CALL,
        472,
        bid=1.0,
        ask=1.05,
    )
    candidate = OptionsSelector().select(
        _signal(),
        _chain(long_leg, short_leg),
        profile=OptionsProfile.DOL_DEBIT_SPREAD,
        as_of=NOW,
        risk_budget=500,
        proxy_target=472.2,
    )[0]

    assert [leg.side for leg in candidate.legs] == ["buy", "sell"]
    assert [leg.contract.strike for leg in candidate.legs] == [470, 472]
    assert candidate.limit_price == 1.1
    assert candidate.estimated_max_profit is not None
    assert candidate.estimated_max_profit > candidate.estimated_max_loss / 2


def test_momentum_profile_requires_session_delivery_and_zero_dte():
    momentum = _quote(
        "SPY240108C00472000", OptionRight.CALL, 472, delta=0.41
    )
    candidate = OptionsSelector().select(
        _signal(session="macro", target=110, origin=100),
        _chain(momentum),
        profile=OptionsProfile.MOMENTUM_0DTE,
        as_of=NOW,
        risk_budget=500,
    )[0]
    assert candidate.profile is OptionsProfile.MOMENTUM_0DTE

    with pytest.raises(ValueError, match="not allowed"):
        OptionsSelector().select(
            _signal(session="london"),
            _chain(momentum),
            profile=OptionsProfile.MOMENTUM_0DTE,
            as_of=NOW,
            risk_budget=500,
        )


def test_selector_rejects_stale_signal_and_low_liquidity():
    low_volume = _quote(
        "SPY240108C00465000", OptionRight.CALL, 465, volume=1
    )
    assert (
        OptionsSelector().select(
            _signal(), _chain(low_volume), as_of=NOW, risk_budget=500
        )
        == ()
    )
    stale = NOW + pd.Timedelta(minutes=3)
    with pytest.raises(ValueError, match="too stale"):
        OptionsSelector().select(
            _signal(), _chain(low_volume), as_of=stale, risk_budget=500
        )


class FakeTransport:
    def __init__(self):
        self.calls = []

    def request(self, method, url, *, headers=None, params=None, body=None):
        self.calls.append((method, url, headers, dict(params or {}), body))
        if url.endswith("/v2/options/contracts"):
            return {
                "option_contracts": [
                    {
                        "symbol": "SPY240108C00470000",
                        "underlying_symbol": "SPY",
                        "type": "call",
                        "strike_price": "470",
                        "expiration_date": "2024-01-08",
                        "size": "100",
                        "tradable": True,
                        "open_interest": "1200",
                    }
                ],
                "next_page_token": None,
            }
        if "/options/snapshots/SPY" in url:
            return {
                "snapshots": {
                    "SPY240108C00470000": {
                        "latestQuote": {
                            "bp": 4.8,
                            "ap": 5.0,
                            "t": "2024-01-08T15:01:00Z",
                        },
                        "latestTrade": {"p": 4.9},
                        "greeks": {
                            "delta": 0.7,
                            "gamma": 0.05,
                            "theta": -0.1,
                            "vega": 0.2,
                        },
                        "impliedVolatility": 0.22,
                        "dailyBar": {"v": 800},
                    }
                },
                "next_page_token": None,
            }
        raise AssertionError(url)


def test_alpaca_chain_client_combines_contracts_quotes_and_greeks():
    transport = FakeTransport()
    client = AlpacaOptionChainClient(
        AlpacaOptionChainConfig("key", "secret"), transport=transport
    )
    chain = client.fetch("spy", as_of=NOW, underlying_price=470)

    assert chain.provider == "alpaca"
    assert len(chain.quotes) == 1
    quote = chain.quotes[0]
    assert quote.delta == 0.7
    assert quote.volume == 800
    assert quote.contract.open_interest == 1200
    assert all(call[2]["APCA-API-KEY-ID"] == "key" for call in transport.calls)


def test_selector_configuration_rejects_invalid_spread_limit():
    with pytest.raises(ValueError, match="between zero and one"):
        OptionsSelectionConfig(max_spread_pct=1.5)
