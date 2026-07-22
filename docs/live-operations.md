# Live Operations Runbook

This runbook covers the guarded Databento, Alpaca, and Tradovate runtime. The
code path is implemented and locally tested. It is not considered production
accepted until the credentialed checklist at the end has been completed.

## Safety model

- Dry-run is the default and never submits or closes an order.
- Paper/demo mode can submit, but its broker endpoint is forced to paper/demo.
- Real-money mode requires `allow_live: true` in the private configuration and
  `--mode live --confirm-live LIVE` on every start.
- The execution ledger and kill switch persist across restarts.
- A critical reconciliation mismatch or unexpected runtime error stops new
  entries and requires operator review.
- Order-changing HTTP requests are never retried blindly after an ambiguous
  provider failure; the runtime halts and reconciliation determines broker state.
- Broker accounts are treated as dedicated by default. An untracked position is
  critical. Set `exclusive_broker_account` to `false` only after deliberately
  accepting the weaker shared-account check.
- The futures route refuses guessed rollover symbols. ES/NQ contracts must be
  mapped explicitly.
- “Halt entries” does not cancel existing broker orders, so protective orders
  remain intact. “Halt + flatten” cancels orders and requests liquidation.

## Install and configure

```bash
python3 -m venv .venv
.venv/bin/python -m pip install -e '.[dev,live]'
cp live.example.json live.json
chmod 600 live.json
```

Keep credentials in environment variables, never in `live.json`.

All routes require:

```text
DATABENTO_API_KEY
```

The Alpaca options route also requires:

```text
ALPACA_API_KEY
ALPACA_SECRET_KEY
```

Optional Unusual Whales gating requires:

```text
UNUSUAL_WHALES_API_KEY
```

The Tradovate futures route requires:

```text
TRADOVATE_ACCOUNT_ID
TRADOVATE_ACCOUNT_SPEC
```

It also needs either `TRADOVATE_ACCESS_TOKEN` or the complete renewable-token
set:

```text
TRADOVATE_USERNAME
TRADOVATE_PASSWORD
TRADOVATE_APP_ID
TRADOVATE_CID
TRADOVATE_SEC
```

`TRADOVATE_DEVICE_ID` and explicit `TRADOVATE_ES_CONTRACT_ID` /
`TRADOVATE_NQ_CONTRACT_ID` values are optional. Contract names belong in the
private config or `TRADOVATE_ES_CONTRACT` / `TRADOVATE_NQ_CONTRACT`.

Set `SILVER_BULLET_CONTROL_TOKEN` even for loopback use. The dashboard refuses
all non-loopback bindings because it does not terminate TLS; use an authenticated
SSH tunnel when remote access is required.

## Entry timing

Databento’s `ohlcv-1m` timestamp is the start of an aggregate interval, and the
completed aggregate is available after the minute ends. The runtime therefore
treats receipt of the signal-producing completed bar as the next-minute entry
boundary. It sends the market-entry request only if both bar delivery and order
preparation remain within `maximum_entry_delay_seconds`.

