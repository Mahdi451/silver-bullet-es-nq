"""Integrated live strategy, market-data, execution, and safety runtime."""

from __future__ import annotations

import asyncio
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Protocol

import pandas as pd

from .broker import (
    BrokerSnapshot,
    ExecutionReconciler,
    ExecutionStore,
    LiveRiskGate,
    OrderSubmission,
    ReconciliationReport,
    SyntheticExitManager,
)
from .context import SignalContextGate
from .dashboard import OperatorDashboard, OperatorState
from .live_data import FeedHealth, LatestPriceBook, ProxyBar
from .models import Bar, TradeSignal
from .options import OptionsProfile, OptionsSelector
from .strategy import SilverBulletStrategy


class ExecutionRoute(str, Enum):
    OPTIONS = "options"
    FUTURES = "futures"


class RuntimeMode(str, Enum):
    DRY_RUN = "dry_run"
    PAPER = "paper"
    LIVE = "live"


class EntryTiming(str, Enum):
    LIVE_BOUNDARY = "live_boundary"
    NEXT_BAR_OPEN = "next_bar_open"


@dataclass(frozen=True, slots=True)
class LiveRuntimeConfig:
    route: ExecutionRoute = ExecutionRoute.OPTIONS
    mode: RuntimeMode = RuntimeMode.DRY_RUN
    options_profile: OptionsProfile = OptionsProfile.HIGH_DELTA_LONG
    options_risk_budget: float = 500.0
    proxy_symbols: Mapping[str, str] = field(
        default_factory=lambda: {"ES": "SPY", "NQ": "QQQ"}
    )
    proxy_maximum_age_seconds: float = 90.0
    proxy_wait_seconds: float = 2.0
    external_context_enabled: bool = False
    context_minimum_premium: float | None = None
    context_limit: int = 100
    reconciliation_interval_seconds: float | None = 30.0
    halt_on_error: bool = True
    live_trading_confirmed: bool = False
    entry_timing: EntryTiming = EntryTiming.LIVE_BOUNDARY
    maximum_entry_delay_seconds: float = 5.0
    exclusive_broker_account: bool = True

    def __post_init__(self) -> None:
        object.__setattr__(self, "route", ExecutionRoute(self.route))
        object.__setattr__(self, "mode", RuntimeMode(self.mode))
        object.__setattr__(
            self, "options_profile", OptionsProfile(self.options_profile)
        )
        object.__setattr__(self, "entry_timing", EntryTiming(self.entry_timing))
        object.__setattr__(
            self,
            "proxy_symbols",
            {
                str(key).upper(): str(value).upper()
                for key, value in self.proxy_symbols.items()
            },
        )
        if self.options_risk_budget <= 0:
            raise ValueError("options risk budget must be positive")
        normalized = {str(key).upper() for key in self.proxy_symbols}
        if not normalized or not normalized <= {"ES", "NQ"}:
            raise ValueError("proxy symbols must map ES and/or NQ")
        if any(not str(value).strip() for value in self.proxy_symbols.values()):
            raise ValueError("proxy symbols cannot be empty")
        if self.proxy_maximum_age_seconds < 0 or self.proxy_wait_seconds < 0:
            raise ValueError("proxy freshness settings cannot be negative")
        if self.context_minimum_premium is not None:
            if self.context_minimum_premium < 0:
                raise ValueError("context minimum premium cannot be negative")
        if self.context_limit < 1:
            raise ValueError("context limit must be positive")
        interval = self.reconciliation_interval_seconds
        if interval is not None and interval <= 0:
            raise ValueError("reconciliation interval must be positive")
        if self.mode is RuntimeMode.LIVE and not self.live_trading_confirmed:
            raise ValueError("live mode requires explicit live trading confirmation")
        if self.mode is not RuntimeMode.DRY_RUN and not self.halt_on_error:
            raise ValueError("paper/live mode requires fail-closed error handling")
        if self.maximum_entry_delay_seconds < 0:
            raise ValueError("maximum entry delay cannot be negative")

    @property
    def dry_run(self) -> bool:
        return self.mode is RuntimeMode.DRY_RUN


