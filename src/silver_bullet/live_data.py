"""Live and replayable market-data boundaries for futures and proxy instruments."""

from __future__ import annotations

import asyncio
import inspect
import os
from collections.abc import AsyncIterator, Awaitable, Callable, Iterable, Mapping
from dataclasses import dataclass, field, replace
from enum import Enum
from typing import Any

import pandas as pd

from .models import Bar, NEW_YORK


class FeedState(str, Enum):
    STOPPED = "stopped"
    CONNECTING = "connecting"
    LIVE = "live"
    BACKOFF = "backoff"
    FAILED = "failed"


@dataclass(frozen=True, slots=True)
class ReconnectPolicy:
    initial_delay_seconds: float = 1.0
    maximum_delay_seconds: float = 30.0
    maximum_attempts: int | None = None

    def __post_init__(self) -> None:
        if self.initial_delay_seconds < 0:
            raise ValueError("initial reconnect delay cannot be negative")
        if self.maximum_delay_seconds < self.initial_delay_seconds:
            raise ValueError("maximum reconnect delay cannot be below initial delay")
        if self.maximum_attempts is not None and self.maximum_attempts < 0:
            raise ValueError("maximum reconnect attempts cannot be negative")

    def delay(self, attempt: int) -> float:
        return min(
            self.maximum_delay_seconds,
            self.initial_delay_seconds * (2 ** max(0, attempt - 1)),
        )


@dataclass(frozen=True, slots=True)
class FeedHealth:
    provider: str
    symbols: tuple[str, ...]
    state: FeedState = FeedState.STOPPED
    started_at: pd.Timestamp | None = None
    last_message_at: pd.Timestamp | None = None
    last_bar_at: pd.Timestamp | None = None
    message_count: int = 0
    bar_count: int = 0
    dropped_count: int = 0
    reconnect_count: int = 0
    last_error: str | None = None

    def is_stale(self, as_of: pd.Timestamp, maximum_age_seconds: float) -> bool:
        if as_of.tz is None:
            raise ValueError("as_of must be timezone-aware")
        if self.last_message_at is None:
            return True
        return (as_of - self.last_message_at).total_seconds() > maximum_age_seconds


@dataclass(frozen=True, slots=True)
class ProxyBar:
    """Completed one-minute bar for an equity or ETF proxy."""

    timestamp: pd.Timestamp
    symbol: str
    open: float
    high: float
    low: float
    close: float
    volume: float

    def __post_init__(self) -> None:
        if self.timestamp.tz is None:
            raise ValueError("proxy bar timestamp must be timezone-aware")
        if not self.symbol:
            raise ValueError("proxy bar symbol is required")
        if self.high < max(self.open, self.close, self.low):
            raise ValueError("proxy bar high is inconsistent")
        if self.low > min(self.open, self.close, self.high):
            raise ValueError("proxy bar low is inconsistent")
        if self.volume < 0:
            raise ValueError("proxy bar volume cannot be negative")


@dataclass(slots=True)
class LatestPriceBook:
    """Small in-memory view used by selection, risk checks, and monitoring."""

    futures: dict[str, Bar] = field(default_factory=dict)
    proxies: dict[str, ProxyBar] = field(default_factory=dict)

    def update(self, bar: Bar | ProxyBar) -> None:
        if isinstance(bar, Bar):
            self.futures[bar.instrument.upper()] = bar
        else:
            self.proxies[bar.symbol.upper()] = bar

    def price(self, symbol: str) -> float | None:
        normalized = symbol.upper()
        item = self.futures.get(normalized) or self.proxies.get(normalized)
        return None if item is None else item.close


BarCallback = Callable[[Bar], Awaitable[None] | None]
ProxyBarCallback = Callable[[ProxyBar], Awaitable[None] | None]
AsyncSleep = Callable[[float], Awaitable[None]]


