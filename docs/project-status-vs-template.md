# Project Status vs. Target Presentation Template

This document maps the current repository to the target automated Silver Bullet
trading-engine presentation. It is intentionally explicit about what is built
today versus what belongs to the next phases.

## Summary

The repository currently contains a deterministic ES/NQ research and backtesting
engine with optional external context and proxy-execution boundaries. It is not
yet a live WebSocket-driven options or futures execution system.

| Area | Current status | Notes |
| --- | --- | --- |
| Historical ES/NQ ingestion | Implemented | CSV schemas, strict validation, timezone normalization, contract metadata, and gap reporting are in `src/silver_bullet/data.py`. |
| Liquidity model | Implemented | Prior day/week levels, tap tracking, and CME trading day/week logic are in `src/silver_bullet/levels.py`. |
| Silver Bullet state machine | Implemented | ATR displacement, swing break, first FVG, mitigation, second FVG, session gating, and signal timing are in `src/silver_bullet/strategy.py`. |
| Backtest execution | Implemented | Deterministic fills, fees, slippage, scale-out, DOL exits, stops, forced close, risk checks, and ledgers are in `src/silver_bullet/execution.py`. |
| Analytics and walk-forward primitives | Implemented | Metrics, tear sheets, ledgers, charts, and parameter-grid primitives are in `src/silver_bullet/analytics.py` and `src/silver_bullet/optimization.py`. |
| Unusual Whales context | Boundary implemented | REST flow-alert normalization and context scoring exist in `src/silver_bullet/unusual_whales.py`; full historical proxy-data ingestion is not built yet. |
| Signal context gate | Implemented | Provider-neutral accept/reject decisions exist in `src/silver_bullet/context.py`. |
| Alpaca proxy execution | Boundary implemented | Dry-run previews and guarded submission boundaries exist in `src/silver_bullet/alpaca.py`; full options execution and live order reconciliation are not built yet. |
| Live WebSocket ingestion | Not implemented | Future Phase 2 work. |
| Advanced bracket orders | Not implemented | Future Phase 2 work after paper-trading account reconciliation is defined. |
| Headless M4 command center | Not implemented | Future operator dashboard concept. |
| Live futures broker routing | Not implemented | Future Phase 3 work after broker selection. |
| Options contract selection | Not implemented | Planned as a separate module after policy and presentation docs are approved. |

## Phase Framing

### Phase 1: Research Core And Proxy Boundaries

This is the current repository state.

- Historical ES/NQ backtesting.
- Deterministic, zero-look-ahead signal generation.
- Conservative simulated execution and account analytics.
- Optional Unusual Whales context gate.
- Optional Alpaca proxy-order preview/submission boundary.

### Phase 2: Paper Trading And Options Proxy Execution

This is the next implementation target.

- Live or near-live SPY/QQQ proxy data ingestion.
- Alpaca paper account checks, order-state synchronization, and reconciliation.
- Options contract selection based on delta, expiry, spread quality, premium risk,
  and DOL target geometry.
- Bracket or bracket-like order management where the broker supports it.
- Operator logs and health checks.

### Phase 3: Direct Futures Deployment

This is future architecture and should not be presented as implemented.

- Direct ES/NQ live feed.
- Low-latency hosting or VPS deployment.
- Futures broker adapter, such as Tradovate, Tradier, or another selected broker.
- Live contract routing with account-level risk enforcement.
- Production-grade monitoring and kill-switch behavior.

## Presentation Guidance

When discussing the template, use status labels:

- `Implemented`: available in the current repository.
- `Boundary implemented`: interface exists, but complete production behavior is not
  built.
- `Planned`: design target for a later phase.
- `Not in scope yet`: should not be implied as current behavior.

This keeps the presentation ambitious without overstating the current codebase.