class FuturesFeed(Protocol):
    @property
    def health(self) -> FeedHealth: ...

    async def run(self, callback: Any) -> None: ...


class ProxyFeed(Protocol):
    @property
    def health(self) -> FeedHealth: ...

    async def run(self, callback: Any) -> None: ...


class SnapshotBroker(Protocol):
    name: str

    def snapshot(self, *, as_of: pd.Timestamp | None = None) -> BrokerSnapshot: ...


class OptionChainProvider(Protocol):
    def fetch(
        self,
        underlying: str,
        *,
        as_of: pd.Timestamp,
        underlying_price: float | None = None,
    ) -> Any: ...


class OptionBroker(SnapshotBroker, Protocol):
    def submit_option(self, candidate: Any, **kwargs: Any) -> OrderSubmission: ...

    def close_trade(self, trade: Any, reason: str) -> OrderSubmission: ...


class FuturesBroker(SnapshotBroker, Protocol):
    def submit_order(self, signal: TradeSignal, **kwargs: Any) -> OrderSubmission: ...


class ExternalContextProvider(Protocol):
    def context_for_signal(self, signal: TradeSignal, **kwargs: Any) -> Any: ...


class SignalRejected(RuntimeError):
    """A safe, expected signal rejection that should not halt market monitoring."""