@dataclass(frozen=True, slots=True)
class DatabentoLiveConfig:
    symbols: Mapping[str, str] = field(
        default_factory=lambda: {"ES": "ES.c.0", "NQ": "NQ.c.0"}
    )
    dataset: str = "GLBX.MDP3"
    schema: str = "ohlcv-1m"
    input_symbology: str = "continuous"
    replay_start: str | pd.Timestamp | None = None
    heartbeat_interval_seconds: int = 30
    reconnect: ReconnectPolicy = field(default_factory=ReconnectPolicy)

    def __post_init__(self) -> None:
        normalized = {key.upper() for key in self.symbols}
        if not normalized or not normalized <= {"ES", "NQ"}:
            raise ValueError("Databento symbols must map ES and/or NQ")
        if self.schema != "ohlcv-1m":
            raise ValueError("the live strategy requires completed ohlcv-1m bars")
        if self.heartbeat_interval_seconds <= 0:
            raise ValueError("heartbeat interval must be positive")


class DatabentoLiveFeed:
    """Normalize Databento ``ohlcv-1m`` records into canonical futures bars."""

    def __init__(
        self,
        config: DatabentoLiveConfig | None = None,
        *,
        client_factory: Callable[[], Any] | None = None,
        sleep: AsyncSleep = asyncio.sleep,
        clock: Callable[[], pd.Timestamp] | None = None,
    ) -> None:
        self.config = config or DatabentoLiveConfig()
        self._client_factory = client_factory or self._environment_client
        self._sleep = sleep
        self._clock = clock or _utc_now
        self._stop = asyncio.Event()
        self._health = FeedHealth(
            "databento", tuple(symbol.upper() for symbol in self.config.symbols)
        )
        self._contracts: dict[int, str] = {}
        self._last_timestamp: dict[str, pd.Timestamp] = {}
        self._client: Any = None

    @property
    def health(self) -> FeedHealth:
        return self._health

    def stop(self) -> None:
        self._stop.set()
        client = self._client
        if client is not None:
            stopper = getattr(client, "stop", None)
            if callable(stopper):
                stopper()

    async def bars(self) -> AsyncIterator[Bar]:
        queue: asyncio.Queue[Bar | None] = asyncio.Queue()

        async def put(bar: Bar) -> None:
            await queue.put(bar)

        task = asyncio.create_task(self.run(put))
        try:
            while True:
                if task.done() and queue.empty():
                    await task
                    break
                try:
                    item = await asyncio.wait_for(queue.get(), timeout=0.1)
                except TimeoutError:
                    continue
                if item is not None:
                    yield item
        finally:
            self.stop()
            if not task.done():
                await task

    async def run(self, callback: BarCallback) -> None:
        self._stop.clear()
        attempt = 0
        while not self._stop.is_set():
            self._health = replace(
                self._health,
                state=FeedState.CONNECTING,
                started_at=self._health.started_at or self._clock(),
            )
            try:
                client = self._client_factory()
                self._client = client
                subscribe_kwargs: dict[str, Any] = {
                    "dataset": self.config.dataset,
                    "schema": self.config.schema,
                    "stype_in": self.config.input_symbology,
                    "symbols": list(self.config.symbols.values()),
                }
                if self.config.replay_start is not None:
                    subscribe_kwargs["start"] = self.config.replay_start
                client.subscribe(**subscribe_kwargs)
                self._health = replace(
                    self._health, state=FeedState.LIVE, last_error=None
                )
                async for record in client:
                    if self._stop.is_set():
                        break
                    self._health = replace(
                        self._health,
                        last_message_at=self._clock(),
                        message_count=self._health.message_count + 1,
                    )
                    bar = self._normalize_record(record)
                    if bar is not None:
                        self._health = replace(
                            self._health,
                            last_bar_at=self._clock(),
                            bar_count=self._health.bar_count + 1,
                        )
                        result = callback(bar)
                        if inspect.isawaitable(result):
                            await result
                if self._stop.is_set():
                    break
                raise ConnectionError("Databento live stream ended")
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                attempt += 1
                if (
                    self.config.reconnect.maximum_attempts is not None
                    and attempt > self.config.reconnect.maximum_attempts
                ):
                    self._health = replace(
                        self._health, state=FeedState.FAILED, last_error=str(exc)
                    )
                    raise
                self._health = replace(
                    self._health,
                    state=FeedState.BACKOFF,
                    reconnect_count=self._health.reconnect_count + 1,
                    last_error=str(exc),
                )
                await self._sleep(self.config.reconnect.delay(attempt))
            finally:
                self._client = None
        self._health = replace(self._health, state=FeedState.STOPPED)

    def _normalize_record(self, record: Any) -> Bar | None:
        if hasattr(record, "stype_out_symbol") and hasattr(record, "instrument_id"):
            contract = str(record.stype_out_symbol)
            self._contracts[int(record.instrument_id)] = contract
            return None
        required = ("ts_event", "instrument_id", "open", "high", "low", "close")
        if not all(hasattr(record, name) for name in required):
            return None
        contract = self._contracts.get(int(record.instrument_id))
        explicit_symbol = getattr(record, "symbol", None)
        root = _futures_root(contract or explicit_symbol)
        if root is None or root not in {key.upper() for key in self.config.symbols}:
            self._drop()
            return None
        timestamp = _timestamp_from_nanoseconds(record.ts_event)
        previous = self._last_timestamp.get(root)
        if previous is not None and timestamp <= previous:
            self._drop()
            return None
        self._last_timestamp[root] = timestamp
        return Bar(
            timestamp=timestamp,
            instrument=root,
            open=_databento_price(record, "open"),
            high=_databento_price(record, "high"),
            low=_databento_price(record, "low"),
            close=_databento_price(record, "close"),
            volume=float(getattr(record, "volume", 0)),
            contract=contract,
        )

    def _drop(self) -> None:
        self._health = replace(
            self._health, dropped_count=self._health.dropped_count + 1
        )

    def _environment_client(self) -> Any:
        if not os.environ.get("DATABENTO_API_KEY"):
            raise RuntimeError("DATABENTO_API_KEY is required for live futures data")
        try:
            import databento as db
        except ImportError as exc:  # pragma: no cover - optional dependency
            raise RuntimeError("install the live extra to use Databento") from exc
        return db.Live(heartbeat_interval_s=self.config.heartbeat_interval_seconds)


