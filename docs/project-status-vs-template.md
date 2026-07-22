# Project Status vs. Target Presentation Template

This document separates locally implemented code from provider-credentialed
acceptance. “Implemented” means the code path and automated tests exist; it does
not mean a live brokerage account has approved the behavior.

## Current status

| Area | Current status | Notes |
| --- | --- | --- |
| Historical ES/NQ ingestion | Implemented and tested | Strict CSV schemas, timezone normalization, contract metadata, and gap reporting. |
| Liquidity and strategy model | Implemented and tested | Causal day/week levels, ATR displacement, swing break, FVG sequence, session gating, and signal timing. |
| Backtest execution and analytics | Implemented and tested | Fills, costs, exits, account risk, metrics, ledgers, charts, and walk-forward primitives. |
| Unusual Whales context | Implemented, optional | Flow alerts normalize into a provider-neutral context decision at entry time. |
| Databento live futures data | Implemented; credentialed acceptance pending | ES/NQ completed bars, contract mapping, reconnect state, health, and intraday replay boundary. |
| Alpaca proxy data | Implemented; credentialed acceptance pending | SPY/QQQ completed bars with reconnect and cross-thread dispatch into the runtime. |
| Options contract selection | Implemented and tested | High-delta long, DOL debit spread, and restricted 0DTE momentum profiles. |
| Alpaca options execution | Implemented; paper acceptance pending | Account checks, single/multi-leg payloads, persistent tracking, close orders, snapshots, and reconciliation. |
| Tradovate futures execution | Implemented; demo acceptance pending | Token renewal, explicit contract mapping, account checks, native OSO/OCO protection, liquidation, and reconciliation. |
| Persistent broker safety | Implemented and tested | Idempotent ledger, account limits, broker-reported daily loss, exclusive-account drift detection, and persistent kill switch. |
| Integrated live runtime | Implemented and tested | Warmup suppression, live entry boundary, late cancellation, route selection, synthetic exits, periodic reconciliation, and fail-closed stop. |
| Local command center | Implemented and locally inspected | Status, health, prices, events, trades, reconcile, halt, flatten, and guarded release. |
| Live command and config | Implemented and tested | Dry-run default, forced paper/demo mode, second real-money confirmation, persistent paths, and graceful shutdown. |

## Phase framing

### Phase 1: Research core

Implemented and locally verified. Remaining research validation includes real
provider exports, holiday/early-close expansion, longer benchmarks, and manual
chart audits.

### Phase 2: Paper options operation

The code is implemented. Completion now requires credentialed Databento and
Alpaca paper sessions, real fill/restart testing, synthetic-exit rehearsal, and
a multi-session soak.

### Phase 3: Direct futures operation

The Tradovate adapter and shared runtime are implemented. Completion now requires
API entitlement, demo bracket and liquidation testing, token-renewal testing,
quarterly rollover rehearsal, and a separate live-capital decision.

## Status language for presentations

- `Implemented and tested`: available locally with automated evidence.
- `Implemented; external acceptance pending`: code exists, but a credentialed
  provider/account run remains.
- `Operationally accepted`: use only after the runbook checklist has recorded
  evidence.
- `Live approved`: use only after a separate human approval enables real-money
  configuration.

See [`live-operations.md`](live-operations.md) for recovery, emergency controls,
known limits, and the credentialed acceptance checklist.
