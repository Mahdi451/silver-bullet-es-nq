# Completed Options Module Plan

This document records the plan used to implement routing from accepted Silver
Bullet signals into options candidates. The module and its Alpaca provider
adapter now exist; the plan remains as design rationale and a non-goal record.

## Goal

Add an options selection layer that converts a gated `TradeSignal` into one or
more risk-filtered option order candidates while keeping the core futures
strategy unchanged.

## Implemented Module Boundary

Module name:

```text
src/silver_bullet/options.py
```

The module sits after `SignalContextGate` and before broker execution:

```text
TradeSignal -> SignalContextGate -> OptionsSelector -> BrokerExecutor
```

## Core Data Types

The implementation introduces provider-neutral types before the Alpaca binding.

```python
OptionContract
OptionQuote
OptionChainSnapshot
OptionsProfile
OptionOrderCandidate
OptionsSelectionConfig
```

These types capture:

- symbol and underlying;
- call or put;
- strike;
- expiry;
- bid, ask, mid, and spread;
- delta, gamma, theta, vega, and implied volatility when available;
- volume and open interest;
- max premium or max debit;
- estimated max loss;
- target profile: high-delta long, debit spread, or 0DTE momentum scalp.

## Selector Inputs

The selector requires:

- accepted `TradeSignal`;
- proxy mapping such as `ES -> SPY` and `NQ -> QQQ`;
- latest proxy price;
- translated DOL target if using spreads;
- session name;
- expected delivery points;
- account risk budget;
- option chain snapshot;
- selected execution profile.

## Selection Flow

1. Map the futures signal to a proxy underlying.
2. Choose call or put from signal direction.
3. Filter expiries according to the profile:
   - `0DTE` or `1DTE` for high-delta contracts;
   - nearest liquid expiry for debit spreads;
   - `0DTE` only for the momentum scalp.
4. Filter contracts by delta, spread quality, liquidity, and premium cap.
5. Rank candidates by fit:
   - high-delta long: closest delta to target and tightest spread;
   - debit spread: best risk/reward to the DOL target;
   - momentum scalp: best liquidity and convexity within premium cap.
6. Return candidates, not orders, so broker execution remains decoupled.

## Risk Controls

The options module rejects candidates when:

- spread is too wide;
- volume or open interest is too low;
- premium exceeds the risk cap;
- expiry is incompatible with account rules;
- DOL-to-strike mapping is too imprecise for a spread;
- signal is stale;
- session is not allowed for the selected profile;
- expected delivery is below the profile threshold.

## Testing Requirements

Use fake option-chain snapshots only. Tests should cover:

- long and short signal mapping;
- high-delta selection;
- debit spread construction near a DOL strike;
- 0DTE momentum scalp gating by session and expected delivery;
- rejection for stale signals, wide spreads, low liquidity, and excessive
  premium;
- deterministic candidate ranking.

No tests should call live broker or market-data APIs.

## Implementation Sequence

1. Completed: provider-neutral option chain and candidate data types.
2. Completed: pure selection logic with fake snapshots.
3. Completed: live configuration example with profile and quality settings.
4. Completed: integrated dry-run order previews.
5. Completed: Alpaca option-chain provider after selector tests.
6. Completed: account-aware single-leg and multi-leg broker construction.

## Current Non-Goals

- No automatic exercise or assignment handling.
- No live Greeks polling loop.
- No portfolio margin modeling.
- No claim of paper or live operational acceptance without credentialed runs.
