"""Local operator command center for live feed, broker, and risk state."""

from __future__ import annotations

import hmac
import json
import threading
from collections import deque
from collections.abc import Mapping
from dataclasses import asdict, dataclass
from enum import Enum
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Protocol
from urllib.parse import urlparse

import pandas as pd

from .broker import (
    BrokerSnapshot,
    ExecutionStore,
    ReconciliationReport,
    ReconciliationSeverity,
)
from .live_data import FeedHealth, FeedState, LatestPriceBook


@dataclass(frozen=True, slots=True)
class OperatorEvent:
    timestamp: pd.Timestamp
    level: str
    category: str
    message: str
    details: Mapping[str, Any] | None = None


class OperatorState:
    """Thread-safe materialized view rendered by the dashboard."""

    def __init__(
        self,
        store: ExecutionStore,
        *,
        mode: str = "dry_run",
        price_book: LatestPriceBook | None = None,
        maximum_events: int = 500,
        clock: Any = None,
    ) -> None:
        self.store = store
        self.mode = mode
        self.price_book = price_book or LatestPriceBook()
        self.maximum_events = maximum_events
        self._clock = clock or (lambda: pd.Timestamp.now(tz="UTC"))
        self._started_at = self._clock()
        self._lock = threading.RLock()
        self._feeds: dict[str, FeedHealth] = {}
        self._brokers: dict[str, BrokerSnapshot] = {}
        self._reconciliations: dict[str, ReconciliationReport] = {}
        self._events: deque[OperatorEvent] = deque(maxlen=maximum_events)

    def update_feed(self, name: str, health: FeedHealth) -> None:
        with self._lock:
            self._feeds[name] = health

    def update_broker(self, snapshot: BrokerSnapshot) -> None:
        with self._lock:
            self._brokers[snapshot.broker] = snapshot

    def update_reconciliation(self, report: ReconciliationReport) -> None:
        with self._lock:
            self._reconciliations[report.broker] = report

    def event(
        self,
        level: str,
        category: str,
        message: str,
        details: Mapping[str, Any] | None = None,
        *,
        timestamp: pd.Timestamp | None = None,
    ) -> None:
        when = timestamp or self._clock()
        if when.tz is None:
            raise ValueError("operator event timestamp must be timezone-aware")
        with self._lock:
            self._events.append(
                OperatorEvent(when, level.lower(), category, message, details)
            )

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            now = self._clock()
            feeds = dict(self._feeds)
            brokers = dict(self._brokers)
            reconciliations = dict(self._reconciliations)
            events = tuple(self._events)
        kill_switch = self.store.kill_switch
        trades = self.store.trades()
        critical = sum(
            1
            for report in reconciliations.values()
            for issue in report.issues
            if issue.severity is ReconciliationSeverity.CRITICAL
        )
        feed_failures = sum(
            1
            for health in feeds.values()
            if health.state in {FeedState.BACKOFF, FeedState.FAILED}
        )
        return _json_value(
            {
                "service": "silver-bullet",
                "mode": self.mode,
                "as_of": now,
                "started_at": self._started_at,
                "uptime_seconds": max(0, (now - self._started_at).total_seconds()),
                "healthy": (
                    critical == 0
                    and feed_failures == 0
                    and not kill_switch.engaged
                ),
                "kill_switch": asdict(kill_switch),
                "summary": {
                    "feeds": len(feeds),
                    "feed_failures": feed_failures,
                    "brokers": len(brokers),
                    "tracked_trades": len(trades),
                    "open_trades": sum(
                        trade.status.value in {"pending_entry", "open", "exiting"}
                        for trade in trades
                    ),
                    "critical_reconciliation_issues": critical,
                },
                "feeds": {name: asdict(health) for name, health in feeds.items()},
                "brokers": {
                    name: {
                        "as_of": snapshot.as_of,
                        "account": asdict(snapshot.account),
                        "orders": [
                            {
                                key: value
                                for key, value in asdict(order).items()
                                if key != "raw"
                            }
                            for order in snapshot.orders
                        ],
                        "positions": [asdict(position) for position in snapshot.positions],
                        "fills": [asdict(fill) for fill in snapshot.fills[-50:]],
                    }
                    for name, snapshot in brokers.items()
                },
                "reconciliations": {
                    name: asdict(report) for name, report in reconciliations.items()
                },
                "trades": [asdict(trade) for trade in trades],
                "prices": {
                    "futures": {
                        symbol: asdict(bar)
                        for symbol, bar in self.price_book.futures.items()
                    },
                    "proxies": {
                        symbol: asdict(bar)
                        for symbol, bar in self.price_book.proxies.items()
                    },
                },
                "events": [asdict(event) for event in reversed(events)],
            }
        )