class LiveRuntime:
    """Run completed bars through the strategy and guarded execution adapters."""

    def __init__(
        self,
        *,
        futures_feed: FuturesFeed,
        strategies: Mapping[str, SilverBulletStrategy],
        store: ExecutionStore,
        config: LiveRuntimeConfig | None = None,
        proxy_feed: ProxyFeed | None = None,
        option_chain: OptionChainProvider | None = None,
        options_selector: OptionsSelector | None = None,
        option_broker: OptionBroker | None = None,
        futures_broker: FuturesBroker | None = None,
        risk_gate: LiveRiskGate | None = None,
        context_provider: ExternalContextProvider | None = None,
        context_gate: SignalContextGate | None = None,
        state: OperatorState | None = None,
        dashboard: OperatorDashboard | None = None,
        reconciler: ExecutionReconciler | None = None,
        exit_manager: SyntheticExitManager | None = None,
        clock: Any = None,
    ) -> None:
        self.config = config or LiveRuntimeConfig()
        self.futures_feed = futures_feed
        self.proxy_feed = proxy_feed
        self.strategies = {
            str(symbol).upper(): strategy for symbol, strategy in strategies.items()
        }
        if not self.strategies:
            raise ValueError("at least one live strategy is required")
        if any(symbol not in {"ES", "NQ"} for symbol in self.strategies):
            raise ValueError("live strategies must be keyed by ES and/or NQ")
        for symbol, strategy in self.strategies.items():
            instrument = getattr(strategy, "instrument", symbol).upper()
            if instrument != symbol:
                raise ValueError("strategy key does not match its instrument")

        self.store = store
        self.option_chain = option_chain
        self.options_selector = options_selector or OptionsSelector()
        self.option_broker = option_broker
        self.futures_broker = futures_broker
        self.risk_gate = risk_gate or LiveRiskGate(store)
        if self.risk_gate.store is not store:
            raise ValueError("risk gate and runtime must share one execution store")
        self.context_provider = context_provider
        self.context_gate = context_gate or SignalContextGate()
        self._clock = clock or (lambda: pd.Timestamp.now(tz="UTC"))
        self.price_book = state.price_book if state else LatestPriceBook()
        if state is not None and state.store is not store:
            raise ValueError("operator state and runtime must share one execution store")
        if state is not None and state.mode != self.config.mode.value:
            raise ValueError("operator state mode does not match runtime mode")
        self.state = state or OperatorState(
            store,
            mode=self.config.mode.value,
            price_book=self.price_book,
            clock=self._clock,
        )
        self.dashboard = dashboard
        self.reconciler = reconciler or ExecutionReconciler(
            store, exclusive_account=self.config.exclusive_broker_account
        )
        if self.reconciler.store is not store:
            raise ValueError("reconciler and runtime must share one execution store")
        self._pending: dict[str, TradeSignal] = {}
        self._locks = {symbol: asyncio.Lock() for symbol in self.strategies}
        self._proxy_changed = asyncio.Condition()
        self._stop = asyncio.Event()
        self._running = False
        self._fatal_error: Exception | None = None

        if self.config.route is ExecutionRoute.OPTIONS:
            if self.option_chain is None or self.option_broker is None:
                raise ValueError(
                    "options routing requires an option chain and option broker"
                )
            brokers: dict[str, SnapshotBroker] = {
                self.option_broker.name: self.option_broker
            }
            self.exit_manager = exit_manager or SyntheticExitManager(
                store, {self.option_broker.name: self.option_broker}
            )
        else:
            if self.futures_broker is None:
                raise ValueError("futures routing requires a futures broker")
            brokers = {self.futures_broker.name: self.futures_broker}
            self.exit_manager = exit_manager
        if self.config.external_context_enabled and self.context_provider is None:
            raise ValueError("external context is enabled but no provider is configured")
        self.brokers = brokers
        for broker in brokers.values():
            broker_store = getattr(broker, "store", store)
            if broker_store is not store:
                raise ValueError("broker and runtime must share one execution store")
        if self.exit_manager is not None and self.exit_manager.store is not store:
            raise ValueError("exit manager and runtime must share one execution store")
        self._validate_broker_mode()

    @property
    def pending_signals(self) -> Mapping[str, TradeSignal]:
        return dict(self._pending)

    @property
    def running(self) -> bool:
        return self._running

    def warmup(self, bars: Mapping[str, Iterable[Bar]]) -> Mapping[str, int]:
        """Warm technical state while intentionally discarding historical signals."""
        if self._running:
            raise RuntimeError("strategy warmup must finish before the runtime starts")
        counts: dict[str, int] = {}
        discarded = 0
        for raw_symbol, source in bars.items():
            symbol = str(raw_symbol).upper()
            strategy = self.strategies.get(symbol)
            if strategy is None:
                raise ValueError(f"no live strategy configured for {symbol}")
            count = 0
            for bar in source:
                signal = strategy.on_bar(bar)
                count += 1
                discarded += signal is not None
            counts[symbol] = count
        self.state.event(
            "info",
            "strategy",
            "strategy warmup completed",
            {"bars": counts, "discarded_historical_signals": discarded},
        )
        return counts

    async def process_proxy_bar(self, bar: ProxyBar) -> None:
        self.price_book.update(bar)
        if self.proxy_feed is not None:
            self.state.update_feed("alpaca_proxy", self.proxy_feed.health)
        async with self._proxy_changed:
            self._proxy_changed.notify_all()

    async def process_futures_bar(self, bar: Bar) -> OrderSubmission | None:
        symbol = bar.instrument.upper()
        strategy = self.strategies.get(symbol)
        if strategy is None:
            raise ValueError(f"no live strategy configured for {symbol}")
        async with self._locks[symbol]:
            self.price_book.update(bar)
            self.state.update_feed("databento_futures", self.futures_feed.health)
            if (
                not self.config.dry_run
                and self.config.route is ExecutionRoute.OPTIONS
                and self.exit_manager is not None
            ):
                exits = await asyncio.to_thread(self.exit_manager.on_bar, bar)
                for submission in exits:
                    self.state.event(
                        "warning",
                        "execution",
                        "synthetic exit submitted",
                        {
                            "broker": submission.broker,
                            "order_id": submission.order_id,
                        },
                        timestamp=bar.timestamp,
                    )

            submission = None
            if self.config.entry_timing is EntryTiming.NEXT_BAR_OPEN:
                try:
                    submission = await self._execute_due_signal(symbol, bar)
                except SignalRejected as exc:
                    self._record_rejection(symbol, exc, bar.timestamp)
            signal = strategy.on_bar(bar)
            if signal is not None:
                if self.store.kill_switch.engaged:
                    self.state.event(
                        "warning",
                        "strategy",
                        "signal discarded while entries are halted",
                        {"instrument": symbol, "session": signal.session},
                        timestamp=bar.timestamp,
                    )
                elif (
                    self.config.entry_timing is EntryTiming.NEXT_BAR_OPEN
                    and symbol in self._pending
                ):
                    self.state.event(
                        "warning",
                        "strategy",
                        "signal discarded because an entry is already pending",
                        {"instrument": symbol, "session": signal.session},
                        timestamp=bar.timestamp,
                    )
                elif signal.execution_time <= bar.timestamp:
                    raise RuntimeError("strategy emitted a non-future execution time")
                elif self.config.entry_timing is EntryTiming.NEXT_BAR_OPEN:
                    self._pending[symbol] = signal
                    self.state.event(
                        "info",
                        "strategy",
                        "signal scheduled for the next minute open",
                        {
                            "instrument": symbol,
                            "session": signal.session,
                            "execution_time": signal.execution_time.isoformat(),
                        },
                        timestamp=bar.timestamp,
                    )
                else:
                    try:
                        submission = await self._execute_at_live_boundary(
                            signal, bar
                        )
                    except SignalRejected as exc:
                        self._record_rejection(symbol, exc, signal.execution_time)
            return submission

    async def reconcile_once(self) -> Mapping[str, ReconciliationReport]:
        reports: dict[str, ReconciliationReport] = {}
        for broker in self.brokers.values():
            snapshot = await asyncio.to_thread(broker.snapshot, as_of=self._clock())
            report = self.reconciler.reconcile(snapshot)
            self.state.update_broker(snapshot)
            self.state.update_reconciliation(report)
            reports[broker.name] = report
            self.state.event(
                "info" if report.healthy else "critical",
                "broker",
                "broker reconciliation completed",
                {
                    "broker": broker.name,
                    "healthy": report.healthy,
                    "issues": len(report.issues),
                },
                timestamp=snapshot.as_of,
            )
        return reports

    async def run(self) -> None:
        if self._running:
            raise RuntimeError("live runtime is already running")
        if self.config.route is ExecutionRoute.OPTIONS and self.proxy_feed is None:
            raise ValueError("options runtime requires a live proxy feed")
        self._running = True
        self._stop.clear()
        self._fatal_error = None
        tasks: list[asyncio.Task[Any]] = []
        dashboard_started = False
        self.state.event(
            "info",
            "runtime",
            "live runtime starting",
            {"mode": self.config.mode.value, "route": self.config.route.value},
        )
        try:
            if self.dashboard is not None:
                self.dashboard.start()
                dashboard_started = True
            await self.reconcile_once()
            tasks.append(
                asyncio.create_task(
                    self.futures_feed.run(self._consume_futures_bar),
                    name="silver-bullet-futures-feed",
                )
            )
            if self.proxy_feed is not None:
                tasks.append(
                    asyncio.create_task(
                        self.proxy_feed.run(self._consume_proxy_bar),
                        name="silver-bullet-proxy-feed",
                    )
                )
            if self.config.reconciliation_interval_seconds is not None:
                tasks.append(
                    asyncio.create_task(
                        self._reconciliation_loop(),
                        name="silver-bullet-reconciliation",
                    )
                )
            tasks.append(
                asyncio.create_task(
                    self._health_loop(), name="silver-bullet-feed-health"
                )
            )
            stop_task = asyncio.create_task(
                self._stop.wait(), name="silver-bullet-stop"
            )
            tasks.append(stop_task)
            feed_tasks = [task for task in tasks if "feed" in task.get_name()]
            done, _ = await asyncio.wait(
                [*feed_tasks, stop_task], return_when=asyncio.FIRST_COMPLETED
            )
            for task in done:
                if task is not stop_task:
                    await task
                    self.request_stop()
            if self._fatal_error is not None:
                raise self._fatal_error
        except asyncio.CancelledError:
            self.request_stop()
            raise
        except Exception as exc:
            await self._halt_for_error(exc)
            raise
        finally:
            self.request_stop()
            for task in tasks:
                if not task.done():
                    task.cancel()
            if tasks:
                await asyncio.gather(*tasks, return_exceptions=True)
            if dashboard_started and self.dashboard is not None:
                await asyncio.to_thread(self.dashboard.stop)
            self._running = False
            self.state.event("info", "runtime", "live runtime stopped")

    def request_stop(self) -> None:
        self._stop.set()
        for feed in (self.futures_feed, self.proxy_feed):
            stopper = getattr(feed, "stop", None)
            if callable(stopper):
                stopper()

    async def _consume_futures_bar(self, bar: Bar) -> None:
        try:
            await self.process_futures_bar(bar)
        except SignalRejected as exc:
            self.state.event(
                "warning",
                "execution",
                f"signal rejected: {exc}",
                {"instrument": bar.instrument.upper()},
                timestamp=bar.timestamp,
            )
        except Exception as exc:
            if self.config.halt_on_error:
                await self._halt_for_error(exc)
                self._fatal_error = exc
                return
            self.state.event(
                "error",
                "runtime",
                f"bar processing failed: {exc}",
                {"instrument": bar.instrument.upper()},
                timestamp=bar.timestamp,
            )

    async def _consume_proxy_bar(self, bar: ProxyBar) -> None:
        try:
            await self.process_proxy_bar(bar)
        except Exception as exc:
            if self.config.halt_on_error:
                await self._halt_for_error(exc)
                self._fatal_error = exc
                return
            self.state.event("error", "runtime", f"proxy bar failed: {exc}")

    async def _execute_due_signal(
        self, symbol: str, bar: Bar
    ) -> OrderSubmission | None:
        signal = self._pending.get(symbol)
        if signal is None or bar.timestamp < signal.execution_time:
            return None
        self._pending.pop(symbol, None)
        if bar.timestamp > signal.execution_time:
            self.state.event(
                "warning",
                "execution",
                "pending signal canceled because its entry minute was missed",
                {
                    "instrument": symbol,
                    "expected": signal.execution_time.isoformat(),
                    "received": bar.timestamp.isoformat(),
                },
                timestamp=bar.timestamp,
            )
            return None
        if self.store.kill_switch.engaged:
            raise SignalRejected("kill switch is engaged")
        return await self._execute_signal(
            signal,
            as_of=bar.timestamp,
            reference_price=bar.open,
            proxy_preferred_timestamp=bar.timestamp,
        )

    async def _execute_at_live_boundary(
        self, signal: TradeSignal, signal_bar: Bar
    ) -> OrderSubmission | None:
        received_at = self._clock()
        if received_at.tz is None:
            raise ValueError("runtime clock must return a timezone-aware timestamp")
        wait_seconds = (signal.execution_time - received_at).total_seconds()
        if wait_seconds > 0:
            await asyncio.sleep(wait_seconds)
            received_at = signal.execution_time
        delay = (received_at - signal.execution_time).total_seconds()
        if delay > self.config.maximum_entry_delay_seconds:
            self.state.event(
                "warning",
                "execution",
                "signal canceled because the live entry boundary was received late",
                {
                    "instrument": signal.instrument,
                    "execution_time": signal.execution_time.isoformat(),
                    "received_at": received_at.isoformat(),
                    "delay_seconds": delay,
                },
                timestamp=received_at,
            )
            return None
        if self.store.kill_switch.engaged:
            raise SignalRejected("kill switch is engaged")
        return await self._execute_signal(
            signal,
            as_of=signal.execution_time,
            reference_price=signal_bar.close,
            proxy_preferred_timestamp=signal_bar.timestamp,
        )

    async def _execute_signal(
        self,
        signal: TradeSignal,
        *,
        as_of: pd.Timestamp,
        reference_price: float,
        proxy_preferred_timestamp: pd.Timestamp,
    ) -> OrderSubmission | None:
        if not await self._context_accepts(signal, as_of):
            return None
        if self.config.route is ExecutionRoute.OPTIONS:
            submission = await self._execute_option(
                signal,
                as_of=as_of,
                futures_reference_price=reference_price,
                proxy_preferred_timestamp=proxy_preferred_timestamp,
            )
        else:
            submission = await self._execute_future(
                signal, as_of=as_of, reference_price=reference_price
            )
        self.state.event(
            "info",
            "execution",
            "order previewed" if submission.dry_run else "order submitted",
            {
                "broker": submission.broker,
                "instrument": signal.instrument.upper(),
                "client_order_id": submission.client_order_id,
                "order_id": submission.order_id,
                "route": self.config.route.value,
            },
            timestamp=as_of,
        )
        return submission

    async def _context_accepts(
        self, signal: TradeSignal, as_of: pd.Timestamp
    ) -> bool:
        if not self.config.external_context_enabled:
            return True
        proxy = self._proxy_symbol(signal.instrument)
        context = None
        try:
            assert self.context_provider is not None
            context = await asyncio.to_thread(
                self.context_provider.context_for_signal,
                signal,
                proxy_symbol=proxy,
                observed_at=as_of,
                min_premium=self.config.context_minimum_premium,
                limit=self.config.context_limit,
            )
        except Exception as exc:
            self.state.event(
                "warning",
                "context",
                f"external context unavailable: {exc}",
                {"instrument": signal.instrument, "proxy": proxy},
                timestamp=as_of,
            )
        decision = self.context_gate.evaluate(signal, context, as_of=as_of)
        self.state.event(
            "info" if decision.accepted else "warning",
            "context",
            "external context accepted signal"
            if decision.accepted
            else "external context rejected signal",
            {
                "instrument": signal.instrument,
                "proxy": proxy,
                "reason": decision.reason,
            },
            timestamp=as_of,
        )
        return decision.accepted

    async def _execute_option(
        self,
        signal: TradeSignal,
        *,
        as_of: pd.Timestamp,
        futures_reference_price: float,
        proxy_preferred_timestamp: pd.Timestamp,
    ) -> OrderSubmission:
        assert self.option_chain is not None
        assert self.option_broker is not None
        proxy_symbol = self._proxy_symbol(signal.instrument)
        proxy_bar = await self._proxy_for_execution(
            proxy_symbol,
            as_of,
            preferred_timestamp=proxy_preferred_timestamp,
        )
        proxy_price = (
            proxy_bar.open
            if proxy_bar.timestamp == as_of
            else proxy_bar.close
        )
        chain = await asyncio.to_thread(
            self.option_chain.fetch,
            proxy_symbol,
            as_of=as_of,
            underlying_price=proxy_price,
        )
        proxy_target = proxy_price * signal.target.price / futures_reference_price
        candidates = self.options_selector.select(
            signal,
            chain,
            profile=self.config.options_profile,
            as_of=as_of,
            risk_budget=self.config.options_risk_budget,
            proxy_target=proxy_target,
        )
        if not candidates:
            raise SignalRejected("no option contract passed the selection rules")
        snapshot = await self._snapshot_and_reconcile(self.option_broker)
        self._ensure_live_entry_is_fresh(signal)
        return await asyncio.to_thread(
            self.option_broker.submit_option,
            candidates[0],
            snapshot=snapshot,
            risk_gate=self.risk_gate,
            dry_run=self.config.dry_run,
            daily_pl=snapshot.account.daily_pl,
        )

    async def _execute_future(
        self,
        signal: TradeSignal,
        *,
        as_of: pd.Timestamp,
        reference_price: float,
    ) -> OrderSubmission:
        assert self.futures_broker is not None
        snapshot = await self._snapshot_and_reconcile(self.futures_broker)
        self._ensure_live_entry_is_fresh(signal)
        return await asyncio.to_thread(
            self.futures_broker.submit_order,
            signal,
            as_of=as_of,
            reference_price=reference_price,
            snapshot=snapshot,
            risk_gate=self.risk_gate,
            dry_run=self.config.dry_run,
            daily_pl=snapshot.account.daily_pl,
        )

    async def _snapshot_and_reconcile(
        self, broker: SnapshotBroker
    ) -> BrokerSnapshot:
        snapshot = await asyncio.to_thread(broker.snapshot, as_of=self._clock())
        report = self.reconciler.reconcile(snapshot)
        self.state.update_broker(snapshot)
        self.state.update_reconciliation(report)
        if not report.healthy:
            raise SignalRejected("broker reconciliation is unhealthy")
        return snapshot

    async def _proxy_for_execution(
        self,
        proxy_symbol: str,
        as_of: pd.Timestamp,
        *,
        preferred_timestamp: pd.Timestamp,
    ) -> ProxyBar:
        def exact_or_newer() -> ProxyBar | None:
            current = self.price_book.proxies.get(proxy_symbol)
            if current is not None and current.timestamp == preferred_timestamp:
                return current
            return None

        current = exact_or_newer()
        if current is None and self.config.proxy_wait_seconds:
            try:
                async with self._proxy_changed:
                    await asyncio.wait_for(
                        self._proxy_changed.wait_for(
                            lambda: exact_or_newer() is not None
                        ),
                        timeout=self.config.proxy_wait_seconds,
                    )
            except TimeoutError:
                pass
            current = exact_or_newer()
        if current is None:
            current = self.price_book.proxies.get(proxy_symbol)
        if current is None:
            raise SignalRejected(f"no proxy bar is available for {proxy_symbol}")
        age = (as_of - current.timestamp).total_seconds()
        if age < 0:
            raise SignalRejected(f"proxy bar for {proxy_symbol} is from the future")
        if age > self.config.proxy_maximum_age_seconds:
            raise SignalRejected(f"proxy bar for {proxy_symbol} is stale")
        return current

    def _record_rejection(
        self, symbol: str, exc: SignalRejected, timestamp: pd.Timestamp
    ) -> None:
        self.state.event(
            "warning",
            "execution",
            f"signal rejected: {exc}",
            {"instrument": symbol},
            timestamp=timestamp,
        )

    def _ensure_live_entry_is_fresh(self, signal: TradeSignal) -> None:
        if self.config.entry_timing is not EntryTiming.LIVE_BOUNDARY:
            return
        now = self._clock()
        if now.tz is None:
            raise ValueError("runtime clock must return a timezone-aware timestamp")
        delay = (now - signal.execution_time).total_seconds()
        if delay > self.config.maximum_entry_delay_seconds:
            raise SignalRejected(
                "entry preparation exceeded the maximum live delay"
            )

    def _proxy_symbol(self, instrument: str) -> str:
        proxy = self.config.proxy_symbols.get(instrument.upper())
        if not proxy:
            raise SignalRejected(f"no proxy configured for {instrument.upper()}")
        return str(proxy).upper()

    def _validate_broker_mode(self) -> None:
        if self.config.mode is RuntimeMode.DRY_RUN:
            return
        for broker in self.brokers.values():
            broker_config = getattr(broker, "config", None)
            if broker_config is None:
                continue
            enabled = getattr(broker_config, "enabled", None)
            allow_live = getattr(broker_config, "allow_live", None)
            paper = getattr(broker_config, "paper", None)
            demo = getattr(broker_config, "demo", None)
            if enabled is False:
                raise ValueError("paper/live mode requires broker submission enabled")
            if self.config.mode is RuntimeMode.PAPER:
                if paper is False or demo is False:
                    raise ValueError("paper mode requires a paper or demo broker endpoint")
            else:
                if paper is True or demo is True:
                    raise ValueError("live mode requires a live broker endpoint")
                if allow_live is False:
                    raise ValueError("live mode requires broker live permission")

    async def _reconciliation_loop(self) -> None:
        interval = self.config.reconciliation_interval_seconds
        assert interval is not None
        while not self._stop.is_set():
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=interval)
                return
            except TimeoutError:
                pass
            try:
                await self.reconcile_once()
            except Exception as exc:
                if self.config.halt_on_error:
                    await self._halt_for_error(exc)
                    self._fatal_error = exc
                    return
                self.state.event(
                    "error", "broker", f"periodic reconciliation failed: {exc}"
                )

    async def _health_loop(self) -> None:
        while not self._stop.is_set():
            self.state.update_feed("databento_futures", self.futures_feed.health)
            if self.proxy_feed is not None:
                self.state.update_feed("alpaca_proxy", self.proxy_feed.health)
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=1.0)
            except TimeoutError:
                pass

    async def _halt_for_error(self, exc: Exception) -> None:
        now = self._clock()
        if now.tz is None:
            now = now.tz_localize("UTC")
        if not self.store.kill_switch.engaged:
            self.store.engage_kill_switch(
                "runtime error; operator reconciliation required", as_of=now
            )
        self.state.event(
            "critical",
            "runtime",
            f"entries halted after runtime error: {exc}",
            timestamp=now,
        )
        self.request_stop()
