from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pandas as pd
import pytest

from silver_bullet.live_data import (
    AlpacaLiveConfig,
    AlpacaProxyLiveFeed,
    DatabentoLiveConfig,
    DatabentoLiveFeed,
    FeedState,
    LatestPriceBook,
    ReconnectPolicy,
    ReplayLiveFeed,
)
from silver_bullet.models import Bar


def _futures_bar(minute: int = 0) -> Bar:
    return Bar(
        pd.Timestamp(f"2024-01-08 10:{minute:02d}", tz="America/New_York"),
        "ES",
        4800,
        4801,
        4799,
        4800.5,
        10,
        "ESH4",
    )


class FakeDatabentoClient:
    def __init__(self, records, error=None):
        self.records = list(records)
        self.error = error
        self.subscribed = None

    def subscribe(self, **kwargs):
        self.subscribed = kwargs

    def __aiter__(self):
        return self._iterate()

    async def _iterate(self):
        for record in self.records:
            yield record
        if self.error:
            raise self.error


def test_databento_feed_normalizes_mapping_and_drops_duplicate_bar():
    asyncio.run(_test_databento_feed_normalizes_mapping_and_drops_duplicate_bar())


async def _test_databento_feed_normalizes_mapping_and_drops_duplicate_bar():
    timestamp = pd.Timestamp("2024-01-08 15:00:00Z").value
    mapping = SimpleNamespace(instrument_id=7, stype_out_symbol="ESH4")
    record = SimpleNamespace(
        instrument_id=7,
        ts_event=timestamp,
        open=4_800_000_000_000,
        high=4_801_000_000_000,
        low=4_799_000_000_000,
        close=4_800_500_000_000,
        volume=42,
    )
    client = FakeDatabentoClient([mapping, record, record])
    config = DatabentoLiveConfig(
        symbols={"ES": "ES.c.0"},
        reconnect=ReconnectPolicy(0, 0, maximum_attempts=0),
    )
    feed = DatabentoLiveFeed(config, client_factory=lambda: client)
    received = []

    with pytest.raises(ConnectionError, match="stream ended"):
        await feed.run(received.append)

    assert len(received) == 1
    assert received[0].instrument == "ES"
    assert received[0].contract == "ESH4"
    assert received[0].close == 4800.5
    assert received[0].timestamp == pd.Timestamp(
        "2024-01-08 10:00", tz="America/New_York"
    )
    assert feed.health.bar_count == 1
    assert feed.health.dropped_count == 1
    assert feed.health.state is FeedState.FAILED
    assert client.subscribed["schema"] == "ohlcv-1m"


def test_databento_feed_reconnects_after_transport_error():
    asyncio.run(_test_databento_feed_reconnects_after_transport_error())


async def _test_databento_feed_reconnects_after_transport_error():
    timestamp = pd.Timestamp("2024-01-08 15:00:00Z").value
    mapping = SimpleNamespace(instrument_id=9, stype_out_symbol="NQH4")
    record = SimpleNamespace(
        instrument_id=9,
        ts_event=timestamp,
        open=17_000.0,
        high=17_001.0,
        low=16_999.0,
        close=17_000.5,
        volume=3,
    )
    clients = iter(
        [
            FakeDatabentoClient([], ConnectionError("temporary")),
            FakeDatabentoClient([mapping, record]),
        ]
    )
    sleeps = []

    async def no_wait(delay):
        sleeps.append(delay)

    config = DatabentoLiveConfig(
        symbols={"NQ": "NQ.c.0"},
        reconnect=ReconnectPolicy(0.25, 1, maximum_attempts=1),
    )
    feed = DatabentoLiveFeed(
        config, client_factory=lambda: next(clients), sleep=no_wait
    )
    received = []

    with pytest.raises(ConnectionError, match="stream ended"):
        await feed.run(received.append)

    assert [bar.instrument for bar in received] == ["NQ"]
    assert sleeps == [0.25]
    assert feed.health.reconnect_count == 1


def test_replay_feed_uses_live_callback_contract():
    asyncio.run(_test_replay_feed_uses_live_callback_contract())


async def _test_replay_feed_uses_live_callback_contract():
    feed = ReplayLiveFeed([_futures_bar(0), _futures_bar(1)])
    received = []
    await feed.run(received.append)
    assert len(received) == 2
    assert feed.health.bar_count == 2
    assert feed.health.state is FeedState.STOPPED


def test_alpaca_proxy_normalization_and_latest_price_book():
    feed = AlpacaProxyLiveFeed(AlpacaLiveConfig(symbols=("SPY",)))
    raw = SimpleNamespace(
        symbol="SPY",
        timestamp=pd.Timestamp("2024-01-08 15:00:00Z"),
        open=470,
        high=471,
        low=469,
        close=470.5,
        volume=1000,
    )
    proxy = feed._normalize_bar(raw)
    assert proxy is not None
    book = LatestPriceBook()
    book.update(_futures_bar())
    book.update(proxy)
    assert book.price("ES") == 4800.5
    assert book.price("spy") == 470.5


def test_feed_health_staleness_requires_timezone_aware_clock():
    feed = ReplayLiveFeed([])
    with pytest.raises(ValueError, match="timezone-aware"):
        feed.health.is_stale(pd.Timestamp("2024-01-08"), 90)