class OperatorBroker(Protocol):
    name: str

    def snapshot(self, **kwargs: Any) -> BrokerSnapshot: ...

    def cancel_all_orders(self) -> Any: ...

    def close_all_positions(self, **kwargs: Any) -> Any: ...


class LiveOperatorController:
    """Coordinates operator actions across configured brokers."""

    def __init__(
        self,
        store: ExecutionStore,
        state: OperatorState,
        brokers: Mapping[str, OperatorBroker],
        *,
        exclusive_account: bool = True,
        clock: Any = None,
    ) -> None:
        self.store = store
        self.state = state
        self.brokers = dict(brokers)
        self.exclusive_account = exclusive_account
        self._clock = clock or (lambda: pd.Timestamp.now(tz="UTC"))

    def engage(self, reason: str, *, flatten: bool) -> dict[str, Any]:
        now = self._clock()
        self.store.engage_kill_switch(reason, as_of=now)
        results: dict[str, Any] = {}
        failures: dict[str, str] = {}
        if flatten:
            for name, broker in self.brokers.items():
                canceled = None
                closed = None
                errors = []
                try:
                    canceled = broker.cancel_all_orders()
                except Exception as exc:
                    errors.append(f"cancel failed: {exc}")
                try:
                    try:
                        closed = broker.close_all_positions(cancel_orders=True)
                    except TypeError:
                        closed = broker.close_all_positions()
                except Exception as exc:
                    errors.append(f"flatten failed: {exc}")
                results[name] = {"canceled": canceled, "closed": closed}
                if errors:
                    failures[name] = "; ".join(errors)
        self.state.event(
            "critical",
            "risk",
            f"kill switch engaged: {reason}",
            {"flatten": flatten, "failures": failures},
            timestamp=now,
        )
        if failures:
            raise RuntimeError(
                "kill switch engaged locally; broker action failed for "
                + ", ".join(sorted(failures))
            )
        return results

    def reconcile(self) -> dict[str, Any]:
        from .broker import ExecutionReconciler

        results = {}
        for name, broker in self.brokers.items():
            snapshot = broker.snapshot(as_of=self._clock())
            report = ExecutionReconciler(
                self.store, exclusive_account=self.exclusive_account
            ).reconcile(snapshot)
            self.state.update_broker(snapshot)
            self.state.update_reconciliation(report)
            results[name] = {
                "healthy": report.healthy,
                "issues": len(report.issues),
            }
        self.state.event("info", "broker", "broker reconciliation completed")
        return results

    def release(self) -> dict[str, Any]:
        results = self.reconcile()
        unhealthy = [name for name, result in results.items() if not result["healthy"]]
        if unhealthy:
            raise RuntimeError(
                "kill switch cannot be released while reconciliation is unhealthy"
            )
        self.store.release_kill_switch()
        self.state.event("warning", "risk", "kill switch released")
        return results


@dataclass(frozen=True, slots=True)
class DashboardConfig:
    host: str = "127.0.0.1"
    port: int = 8765
    control_token: str | None = None

    def __post_init__(self) -> None:
        if not 0 <= self.port <= 65535:
            raise ValueError("dashboard port is invalid")
        if self.host not in {"127.0.0.1", "localhost", "::1"}:
            raise ValueError(
                "dashboard supports loopback binding only; use a secure tunnel"
            )


