# Automated Silver Bullet Trading Engine Presentation Outline

This outline is designed for a technical slideshow. It presents the current
repository honestly while showing the path toward the full automated execution
engine.

## Slide 1: Thesis And System Vision

**Core technical message:** Silver Bullet execution should be local,
deterministic, event-driven, and free from third-party webhook latency.

**Featured visual:** `docs/images/blueprints/event-driven-system-architecture.png`

**Speaker notes:**

- The project starts with a trustworthy deterministic research engine before
  adding live execution.
- The strategic goal is direct event processing: data arrives, state updates, a
  signal is emitted, and risk controls decide whether execution is allowed.
- Phase labels now distinguish local implementation from credentialed acceptance:
  the research, live streams, options route, futures route, and dashboard exist,
  while paper/demo soak evidence remains.

## Slide 2: Current Implemented Research Core

**Core technical message:** The current codebase already covers the full
historical research path for ES/NQ.

**Featured visual:** `docs/images/project-state-flow.svg`

**Speaker notes:**

- Historical bars flow through strict ingestion, causal liquidity levels, the
  Silver Bullet state machine, deterministic execution, and analytics.
- The system emits repeatable `TradeSignal` objects with target, stop, session,
  displacement, and FVG metadata.
- This phase protects against future-data leakage before any live integration is
  trusted.

## Slide 3: Silver Bullet State Machine

**Core technical message:** The strategy is implemented as a rule-based state
machine rather than discretionary chart reading.

**Featured visual:** `docs/images/blueprints/strategy-sequence-diagram.png`

**Speaker notes:**

- The engine hunts a draw on liquidity using untapped PDH, PDL, PWH, or PWL.
- Displacement requires a body greater than the configured ATR multiple and a
  structural swing break.
- A signal only fires after the first FVG, mitigation, and second directed FVG
  sequence completes inside the active session.

## Slide 4: Data And Liquidity Model

**Core technical message:** Data validation and level causality are part of the
edge because bad timestamps or future levels can corrupt the backtest.

**Featured visual:** `docs/images/silver-bullet-indicator-overview.svg`

**Speaker notes:**

- The loader rejects duplicate timestamps, invalid OHLC, non-finite values,
  off-minute bars, negative volume, and off-tick prices.
- Prior-day and prior-week levels are published only after completed periods and
  become ineligible once tapped.
- Contract changes and unexpected gaps reset technical state so patterns do not
  span unsafe data boundaries.

## Slide 5: Execution Simulation And Risk Accounting

**Core technical message:** The backtester models fills, costs, risk, and account
state separately from signal generation.

**Featured visual:** `docs/images/silver-bullet-bullish-setup.svg`

**Speaker notes:**

- Signals are intentions; the execution layer handles missing execution bars,
  slippage, fees, stops, scale-outs, DOL targets, and forced closes.
- The model uses conservative stop-first handling when a bar touches both stop
  and target.
- Outputs include fills, trades, equity, cancellations, risk violations, metrics,
  and tear-sheet artifacts.

## Slide 6: Unusual Whales Context Gate

**Core technical message:** Equity/options flow is a context filter around the
futures signal, not a replacement for the Silver Bullet state machine.

**Featured visual:** `docs/images/blueprints/multi-broker-execution-logic.png`

**Speaker notes:**

- ES signals can map to SPY context, and NQ signals can map to QQQ context.
- Current implementation normalizes Unusual Whales option-flow alerts into
  bullish and bearish premium scores.
- The gate rejects missing, stale, wrong-proxy, weak, or conflicting context
  before execution is considered.

## Slide 7: Options Execution Profiles

**Core technical message:** Options execution must preserve the futures signal’s
edge instead of defaulting to weak generic OTM contracts.

**Featured visual:** `docs/images/silver-bullet-bearish-setup.svg`

**Speaker notes:**

- The default implemented profile uses high-delta short-dated calls or puts with
  delta around `0.65` to `0.75`.
- Target-defined debit spreads fit the strategy because the DOL target gives a
  natural short-strike reference.
- The 0DTE momentum scalp is aggressive and should be restricted to Macro Open or
  NY AM with at least 10 points of expected delivery.

## Slide 8: Alpaca Paper And Proxy Execution

**Core technical message:** Alpaca is the guarded SPY/QQQ options route, not the
direct ES/NQ futures broker.

**Featured visual:** `docs/images/blueprints/headless-command-center.png`

**Speaker notes:**

- The adapter fetches option chains, routes selected single/multi-leg candidates,
  and blocks stale, oversized, disabled, or unsafe submissions.
- Persistent order state, account snapshots, reconciliation, synthetic exits,
  and dashboard controls are implemented; credentialed paper acceptance remains.
- Live mode remains behind config opt-in and a second command confirmation.

## Slide 9: Deployment And Multi-Broker Runtime

**Core technical message:** The long-term architecture decouples data ingress,
state-machine logic, and broker execution.

**Featured visual:** `docs/images/blueprints/multi-broker-execution-logic.png`

**Speaker notes:**

- The runtime routes either Alpaca options or explicitly mapped Tradovate futures
  while sharing strategy, risk, persistence, and monitoring.
- Phase 2 and Phase 3 acceptance now means paper/demo sessions, restarts,
  disconnects, rollover, emergency controls, and multi-session soak testing.
- The same state-machine core should remain replayable and testable across
  historical, paper, and live modes.

## Slide 10: Validation Roadmap And Remaining Risks

**Core technical message:** The path forward is acceptance testing, paper
verification, then live deployment only after safety gaps are closed.

**Featured visual:** `docs/images/blueprints/event-driven-system-architecture.png`

**Speaker notes:**

- Remaining research work includes real Databento ES/NQ validation, CME holiday
  hardening, multi-year benchmarks, and manual chart audits.
- Remaining execution work is credentialed acceptance of real feed timing,
  paper/demo fills, partial failures, restart drift, and emergency controls.
- Present implementation, operational acceptance, and real-money approval as
  three separate milestones.