@dataclass(frozen=True, slots=True)
class AlpacaLiveConfig:
    symbols: tuple[str, ...] = ("SPY", "QQQ")
    feed: str = "iex"
    reconnect: ReconnectPolicy = field(default_factory=ReconnectPolicy)

    def __post_init__(self) -> None:
        if not self.symbols or any(not symbol for symbol in self.symbols):
            raise ValueError("at least one proxy symbol is required")


class AlpacaProxyLiveFeed:
    """Normalize Alpaca minute-bar stream events for SPY/QQQ monitoring."""

    def __init__(
        self,
        config: AlpacaLiveConfig | None = None,
        *,
        stream_factory: Callable[[], Any] | None = None,
        sleep: AsyncSleep = asyncio.sleep,
        clock: Callable[[], pd.Timestamp] | None = None,
    ) -> None:
        self.config = config or AlpacaLiveConfig()
        self._stream_factory = stream_factory or self._environment_stream
        self._sleep = sleep
        self._clock = clock or _utc_now
        self._stop = asyncio.Event()
        self._health = FeedHealth(
            "alpaca", tuple(symbol.upper() for symbol in self.config.symbols)
        )
        self._last_timestamp: dict[str, pd.Timestamp] = {}
        self._stream: Any = None

    @property
    def health(self) -> FeedHealth:
        return self._health

    def stop(self) -> None:
        self._stop.set()
        stream = self._stream
        if stream is not None:
            stopper = getattr(stream, "stop", None)
            if callable(stopper):
                result = stopper()
                if inspect.isawaitable(result):
                    try:
                        asyncio.get_running_loop().create_task(result)
                    except RuntimeError:
                        pass

    async def run(self, callback: ProxyBarCallback) -> None:
        self._stop.clear()
        attempt = 0
        while not self._stop.is_set():
            self._health = replace(
                self._health,
                state=FeedState.CONNECTING,
                started_at=self._health.started_at or self._clock(),
            )
            try:
                stream = self._stream_factory()
                self._stream = stream

                async def handler(raw_bar: Any) -> None:
                    self._health = replace(
                        self._health,
                        last_message_at=self._clock(),
                        message_count=self._health.message_count + 1,
                    )
                    bar = self._normalize_bar(raw_bar)
                    if bar is None:
                        return
                    self._health = replace(
                        self._health,
                        last_bar_at=self._clock(),
                        bar_count=self._health.bar_count + 1,
                    )
                    result = callback(bar)
                    if inspect.isawaitable(result):
                        await result

                stream.subscribe_bars(handler, *self.config.symbols)
                self._health = replace(
                    self._health, state=FeedState.LIVE, last_error=None
                )
                if inspect.iscoroutinefunction(stream.run):
                    await stream.run()
                else:
                    await asyncio.to_thread(stream.run)
                if self._stop.is_set():
                    break
                raise ConnectionError("Alpaca proxy stream ended")
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                attempt += 1
                if (
                    self.config.reconnect.maximum_attempts is not None
                    and attempt > self.config.reconnect.maximum_attempts
                ):
                    self._health = replace(
                        self._health, state=FeedState.FAILED, last_error=str(exc)
                    )
                    raise
                self._health = replace(
                    self._health,
                    state=FeedState.BACKOFF,
                    reconnect_count=self._health.reconnect_count + 1,
                    last_error=str(exc),
                )
                await self._sleep(self.config.reconnect.delay(attempt))
            finally:
                self._stream = None
        self._health = replace(self._health, state=FeedState.STOPPED)

    def _normalize_bar(self, raw_bar: Any) -> ProxyBar | None:
        symbol = str(getattr(raw_bar, "symbol", "")).upper()
        if symbol not in {item.upper() for item in self.config.symbols}:
            self._drop()
            return None
        timestamp = pd.Timestamp(getattr(raw_bar, "timestamp"))
        if timestamp.tz is None:
            raise ValueError("Alpaca bar timestamp must be timezone-aware")
        timestamp = timestamp.tz_convert(NEW_YORK)
        previous = self._last_timestamp.get(symbol)
        if previous is not None and timestamp <= previous:
            self._drop()
            return None
        self._last_timestamp[symbol] = timestamp
        return ProxyBar(
            timestamp=timestamp,
            symbol=symbol,
            open=float(raw_bar.open),
            high=float(raw_bar.high),
            low=float(raw_bar.low),
            close=float(raw_bar.close),
            volume=float(raw_bar.volume),
        )

    def _drop(self) -> None:
        self._health = replace(
            self._health, dropped_count=self._health.dropped_count + 1
        )

    def _environment_stream(self) -> Any:
        api_key = os.environ.get("ALPACA_API_KEY")
        secret_key = os.environ.get("ALPACA_SECRET_KEY")
        if not api_key or not secret_key:
            raise RuntimeError("ALPACA_API_KEY and ALPACA_SECRET_KEY are required")
        try:
            from alpaca.data.live import StockDataStream
        except ImportError as exc:  # pragma: no cover - optional dependency
            raise RuntimeError("install the live extra to use Alpaca streams") from exc
        return StockDataStream(api_key, secret_key, feed=self.config.feed)


