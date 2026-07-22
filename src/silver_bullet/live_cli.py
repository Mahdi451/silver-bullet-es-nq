"""Command-line entrypoint for guarded dry-run, paper, and live operation."""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import signal
import sys
from dataclasses import fields, replace
from pathlib import Path
from typing import Any

from .alpaca_broker import AlpacaBroker, AlpacaBrokerConfig
from .broker import ExecutionStore, LiveRiskGate, LiveRiskLimits
from .context import ContextGateConfig, SignalContextGate
from .dashboard import (
    DashboardConfig,
    LiveOperatorController,
    OperatorDashboard,
    OperatorState,
)
from .data import CSVBarLoader, CSVSchema
from .live_data import (
    AlpacaLiveConfig,
    AlpacaProxyLiveFeed,
    DatabentoLiveConfig,
    DatabentoLiveFeed,
    ReconnectPolicy,
)
from .options import (
    AlpacaOptionChainClient,
    AlpacaOptionChainConfig,
    OptionsProfile,
    OptionsSelectionConfig,
    OptionsSelector,
)
from .runtime import (
    EntryTiming,
    ExecutionRoute,
    LiveRuntime,
    LiveRuntimeConfig,
    RuntimeMode,
)
from .strategy import SilverBulletStrategy
from .tradovate import TradovateBroker, TradovateConfig
from .unusual_whales import UnusualWhalesClient, UnusualWhalesConfig


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--config",
        type=Path,
        default=Path("live.json"),
        help="JSON configuration file (default: live.json)",
    )
    parser.add_argument("--route", choices=("options", "futures"))
    parser.add_argument("--mode", choices=("dry-run", "paper", "live"))
    parser.add_argument(
        "--confirm-live",
        metavar="LIVE",
        help="Required literal LIVE confirmation for real-money mode",
    )
    parser.add_argument("--state-path", type=Path)
    parser.add_argument("--no-dashboard", action="store_true")
    parser.add_argument("--dashboard-port", type=int)
    return parser.parse_args(argv)


def load_config(path: Path) -> dict[str, Any]:
    payload = json.loads(path.read_text())
    if not isinstance(payload, dict):
        raise ValueError("live configuration must be a JSON object")
    return payload


