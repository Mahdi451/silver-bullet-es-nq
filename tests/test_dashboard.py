from __future__ import annotations

import json
import urllib.error
import urllib.request

import pandas as pd
import pytest

from silver_bullet.broker import BrokerAccount, BrokerSnapshot, ExecutionStore
from silver_bullet.dashboard import (
    DashboardConfig,
    LiveOperatorController,
    OperatorDashboard,
    OperatorState,
)
from silver_bullet.live_data import FeedHealth, FeedState


NOW = pd.Timestamp("2024-01-08 15:00:00Z")


class FakeBroker:
    name = "fake"

    def __init__(self):
        self.canceled = 0
        self.closed = 0

    def snapshot(self, **kwargs):
        return BrokerSnapshot(
            "fake",
            kwargs.get("as_of") or NOW,
            BrokerAccount("paper", "ACTIVE", 100_000, 50_000, 50_000),
            (),
            (),
        )

    def cancel_all_orders(self):
        self.canceled += 1
        return []

    def close_all_positions(self, **kwargs):
        self.closed += 1
        return []


def _request(url, *, method="GET", token=None, body=None):
    data = None if body is None else json.dumps(body).encode()
    headers = {"Content-Type": "application/json"}
    if token:
        headers["X-Silver-Bullet-Token"] = token
    request = urllib.request.Request(url, data=data, headers=headers, method=method)
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    with opener.open(request, timeout=3) as response:
        return response.status, response.headers, response.read().decode()


def test_dashboard_serves_status_and_token_protected_controls():
    store = ExecutionStore()
    state = OperatorState(store, mode="paper", clock=lambda: NOW)
    state.update_feed(
        "databento",
        FeedHealth(
            "databento", ("ES", "NQ"), state=FeedState.LIVE, bar_count=12
        ),
    )
    broker = FakeBroker()
    controller = LiveOperatorController(
        store, state, {"fake": broker}, clock=lambda: NOW
    )
    dashboard = OperatorDashboard(
        state,
        controller,
        DashboardConfig(port=0, control_token="secret-token"),
    )
    dashboard.start()
    host, port = dashboard.address
    base = f"http://{host}:{port}"
    try:
        status, headers, html = _request(base + "/")
        assert status == 200
        assert "Silver Bullet Command Center" in html
        assert headers["X-Frame-Options"] == "DENY"

        with pytest.raises(urllib.error.HTTPError) as error:
            _request(base + "/api/status")
        assert error.value.code == 401

        _, _, raw = _request(base + "/api/status", token="secret-token")
        payload = json.loads(raw)
        assert payload["mode"] == "paper"
        assert payload["feeds"]["databento"]["bar_count"] == 12

        with pytest.raises(urllib.error.HTTPError) as error:
            _request(
                base + "/api/kill-switch/engage",
                method="POST",
                body={"reason": "test"},
            )
        assert error.value.code == 401

        _request(
            base + "/api/kill-switch/engage",
            method="POST",
            token="secret-token",
            body={"reason": "operator test", "flatten": True},
        )
        assert store.kill_switch.engaged
        assert broker.canceled == 1
        assert broker.closed == 1
        _, _, raw = _request(base + "/api/status", token="secret-token")
        assert not json.loads(raw)["healthy"]

        _request(
            base + "/api/kill-switch/release",
            method="POST",
            token="secret-token",
            body={"confirmation": "RELEASE"},
        )
        assert not store.kill_switch.engaged
    finally:
        dashboard.stop()


def test_non_loopback_dashboard_requires_control_token():
    with pytest.raises(ValueError, match="loopback"):
        DashboardConfig(host="0.0.0.0", port=8765)
    with pytest.raises(ValueError, match="loopback"):
        DashboardConfig(host="0.0.0.0", port=8765, control_token="secret")


def test_halt_entries_preserves_existing_broker_protection():
    store = ExecutionStore()
    state = OperatorState(store, clock=lambda: NOW)
    broker = FakeBroker()
    controller = LiveOperatorController(
        store, state, {"fake": broker}, clock=lambda: NOW
    )

    result = controller.engage("pause new entries", flatten=False)

    assert result == {}
    assert store.kill_switch.engaged
    assert broker.canceled == 0
    assert broker.closed == 0


def test_flatten_failure_keeps_local_kill_switch_engaged():
    class FailingBroker(FakeBroker):
        def cancel_all_orders(self):
            raise RuntimeError("broker unavailable")

    store = ExecutionStore()
    state = OperatorState(store, clock=lambda: NOW)
    broker = FailingBroker()
    controller = LiveOperatorController(
        store, state, {"fake": broker}, clock=lambda: NOW
    )

    with pytest.raises(RuntimeError, match="engaged locally"):
        controller.engage("emergency", flatten=True)

    assert store.kill_switch.engaged
    assert broker.closed == 1