class ReplayLiveFeed:
    """Replay canonical bars through the same callback used by a live feed."""

    def __init__(self, bars: Iterable[Bar]) -> None:
        self._bars = tuple(bars)
        self._health = FeedHealth(
            "replay", tuple(sorted({bar.instrument for bar in self._bars}))
        )

    @property
    def health(self) -> FeedHealth:
        return self._health

    async def run(self, callback: BarCallback) -> None:
        self._health = replace(
            self._health, state=FeedState.LIVE, started_at=_utc_now()
        )
        for bar in self._bars:
            now = _utc_now()
            self._health = replace(
                self._health,
                last_message_at=now,
                last_bar_at=now,
                message_count=self._health.message_count + 1,
                bar_count=self._health.bar_count + 1,
            )
            result = callback(bar)
            if inspect.isawaitable(result):
                await result
        self._health = replace(self._health, state=FeedState.STOPPED)


def _futures_root(symbol: Any) -> str | None:
    if symbol is None:
        return None
    normalized = str(symbol).upper()
    for root in ("ES", "NQ"):
        if normalized.startswith(root):
            return root
    return None


def _databento_price(record: Any, field_name: str) -> float:
    pretty = getattr(record, f"pretty_{field_name}", None)
    if pretty is not None:
        return float(pretty)
    value = getattr(record, field_name)
    if isinstance(value, int) and abs(value) >= 10_000_000:
        return value / 1_000_000_000
    return float(value)


def _timestamp_from_nanoseconds(value: Any) -> pd.Timestamp:
    timestamp = pd.Timestamp(int(value), unit="ns", tz="UTC")
    return timestamp.tz_convert(NEW_YORK)


def _utc_now() -> pd.Timestamp:
    return pd.Timestamp.now(tz="UTC")