class OperatorDashboard:
    def __init__(
        self,
        state: OperatorState,
        controller: LiveOperatorController,
        config: DashboardConfig | None = None,
    ) -> None:
        self.state = state
        self.controller = controller
        self.config = config or DashboardConfig()
        self._server: ThreadingHTTPServer | None = None
        self._thread: threading.Thread | None = None

    @property
    def address(self) -> tuple[str, int]:
        if self._server is None:
            return self.config.host, self.config.port
        host, port = self._server.server_address[:2]
        return str(host), int(port)

    def start(self) -> None:
        if self._server is not None:
            raise RuntimeError("dashboard is already running")
        handler = _handler_factory(self.state, self.controller, self.config)
        self._server = ThreadingHTTPServer(
            (self.config.host, self.config.port), handler
        )
        self._thread = threading.Thread(
            target=self._server.serve_forever,
            name="silver-bullet-dashboard",
            daemon=True,
        )
        self._thread.start()

    def serve_forever(self) -> None:
        if self._server is not None:
            raise RuntimeError("dashboard is already running")
        handler = _handler_factory(self.state, self.controller, self.config)
        self._server = ThreadingHTTPServer(
            (self.config.host, self.config.port), handler
        )
        self._server.serve_forever()

    def stop(self) -> None:
        server = self._server
        if server is None:
            return
        server.shutdown()
        server.server_close()
        if self._thread and self._thread is not threading.current_thread():
            self._thread.join(timeout=5)
        self._server = None
        self._thread = None


def _handler_factory(
    state: OperatorState,
    controller: LiveOperatorController,
    config: DashboardConfig,
):
    class Handler(BaseHTTPRequestHandler):
        server_version = "SilverBulletOperator/1.0"

        def do_GET(self) -> None:  # noqa: N802
            path = urlparse(self.path).path
            if path == "/":
                self._send(HTTPStatus.OK, DASHBOARD_HTML, "text/html; charset=utf-8")
                return
            if path in {"/api/status", "/api/health"}:
                if not self._authorized():
                    self._json(HTTPStatus.UNAUTHORIZED, {"error": "unauthorized"})
                    return
                payload = state.snapshot()
                status = (
                    HTTPStatus.OK
                    if path == "/api/status" or payload["healthy"]
                    else HTTPStatus.SERVICE_UNAVAILABLE
                )
                self._json(status, payload)
                return
            self._json(HTTPStatus.NOT_FOUND, {"error": "not_found"})

        def do_POST(self) -> None:  # noqa: N802
            path = urlparse(self.path).path
            if path not in {
                "/api/kill-switch/engage",
                "/api/kill-switch/release",
                "/api/reconcile",
            }:
                self._json(HTTPStatus.NOT_FOUND, {"error": "not_found"})
                return
            if not self._authorized():
                self._json(HTTPStatus.UNAUTHORIZED, {"error": "unauthorized"})
                return
            try:
                body = self._body()
                if path.endswith("/engage"):
                    reason = str(body.get("reason") or "operator request").strip()
                    result = controller.engage(
                        reason, flatten=bool(body.get("flatten", False))
                    )
                elif path.endswith("/release"):
                    if body.get("confirmation") != "RELEASE":
                        self._json(
                            HTTPStatus.BAD_REQUEST,
                            {"error": "confirmation must equal RELEASE"},
                        )
                        return
                    result = controller.release()
                else:
                    result = controller.reconcile()
                self._json(HTTPStatus.OK, {"ok": True, "result": result})
            except (ValueError, json.JSONDecodeError) as exc:
                self._json(HTTPStatus.BAD_REQUEST, {"error": str(exc)})
            except Exception as exc:
                self._json(HTTPStatus.CONFLICT, {"error": str(exc)})

        def log_message(self, format: str, *args: Any) -> None:
            return

        def _authorized(self) -> bool:
            expected = config.control_token
            if expected is None:
                return True
            provided = self.headers.get("X-Silver-Bullet-Token", "")
            return hmac.compare_digest(expected, provided)

        def _body(self) -> dict[str, Any]:
            raw_length = self.headers.get("Content-Length", "0")
            length = int(raw_length)
            if length < 0 or length > 65_536:
                raise ValueError("request body is too large")
            if length == 0:
                return {}
            payload = json.loads(self.rfile.read(length))
            if not isinstance(payload, dict):
                raise ValueError("request body must be a JSON object")
            return payload

        def _json(self, status: HTTPStatus, payload: Any) -> None:
            self._send(
                status,
                json.dumps(_json_value(payload), separators=(",", ":")),
                "application/json",
            )

        def _send(self, status: HTTPStatus, content: str, content_type: str) -> None:
            encoded = content.encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(encoded)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("X-Frame-Options", "DENY")
            self.send_header(
                "Content-Security-Policy",
                "default-src 'self'; script-src 'unsafe-inline'; "
                "style-src 'unsafe-inline'; connect-src 'self'",
            )
            self.end_headers()
            self.wfile.write(encoded)

    return Handler


