# Live Trading Implementation Checkpoint

Date: July 21, 2026

## Goal

Complete the remaining Phase 2 and Phase 3 work without changing the tested
historical strategy rules:

- live and replayable ES/NQ and SPY/QQQ market data;
- automatic options-chain selection and options order construction;
- account checks, order and fill synchronization, reconciliation, protective
  exits, and kill-switch behavior;
- guarded direct futures routing;
- a local operator command center;
- one integrated live runtime, command-line workflow, configuration examples,
  documentation, and final verification.

## Work Completed In This Checkpoint

### Live market data foundation

Implemented `src/silver_bullet/live_data.py` with:

- Databento completed one-minute ES/NQ bar normalization;
- Alpaca completed one-minute SPY/QQQ proxy bar normalization;
- continuous-contract-to-raw-contract tracking;
- duplicate and out-of-order event rejection;
- reconnect and backoff behavior;
- feed health counters and timestamps;
- an in-memory latest-price book;
- replay support using the same callback contract as a live feed.

### Options selection and Alpaca chain ingestion

Implemented `src/silver_bullet/options.py` with:

- provider-neutral option contracts, quotes, chains, legs, and candidates;
- deterministic high-delta call/put selection;
- DOL-targeted vertical debit-spread construction;
- session-restricted 0DTE momentum selection;
- freshness, spread, liquidity, premium, expiry, and risk-budget filters;
- deterministic ranking and contract sizing;
- Alpaca contract, option-chain, Greeks, quote, open-interest, and volume
  normalization.

### Shared broker safety and reconciliation

Implemented `src/silver_bullet/broker.py` with:

- normalized accounts, orders, positions, and fills;
- an atomic, persistent execution ledger;
- a persistent kill switch;
- account, buying-power, position-count, open-order, and per-order risk checks;
- broker-versus-ledger reconciliation;
- orphan-order and position-drift detection;
- automatic kill-switch engagement on critical reconciliation drift;
- synthetic stop, DOL-target, and forced-close handling for brokers that cannot
  attach native option brackets.

### Alpaca options execution

Implemented `src/silver_bullet/alpaca_broker.py` with:

- account, order, position, and fill snapshots;
- dry-run previews;
- idempotent strategy order identifiers;
- single-leg long-option orders;
- atomic multi-leg debit-spread orders;
- options approval and options buying-power checks;
- reverse closing orders for single- and multi-leg positions;
- cancel-all, close-all, reconciliation, and kill-switch operations;
- paper-first behavior and an additional opt-in for live use.

### Tradovate futures execution

Implemented `src/silver_bullet/tradovate.py` with:

- explicit live-contract mapping rather than unsafe automatic rollover guesses;
- demo-first behavior and an additional opt-in for live use;
- access-token acquisition and renewal;
- account, balance, order, position, and fill snapshots;
- stale-signal, quantity, contract, stop, target, and risk checks;
- automated-order tagging required by the broker;
- native entry-plus-stop-and-target OSO/OCO submission;
- individual and account-wide cancellation/liquidation support;
- persistent tracking and broker reconciliation.

### Operator command center

Implemented `src/silver_bullet/dashboard.py` with:

- a responsive local web dashboard;
- feed, account, position, trade, price, reconciliation, and event state;
- health and status JSON endpoints;
- manual reconciliation;
- halt-new-entry and halt-plus-flatten controls;
- guarded kill-switch release after clean reconciliation;
- control-token protection and a loopback-only default;
- browser security headers and no external front-end dependencies.

### Shared transport

Implemented `src/silver_bullet/transport.py` with bounded JSON HTTP retries,
clear external-service errors, query encoding, and standard-library-only network
support for the broker adapters.

## Verification At The Pause Point

The repository was clean before this work began. The complete suite now reports:

```text
75 passed in 3.66s
```

The complete code-quality check reports:

```text
All checks passed!
```

The new tests cover live-feed normalization and reconnects, all three options
profiles, Alpaca option-chain parsing, execution-state persistence, risk gates,
reconciliation drift, synthetic exits, Alpaca account and multi-leg execution,
Tradovate authentication and native brackets, and dashboard HTTP controls.

No real market-data or broker requests were made. Network behavior is tested
with deterministic fake transports.

## Exact Stopping Point

The next file was going to be `src/silver_bullet/runtime.py`.

That runtime still needs to connect the completed pieces into one event loop. In
particular, it must preserve the strategy's next-minute execution rule: a signal
created on one completed futures bar must remain pending and execute only at the
next expected bar's open. It should then apply optional context gating and route
the accepted signal either to Alpaca options or Tradovate futures.

The newly added modules are intentionally not yet exported from
`src/silver_bullet/__init__.py`, and the package metadata, live CLI, example live
configuration, README status, and roadmap documents have not yet been updated.
This prevents the repository from claiming that the unfinished integrated
runtime is complete.

## Resume Task List

1. Implement `src/silver_bullet/runtime.py`.
   - Warm the strategy with historical bars without submitting old signals.
   - Maintain pending signals until the exact next-minute futures bar.
   - Run futures and proxy feeds together.
   - Apply optional Unusual Whales context gating.
   - Translate the futures DOL move to a configurable proxy target.
   - Fetch and select an option chain for the chosen profile.
   - Route to Alpaca options or Tradovate futures.
   - Run synthetic exits and periodic reconciliation.
   - Update command-center state and events.
   - Halt safely on submission, feed, or reconciliation failures.

2. Add the live command-line workflow.
   - Add `src/silver_bullet/live_cli.py`.
   - Add a `silver-bullet-live` package entry point.
   - Support dry-run, paper, and explicitly armed live modes.
   - Handle graceful shutdown and dashboard startup.

3. Add configuration and packaging support.
   - Add `live.example.json` with environment-variable names only, never secrets.
   - Add optional live dependencies for Databento and Alpaca.
   - Export the new public types from `src/silver_bullet/__init__.py`.
   - Document persistent state and control-token paths.

4. Complete integration tests.
   - Prove signal-bar to next-minute-entry behavior end to end.
   - Prove options and futures route selection.
   - Prove restart/reconciliation behavior.
   - Prove kill-switch behavior when one broker action fails.
   - Run the full test and code-quality suites again.

5. Inspect the dashboard visually.
   - Start it with representative fake state.
   - Check desktop and narrow layouts in a browser-capable environment.
   - Exercise reconcile, halt, flatten, and release controls.

6. Update the README and roadmap documents.
   - Mark code-complete items accurately.
   - Keep licensed-feed, paper-account, and live-account acceptance separate from
     local implementation status.
   - Document operating procedures, recovery, and emergency flattening.

7. Perform external acceptance when credentials and subscriptions are available.
   - Validate Databento ES/NQ live records and contract mappings.
   - Validate Alpaca option chains and paper fills.
   - Validate Tradovate demo access, native brackets, reconciliation, and token
     renewal.
   - Run a monitored paper soak test before enabling live capital.

## Safety Decisions To Preserve

- Dry-run is the default.
- Paper/demo mode is the default for both brokers.
- Live submission always requires a second explicit opt-in.
- Tradovate contract symbols are explicit; the runtime must not guess a rollover.
- Every automated Tradovate order keeps `isAutomated=true`.
- Strategy entries are idempotent and persist across restarts.
- Stops take priority when a bar reaches both stop and target.
- The kill switch blocks new entries but must never block risk-reducing exits.
- A kill switch cannot be released until reconciliation is clean.
- Credentials remain in environment variables and must never be written to
  configuration, logs, dashboard payloads, or repository files.