The completed close is a risk and bracket-validation reference, not a promised
fill. Neither the network nor a broker can guarantee the resulting fill equals
the recorded next bar open. Databento also omits an OHLCV record when an interval
contains no trade; the strategy does not synthesize that missing bar. See the
official [Databento OHLCV convention](https://databento.com/docs/knowledge-base).

## Start in dry-run

```bash
.venv/bin/silver-bullet-live --config live.json
```

Dry-run still reads live market data, options chains, and broker account state so
it needs the relevant provider credentials. It performs full selection, risk,
and reconciliation checks but returns previews.

On startup, confirm:

1. The printed mode is `dry_run` and the intended route is shown.
2. The dashboard is reachable at the configured loopback address.
3. Databento and, for options, Alpaca feed health become live.
4. The broker snapshot has the expected account identifier and endpoint.
5. Reconciliation is healthy and the kill switch is not engaged.
6. ES/NQ contract mappings match the current quarter before using futures.

The default state file is `.silver-bullet/execution-state.json` beside the
configuration file. Do not delete or hand-edit it to clear a warning.

## Paper or demo mode

```bash
.venv/bin/silver-bullet-live --config live.json --mode paper
```

For `options`, this forces Alpaca paper. For `futures`, this forces Tradovate
demo. The same risk limits, persistence, reconciliation, late-entry rejection,
and dashboard controls apply.

Alpaca multi-leg candidates are submitted as one `mleg` limit order, matching
Alpaca’s documented legs and position-intent model. Synthetic options stop,
target, and forced-close handling depends on this runtime remaining connected;
it is not a broker-hosted bracket. Tradovate futures use a broker-hosted native
OSO entry with target and stop children.

## Operator dashboard

The dashboard exposes:

- feed and account health;
- latest ES/NQ and SPY/QQQ bars;
- tracked entries and exits;
- reconciliation issues and a recent event stream;
- reconcile, halt entries, halt plus flatten, and release controls.

Controls use the `X-Silver-Bullet-Token` header. The browser asks once and keeps
the token in session storage. API endpoints are:

```text
GET  /api/status
GET  /api/health
POST /api/reconcile
POST /api/kill-switch/engage
POST /api/kill-switch/release
```

Release requires the literal confirmation `RELEASE` and succeeds only after a
clean reconciliation.

## Normal shutdown

`Ctrl-C` or `SIGTERM` stops both feeds and the dashboard cleanly. It does not
cancel broker orders or flatten positions. Before shutting down with an open
Alpaca options trade, keep the runtime running through exit or deliberately use
the emergency flatten control; synthetic protection is local to this process.
Broker-hosted Tradovate bracket orders remain at Tradovate after the process
stops, but their presence must be confirmed directly with the broker.

## Recovery after a crash or disconnect

1. Do not remove the execution-state file.
2. Inspect the broker directly for positions and working orders.
3. Restart with the same route, account, state path, and contract mapping.
4. Let startup reconciliation compare the persistent ledger with the broker.
5. If drift is reported, leave the kill switch engaged and resolve the broker
   state manually or use “Halt + flatten.”
6. Reconcile again.
7. Release only after the report is clean and the expected positions are known.

If the state file is lost while the dedicated account has a position, exclusive
account reconciliation treats that position as critical. Restore the latest
known state backup if available; never fabricate tracked orders.

## Emergency procedure

If the dashboard and broker are reachable:

1. Select “Halt entries” to block new entries without touching current broker
   protection.
2. Inspect positions, working orders, and reconciliation.
3. If exposure must be removed, select “Halt + flatten,” enter a reason, and
   confirm.
4. Verify the broker itself shows no unintended position or working order.

If broker cancellation or liquidation fails, the local kill switch remains
engaged and the dashboard reports the failure. Log in to Alpaca or Tradovate
directly and manage the account there. Do not release the local switch until a
subsequent reconciliation is clean.

## Rollover procedure

1. Stop new entries before the planned rollover.
2. Confirm there is no tracked or broker position in the expiring contract.
3. Replace the ES/NQ contract names in the private configuration.
4. Update optional contract IDs if they are pinned.
5. Run dry-run, then Tradovate demo, and inspect the previewed symbol and native
   bracket prices.
6. Restart paper/demo monitoring and reconcile before considering live mode.

## Known operational limits

- Credentialed provider behavior, entitlements, rate limits, and live response
  shapes have not been accepted in this workspace.
- Alpaca options exercise, assignment, and expiry are visible as account
  activities but are not automatically managed by the runtime.
- Options exits are synthetic and rely on completed futures bars plus periodic
  REST reconciliation; there is no broker trade-update stream yet.
- Tradovate polling uses cash-balance snapshots; a long soak must confirm the
  selected cadence stays within account limits.
- The calendar handles regular CME daily/weekend closures but is not a complete
  holiday and early-close calendar.
- Market, spread, and subscription costs must be validated against the actual
  account and data plans.

## Credentialed acceptance checklist

Keep real-money mode disabled until all applicable items have recorded evidence:

- [ ] Databento ES and NQ mappings resolve the intended contracts.
- [ ] Completed-bar delivery delay stays within the configured entry limit.
- [ ] Disconnect and reconnect produce no duplicate or late entries.
- [ ] Alpaca paper single-leg and debit-spread fills reconcile after restart.
- [ ] Alpaca synthetic stop, DOL target, and forced close work through paper
  fills and partial fills.
- [ ] Tradovate demo entry creates the intended native target and stop children.
- [ ] Tradovate demo cancel, flatten, token renewal, and rollover are rehearsed.
- [ ] Daily-loss, order-risk, equity, position, and open-order limits reject as
  configured using broker-reported account values.
- [ ] Dashboard halt preserves protection; flatten failure leaves the local kill
  switch engaged.
- [ ] A multi-session soak completes without unexplained reconciliation drift,
  leaked credentials, or orphaned orders.

Only after this checklist is complete should a separate approval decide whether
to set `allow_live` to `true`. The code never enables it automatically.