def _json_value(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {str(key): _json_value(item) for key, item in value.items()}
    if isinstance(value, (tuple, list, deque)):
        return [_json_value(item) for item in value]
    if isinstance(value, Enum):
        return value.value
    if isinstance(value, pd.Timestamp):
        return value.isoformat()
    return value


DASHBOARD_HTML = r'''<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Silver Bullet Command Center</title>
  <style>
    :root { color-scheme: dark; --bg:#07100d; --panel:#101c17; --line:#263a32;
      --text:#eaf5ef; --muted:#93a69d; --lime:#a8ff60; --amber:#ffbd59;
      --red:#ff5d68; --blue:#69b7ff; }
    * { box-sizing:border-box } body { margin:0; background:radial-gradient(circle at 85% 0,#173126 0,transparent 34%),var(--bg);
      color:var(--text); font:14px/1.45 ui-monospace,SFMono-Regular,Menlo,monospace; min-height:100vh }
    header { display:flex; justify-content:space-between; gap:24px; align-items:center; padding:28px 34px 20px; border-bottom:1px solid var(--line) }
    .brand { display:flex; align-items:center; gap:14px } .mark { width:14px; height:38px; background:var(--lime); box-shadow:0 0 28px #a8ff6070 }
    h1 { font:700 20px/1.1 system-ui,sans-serif; margin:0; letter-spacing:.02em } .subtitle { color:var(--muted); font-size:12px; margin-top:4px }
    .topline { display:flex; gap:12px; align-items:center; flex-wrap:wrap } .pill { border:1px solid var(--line); background:#0d1814; padding:7px 10px; border-radius:999px; color:var(--muted) }
    .dot { display:inline-block; width:8px; height:8px; border-radius:50%; margin-right:7px; background:var(--lime); box-shadow:0 0 10px currentColor }
    main { padding:24px 34px 40px; max-width:1600px; margin:auto } .metrics { display:grid; grid-template-columns:repeat(5,minmax(140px,1fr)); gap:12px; margin-bottom:14px }
    .metric,.panel { background:linear-gradient(145deg,#12211a,#0d1713); border:1px solid var(--line); border-radius:12px; box-shadow:0 14px 35px #0004 }
    .metric { padding:16px } .metric .label { color:var(--muted); font-size:11px; text-transform:uppercase; letter-spacing:.12em } .metric .value { font:700 24px/1.2 system-ui,sans-serif; margin-top:8px }
    .grid { display:grid; grid-template-columns:1.2fr .8fr; gap:14px } .panel { padding:18px; overflow:hidden } .panel.wide { grid-column:1/-1 }
    .panel-head { display:flex; align-items:center; justify-content:space-between; margin-bottom:14px } h2 { margin:0; font:650 15px/1 system-ui,sans-serif }
    table { border-collapse:collapse; width:100%; font-size:12px } th { color:var(--muted); font-weight:500; text-align:left; padding:9px 8px; border-bottom:1px solid var(--line) } td { padding:10px 8px; border-bottom:1px solid #1b2b24; white-space:nowrap }
    .empty { color:var(--muted); padding:28px 8px; text-align:center } button { cursor:pointer; border:1px solid var(--line); color:var(--text); background:#192a22; padding:9px 12px; border-radius:8px; font:600 12px system-ui,sans-serif }
    button:hover { border-color:#537865 } button.danger { background:#3b1519; border-color:#743039; color:#ffdfe2 } button.warn { background:#392b12; border-color:#735a25; color:#ffe9b8 }
    .controls { display:flex; gap:8px; flex-wrap:wrap }.feed { display:grid; grid-template-columns:1fr auto; gap:4px 14px; padding:11px 0; border-bottom:1px solid #1b2b24 }.feed small { color:var(--muted) }
    .event { display:grid; grid-template-columns:145px 74px 1fr; gap:10px; padding:9px 0; border-bottom:1px solid #1b2b24; font-size:12px }.event .time,.event .cat { color:var(--muted) }.event.critical .msg { color:var(--red) }.event.warning .msg { color:var(--amber) }
    .kill { color:var(--lime) }.kill.engaged { color:var(--red) } @media(max-width:900px){header{align-items:flex-start;flex-direction:column}.metrics{grid-template-columns:repeat(2,1fr)}.grid{grid-template-columns:1fr}.panel.wide{grid-column:auto}main{padding:18px}.event{grid-template-columns:1fr}.event .time,.event .cat{display:inline}}
  </style>
</head>
<body>
<header><div class="brand"><div class="mark"></div><div><h1>Silver Bullet Command Center</h1><div class="subtitle">Live strategy, execution, and account oversight</div></div></div><div class="topline"><span class="pill"><span class="dot" id="health-dot"></span><span id="health">CONNECTING</span></span><span class="pill" id="mode">MODE —</span><span class="pill" id="clock">—</span></div></header>
<main><section class="metrics"><div class="metric"><div class="label">Feed links</div><div class="value" id="feeds">—</div></div><div class="metric"><div class="label">Broker links</div><div class="value" id="brokers">—</div></div><div class="metric"><div class="label">Open trades</div><div class="value" id="open-trades">—</div></div><div class="metric"><div class="label">Critical drift</div><div class="value" id="drift">—</div></div><div class="metric"><div class="label">Kill switch</div><div class="value kill" id="kill">—</div></div></section>
<section class="grid"><article class="panel"><div class="panel-head"><h2>Market data</h2><span class="subtitle">connection health</span></div><div id="feed-list" class="empty">Waiting for feed state</div></article><article class="panel"><div class="panel-head"><h2>Operator controls</h2><span class="subtitle">token protected</span></div><div class="controls"><button id="reconcile">Reconcile now</button><button class="warn" id="halt">Halt entries</button><button class="danger" id="flatten">Halt + flatten</button><button id="release">Release halt</button></div><p class="subtitle" id="control-result">Destructive controls require confirmation.</p></article>
<article class="panel wide"><div class="panel-head"><h2>Tracked executions</h2><span class="subtitle" id="trade-count">0 records</span></div><div style="overflow:auto"><table><thead><tr><th>Status</th><th>Route</th><th>Instrument</th><th>Direction</th><th>Entry order</th><th>Stop</th><th>Target</th><th>Updated</th></tr></thead><tbody id="trade-table"></tbody></table></div></article>
<article class="panel wide"><div class="panel-head"><h2>Event stream</h2><span class="subtitle">newest first</span></div><div id="events" class="empty">No operator events</div></article></section></main>
<script>
const $=id=>document.getElementById(id), token=()=>sessionStorage.getItem('sb-token')||'';
const esc=v=>String(v??'—').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
const stamp=v=>v?new Date(v).toLocaleTimeString([], {hour:'2-digit',minute:'2-digit',second:'2-digit'}):'—';
async function post(path,body={}){let t=token();const send=()=>fetch(path,{method:'POST',headers:{'Content-Type':'application/json','X-Silver-Bullet-Token':t},body:JSON.stringify(body)});let r=await send();if(r.status===401){t=prompt('Control token')||'';sessionStorage.setItem('sb-token',t);r=await send()}const j=await r.json();if(!r.ok)throw Error(j.error||r.statusText);return j}
function render(s){$('clock').textContent=new Date(s.as_of).toLocaleString();$('mode').textContent='MODE '+s.mode.toUpperCase();$('health').textContent=s.healthy?'HEALTHY':'ATTENTION';$('health-dot').style.background=s.healthy?'var(--lime)':'var(--red)';$('feeds').textContent=s.summary.feeds;$('brokers').textContent=s.summary.brokers;$('open-trades').textContent=s.summary.open_trades;$('drift').textContent=s.summary.critical_reconciliation_issues;const k=s.kill_switch.engaged;$('kill').textContent=k?'ENGAGED':'ARMED';$('kill').className='value kill'+(k?' engaged':'');const feeds=Object.entries(s.feeds);$('feed-list').className=feeds.length?'':'empty';$('feed-list').innerHTML=feeds.map(([n,f])=>`<div class="feed"><strong>${esc(n)}</strong><span>${esc(f.state).toUpperCase()}</span><small>${esc(f.symbols.join(' · '))}</small><small>${f.bar_count} bars · ${f.reconnect_count} reconnects</small></div>`).join('')||'Waiting for feed state';$('trade-count').textContent=s.trades.length+' records';$('trade-table').innerHTML=s.trades.map(t=>`<tr><td>${esc(t.status)}</td><td>${esc(t.broker)} / ${esc(t.route)}</td><td>${esc(t.instrument)}</td><td>${esc(t.direction)}</td><td>${esc(t.entry_order_id)}</td><td>${esc(t.stop_price)}</td><td>${esc(t.target_price)}</td><td>${stamp(t.updated_at)}</td></tr>`).join('')||'<tr><td colspan="8" class="empty">No tracked executions</td></tr>';$('events').className=s.events.length?'':'empty';$('events').innerHTML=s.events.map(e=>`<div class="event ${esc(e.level)}"><span class="time">${new Date(e.timestamp).toLocaleString()}</span><span class="cat">${esc(e.category)}</span><span class="msg">${esc(e.message)}</span></div>`).join('')||'No operator events'}
async function refresh(){try{let t=token(),r=await fetch('/api/status',{cache:'no-store',headers:t?{'X-Silver-Bullet-Token':t}:{}});if(r.status===401){t=prompt('Control token')||'';sessionStorage.setItem('sb-token',t);r=await fetch('/api/status',{cache:'no-store',headers:{'X-Silver-Bullet-Token':t}})}if(!r.ok)throw Error(r.statusText);render(await r.json())}catch(e){$('health').textContent='OFFLINE';$('health-dot').style.background='var(--red)'}}setInterval(refresh,2000);refresh();
async function act(path,body){try{const r=await post(path,body);$('control-result').textContent='Completed: '+JSON.stringify(r.result);refresh()}catch(e){$('control-result').textContent='Failed: '+e.message}}
$('reconcile').onclick=()=>act('/api/reconcile',{});$('halt').onclick=()=>{const r=prompt('Reason for halting entries?');if(r)act('/api/kill-switch/engage',{reason:r,flatten:false})};$('flatten').onclick=()=>{const r=prompt('Type a reason to cancel orders and flatten all positions');if(r&&confirm('Cancel all open orders and flatten every configured broker position?'))act('/api/kill-switch/engage',{reason:r,flatten:true})};$('release').onclick=()=>{if(confirm('Release only after a clean reconciliation?'))act('/api/kill-switch/release',{confirmation:'RELEASE'})};
</script></body></html>'''