def build_runtime(
    settings: dict[str, Any],
    args: argparse.Namespace,
    *,
    config_directory: Path,
) -> tuple[LiveRuntime, dict[str, list[Any]]]:
    route = ExecutionRoute(args.route or settings.get("route", "options"))
    raw_mode = (args.mode or settings.get("mode", "dry_run")).replace("-", "_")
    mode = RuntimeMode(raw_mode)
    live_confirmed = args.confirm_live == "LIVE"
    if mode is RuntimeMode.LIVE:
        if not settings.get("allow_live", False):
            raise ValueError("live mode requires allow_live=true in the config")
        if not live_confirmed:
            raise ValueError("live mode requires --confirm-live LIVE")
    elif args.confirm_live is not None:
        raise ValueError("--confirm-live is only valid with live mode")

    instruments = tuple(
        str(symbol).upper() for symbol in settings.get("instruments", ["ES", "NQ"])
    )
    if not instruments or len(set(instruments)) != len(instruments):
        raise ValueError("instruments must contain unique ES and/or NQ symbols")
    if any(symbol not in {"ES", "NQ"} for symbol in instruments):
        raise ValueError("live instruments must be ES and/or NQ")

    databento = _section(settings, "databento")
    if databento.get("replay_start") is not None and mode is not RuntimeMode.DRY_RUN:
        raise ValueError("Databento replay_start is allowed only in dry-run mode")
    live_symbols = {
        symbol: str(databento.get("symbols", {}).get(symbol, f"{symbol}.c.0"))
        for symbol in instruments
    }
    reconnect = ReconnectPolicy(**_known_kwargs(ReconnectPolicy, databento.get("reconnect")))
    futures_feed = DatabentoLiveFeed(
        DatabentoLiveConfig(
            symbols=live_symbols,
            dataset=str(databento.get("dataset", "GLBX.MDP3")),
            schema="ohlcv-1m",
            input_symbology=str(databento.get("input_symbology", "continuous")),
            replay_start=databento.get("replay_start"),
            heartbeat_interval_seconds=int(
                databento.get("heartbeat_interval_seconds", 30)
            ),
            reconnect=reconnect,
        )
    )

    state_path = args.state_path or Path(
        settings.get("state_path", ".silver-bullet/execution-state.json")
    )
    state_path = _resolve_path(state_path, config_directory)
    store = ExecutionStore(state_path)
    risk_settings = _section(settings, "risk")
    risk_limits = LiveRiskLimits(**_known_kwargs(LiveRiskLimits, risk_settings))
    risk_gate = LiveRiskGate(store, risk_limits)
    strategies = {symbol: SilverBulletStrategy(symbol) for symbol in instruments}

    proxy_symbols = {
        symbol: str(settings.get("proxy_symbols", {}).get(
            symbol, "SPY" if symbol == "ES" else "QQQ"
        )).upper()
        for symbol in instruments
    }
    options_settings = _section(settings, "options")
    profile = OptionsProfile(
        options_settings.get("profile", OptionsProfile.HIGH_DELTA_LONG.value)
    )
    runtime_config = LiveRuntimeConfig(
        route=route,
        mode=mode,
        options_profile=profile,
        options_risk_budget=float(options_settings.get("risk_budget", 500.0)),
        proxy_symbols=proxy_symbols,
        proxy_maximum_age_seconds=float(
            options_settings.get("proxy_maximum_age_seconds", 90.0)
        ),
        proxy_wait_seconds=float(options_settings.get("proxy_wait_seconds", 2.0)),
        external_context_enabled=bool(
            _section(settings, "context").get("enabled", False)
        ),
        context_minimum_premium=_optional_float(
            _section(settings, "context").get("minimum_premium")
        ),
        context_limit=int(_section(settings, "context").get("limit", 100)),
        reconciliation_interval_seconds=_optional_float(
            settings.get("reconciliation_interval_seconds", 30.0)
        ),
        halt_on_error=bool(settings.get("halt_on_error", True)),
        live_trading_confirmed=live_confirmed,
        maximum_entry_delay_seconds=float(
            settings.get("maximum_entry_delay_seconds", 5.0)
        ),
        exclusive_broker_account=bool(
            settings.get("exclusive_broker_account", True)
        ),
        entry_timing=(
            EntryTiming.NEXT_BAR_OPEN
            if databento.get("replay_start") is not None
            else EntryTiming.LIVE_BOUNDARY
        ),
    )

    option_chain = None
    options_selector = None
    option_broker = None
    futures_broker = None
    proxy_feed = None
    if route is ExecutionRoute.OPTIONS:
        api_key, secret_key = _alpaca_credentials()
        alpaca = _section(settings, "alpaca")
        paper = mode is not RuntimeMode.LIVE
        option_chain = AlpacaOptionChainClient(
            AlpacaOptionChainConfig(
                api_key,
                secret_key,
                paper=paper,
                option_feed=str(alpaca.get("option_feed", "indicative")),
                stock_feed=str(alpaca.get("stock_feed", "iex")),
                maximum_dte=int(alpaca.get("maximum_dte", 7)),
                page_limit=int(alpaca.get("page_limit", 1000)),
                maximum_pages=int(alpaca.get("maximum_pages", 20)),
            )
        )
        selection_values = dict(_section(options_settings, "selection"))
        if "momentum_sessions" in selection_values:
            selection_values["momentum_sessions"] = frozenset(
                selection_values["momentum_sessions"]
            )
        selection_values.update(proxy_symbols=proxy_symbols)
        selection = OptionsSelectionConfig(
            **_known_kwargs(OptionsSelectionConfig, selection_values)
        )
        options_selector = OptionsSelector(selection)
        option_broker = AlpacaBroker(
            AlpacaBrokerConfig(
                api_key,
                secret_key,
                paper=paper,
                enabled=mode is not RuntimeMode.DRY_RUN,
                allow_live=live_confirmed,
            ),
            store=store,
        )
        proxy_feed = AlpacaProxyLiveFeed(
            AlpacaLiveConfig(
                symbols=tuple(proxy_symbols.values()),
                feed=str(alpaca.get("stock_feed", "iex")),
                reconnect=ReconnectPolicy(
                    **_known_kwargs(ReconnectPolicy, alpaca.get("reconnect"))
                ),
            )
        )
        brokers = {option_broker.name: option_broker}
    else:
        tradovate = _section(settings, "tradovate")
        contracts = {
            str(key).upper(): str(value)
            for key, value in tradovate.get("contracts", {}).items()
            if str(key).upper() in instruments
        }
        if any("REPLACE" in value.upper() for value in contracts.values()):
            raise ValueError("replace the Tradovate contract placeholders first")
        broker_config = TradovateConfig.from_environment(
            contracts=contracts,
            enabled=mode is not RuntimeMode.DRY_RUN,
            allow_live=live_confirmed,
        )
        quantities = {
            symbol: int(tradovate.get("quantities", {}).get(symbol, 1))
            for symbol in instruments
        }
        broker_config = replace(
            broker_config,
            quantities=quantities,
            demo=mode is not RuntimeMode.LIVE,
            maximum_quantity=int(tradovate.get("maximum_quantity", 2)),
            maximum_signal_age_minutes=float(
                tradovate.get("maximum_signal_age_minutes", 2.0)
            ),
        )
        futures_broker = TradovateBroker(broker_config, store=store)
        brokers = {futures_broker.name: futures_broker}

    context_settings = _section(settings, "context")
    context_provider = None
    context_gate = None
    if runtime_config.external_context_enabled:
        context_provider = UnusualWhalesClient(
            UnusualWhalesConfig.from_environment()
        )
        gate_values = dict(_section(context_settings, "gate"))
        gate_values.update(proxy_symbols=proxy_symbols)
        context_gate = SignalContextGate(
            ContextGateConfig(**_known_kwargs(ContextGateConfig, gate_values))
        )

    state = OperatorState(store, mode=mode.value)
    dashboard = None
    dashboard_settings = _section(settings, "dashboard")
    dashboard_enabled = bool(dashboard_settings.get("enabled", True))
    if args.no_dashboard:
        dashboard_enabled = False
    if dashboard_enabled:
        token_environment = str(
            dashboard_settings.get(
                "control_token_environment", "SILVER_BULLET_CONTROL_TOKEN"
            )
        )
        token = os.environ.get(token_environment)
        dashboard_config = DashboardConfig(
            host=str(dashboard_settings.get("host", "127.0.0.1")),
            port=int(
                args.dashboard_port
                if args.dashboard_port is not None
                else dashboard_settings.get("port", 8765)
            ),
            control_token=token,
        )
        controller_brokers = {} if mode is RuntimeMode.DRY_RUN else brokers
        controller = LiveOperatorController(
            store,
            state,
            controller_brokers,
            exclusive_account=runtime_config.exclusive_broker_account,
        )
        dashboard = OperatorDashboard(state, controller, dashboard_config)

    runtime = LiveRuntime(
        futures_feed=futures_feed,
        proxy_feed=proxy_feed,
        strategies=strategies,
        store=store,
        config=runtime_config,
        option_chain=option_chain,
        options_selector=options_selector,
        option_broker=option_broker,
        futures_broker=futures_broker,
        risk_gate=risk_gate,
        context_provider=context_provider,
        context_gate=context_gate,
        state=state,
        dashboard=dashboard,
    )
    warmup = _load_warmup(settings, instruments, config_directory)
    return runtime, warmup


