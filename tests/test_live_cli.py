from __future__ import annotations

import json
from pathlib import Path

import pytest

from silver_bullet.live_cli import build_runtime, load_config, parse_args
from silver_bullet.runtime import EntryTiming, ExecutionRoute, RuntimeMode


def _settings(**overrides):
    values = {
        "route": "options",
        "mode": "dry_run",
        "instruments": ["ES"],
        "dashboard": {"enabled": False},
        "reconciliation_interval_seconds": None,
    }
    values.update(overrides)
    return values


def _args(*values):
    return parse_args(["--config", "unused.json", *values])


def test_load_config_requires_json_object(tmp_path: Path):
    path = tmp_path / "live.json"
    path.write_text(json.dumps(["not", "an", "object"]))
    with pytest.raises(ValueError, match="JSON object"):
        load_config(path)


def test_build_dry_run_options_runtime_is_non_submitting_and_persistent(
    tmp_path: Path, monkeypatch
):
    monkeypatch.setenv("ALPACA_API_KEY", "paper-key")
    monkeypatch.setenv("ALPACA_SECRET_KEY", "paper-secret")

    runtime, warmup = build_runtime(
        _settings(state_path="state/execution.json"),
        _args(),
        config_directory=tmp_path,
    )

    assert runtime.config.mode is RuntimeMode.DRY_RUN
    assert runtime.config.route is ExecutionRoute.OPTIONS
    assert not runtime.option_broker.config.enabled
    assert runtime.option_broker.config.paper
    assert runtime.store.path == tmp_path / "state/execution.json"
    assert warmup == {}


def test_paper_mode_enables_only_paper_submission(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("ALPACA_API_KEY", "paper-key")
    monkeypatch.setenv("ALPACA_SECRET_KEY", "paper-secret")

    runtime, _ = build_runtime(
        _settings(mode="paper"),
        _args(),
        config_directory=tmp_path,
    )

    assert runtime.config.mode is RuntimeMode.PAPER
    assert runtime.option_broker.config.enabled
    assert runtime.option_broker.config.paper
    assert not runtime.option_broker.config.allow_live


def test_live_mode_requires_config_opt_in_and_command_confirmation(
    tmp_path: Path, monkeypatch
):
    monkeypatch.setenv("ALPACA_API_KEY", "live-key")
    monkeypatch.setenv("ALPACA_SECRET_KEY", "live-secret")

    with pytest.raises(ValueError, match="allow_live=true"):
        build_runtime(
            _settings(mode="live"),
            _args(),
            config_directory=tmp_path,
        )
    with pytest.raises(ValueError, match="--confirm-live LIVE"):
        build_runtime(
            _settings(mode="live", allow_live=True),
            _args(),
            config_directory=tmp_path,
        )

    runtime, _ = build_runtime(
        _settings(mode="live", allow_live=True),
        _args("--confirm-live", "LIVE"),
        config_directory=tmp_path,
    )
    assert runtime.config.mode is RuntimeMode.LIVE
    assert not runtime.option_broker.config.paper
    assert runtime.option_broker.config.enabled
    assert runtime.option_broker.config.allow_live


def test_futures_route_rejects_unresolved_contract_placeholders(tmp_path: Path):
    settings = _settings(
        route="futures",
        tradovate={"contracts": {"ES": "REPLACE_WITH_ACTIVE_ES_CONTRACT"}},
    )
    with pytest.raises(ValueError, match="contract placeholders"):
        build_runtime(settings, _args(), config_directory=tmp_path)


def test_databento_replay_is_dry_run_only_and_uses_next_bar_timing(
    tmp_path: Path, monkeypatch
):
    monkeypatch.setenv("ALPACA_API_KEY", "paper-key")
    monkeypatch.setenv("ALPACA_SECRET_KEY", "paper-secret")
    settings = _settings(databento={"replay_start": 0})

    runtime, _ = build_runtime(
        settings, _args(), config_directory=tmp_path
    )
    assert runtime.config.entry_timing is EntryTiming.NEXT_BAR_OPEN

    with pytest.raises(ValueError, match="dry-run"):
        build_runtime(
            {**settings, "mode": "paper"},
            _args(),
            config_directory=tmp_path,
        )