async def run_runtime(runtime: LiveRuntime) -> None:
    loop = asyncio.get_running_loop()
    registered: list[signal.Signals] = []
    for item in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(item, runtime.request_stop)
            registered.append(item)
        except (NotImplementedError, RuntimeError):
            pass
    try:
        await runtime.run()
    finally:
        for item in registered:
            loop.remove_signal_handler(item)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    try:
        settings = load_config(args.config)
        runtime, warmup = build_runtime(
            settings,
            args,
            config_directory=args.config.resolve().parent,
        )
        if warmup:
            runtime.warmup(warmup)
        dashboard_note = "disabled"
        if runtime.dashboard is not None:
            host, port = runtime.dashboard.address
            dashboard_note = f"http://{host}:{port}"
        print(
            "Starting Silver Bullet "
            f"mode={runtime.config.mode.value} route={runtime.config.route.value} "
            f"dashboard={dashboard_note} state={runtime.store.path}"
        )
        asyncio.run(run_runtime(runtime))
    except KeyboardInterrupt:
        return 130
    except (OSError, ValueError, RuntimeError, json.JSONDecodeError) as exc:
        print(f"silver-bullet-live: {exc}", file=sys.stderr)
        return 2
    return 0


def _section(settings: dict[str, Any], name: str) -> dict[str, Any]:
    value = settings.get(name, {})
    if value is None:
        return {}
    if not isinstance(value, dict):
        raise ValueError(f"{name} configuration must be a JSON object")
    return value


def _known_kwargs(model: type[Any], values: Any) -> dict[str, Any]:
    if values is None:
        return {}
    if not isinstance(values, dict):
        raise ValueError(f"{model.__name__} settings must be a JSON object")
    allowed = {item.name for item in fields(model)}
    unknown = set(values) - allowed
    if unknown:
        raise ValueError(
            f"unknown {model.__name__} settings: {sorted(unknown)}"
        )
    return dict(values)


def _optional_float(value: Any) -> float | None:
    return None if value is None else float(value)


def _resolve_path(path: Path, directory: Path) -> Path:
    return path if path.is_absolute() else directory / path


def _alpaca_credentials() -> tuple[str, str]:
    api_key = os.environ.get("ALPACA_API_KEY")
    secret_key = os.environ.get("ALPACA_SECRET_KEY")
    if not api_key or not secret_key:
        raise RuntimeError("ALPACA_API_KEY and ALPACA_SECRET_KEY are required")
    return api_key, secret_key


def _load_warmup(
    settings: dict[str, Any], instruments: tuple[str, ...], directory: Path
) -> dict[str, list[Any]]:
    warmup = _section(settings, "warmup")
    raw_data = warmup.get("data", {})
    if not raw_data:
        return {}
    if not isinstance(raw_data, dict):
        raise ValueError("warmup data must be a JSON object")
    provider = str(warmup.get("provider", "databento"))
    if provider not in {"databento", "firstrate"}:
        raise ValueError("warmup provider must be databento or firstrate")
    contracts = warmup.get("contracts", {})
    loaded: dict[str, list[Any]] = {}
    for raw_symbol, raw_path in raw_data.items():
        symbol = str(raw_symbol).upper()
        if symbol not in instruments:
            raise ValueError(f"warmup data supplied for inactive instrument {symbol}")
        factory = (
            CSVSchema.databento if provider == "databento" else CSVSchema.firstrate
        )
        schema = factory(symbol, contract=contracts.get(symbol))
        path = _resolve_path(Path(raw_path), directory)
        loaded[symbol] = list(CSVBarLoader(schema).load(path))
    return loaded


if __name__ == "__main__":
    raise SystemExit(main())
