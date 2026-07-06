# Options Execution Roadmap

This roadmap defines how Silver Bullet `TradeSignal` objects should eventually
route into an options engine. It is policy-level documentation only; live options
contract selection and execution are not implemented yet.

## Why Generic OTM Contracts Are Not The Default

Generic out-of-the-money contracts can erase the strategy edge because they
combine low delta sensitivity, fast theta decay, wide spreads, and poor fill
quality. The Silver Bullet setup is a short-duration directional delivery model,
so the option profile must respond quickly to index movement and must have
controlled premium risk.

The execution engine should treat the futures signal as the source of direction,
target, stop geometry, and timing. Options are an execution wrapper, not a
separate signal.

## Profile 1: High-Delta Short-Dated Long Contracts

**Use case:** closest SPY/QQQ options proxy for a futures-like directional scalp.

**Execution profile:**

- Buy deep ITM or high ATM calls for long signals.
- Buy deep ITM or high ATM puts for short signals.
- Target delta range: `0.65` to `0.75`.
- Target expiry: `0DTE` or `1DTE`.
- Preferred holding time: roughly `10` to `30` minutes.

**Why it fits Silver Bullet:**

High-delta contracts mimic the futures move more closely than low-delta OTM
contracts. If the second FVG triggers and price accelerates toward the draw on
liquidity, most of the option move is intrinsic and immediate. Gamma can improve
the payoff as the contract moves deeper ITM, while the short holding period keeps
theta decay less dominant.

**Required selection filters:**

- Proxy symbol mapped from the futures signal, such as `ES -> SPY` or `NQ -> QQQ`.
- Delta between `0.65` and `0.75`.
- Tight bid/ask spread relative to premium.
- Sufficient volume and open interest.
- Premium risk within the configured trade budget.
- Expiry aligned with the session and account rules.

## Profile 2: Target-Defined Vertical Debit Spreads

**Use case:** risk-defined trade when the DOL target is explicit and premium cost
or IV crush risk matters.

**Execution profile:**

- Buy the ATM call or put in the signal direction.
- Sell an OTM call or put near the strike corresponding to the DOL target.
- Use the DOL strike as the natural profit cap.
- Prefer liquid strikes with clean spread markets.

**Why it fits Silver Bullet:**

The strategy already defines the target as an un-swept liquidity level. Selling
the strike near that DOL target reduces premium outlay and dampens IV-crush risk,
especially if entry occurs after an oversized displacement candle. This profile
fits a target-defined model better than buying random OTM contracts.

**Required selection filters:**

- DOL-to-proxy strike translation.
- Maximum debit per spread.
- Minimum reward-to-risk after fees and spread.
- Strike liquidity on both legs.
- Maximum acceptable distance between DOL proxy price and short strike.
- Spread width and max loss compatible with account risk.

## Profile 3: 0DTE Momentum Scalp

**Use case:** aggressive asymmetry during fast repricing windows.

**Execution profile:**

- Buy slightly OTM calls or puts.
- Target delta around `0.40`.
- Restrict to Macro Open or NY AM.
- Require expected delivery of at least `10` index points.
- Treat as a premium-risk scalp with fast invalidation.

**Why it fits Silver Bullet:**

This profile depends on directional velocity. When price reprices out of the
first FVG and the second directed FVG forms, delta and vega can expand quickly.
If price reaches the DOL within minutes, the option payoff can be highly convex.
The trade-off is a lower tolerance for slow movement, wider spreads, and higher
probability of premium decay if the setup stalls.

**Required selection filters:**

- Session must be Macro Open or NY AM.
- Expected delivery must be at least `10` index points.
- Delta near `0.40`.
- Premium capped to a small account-risk budget.
- Tight spread and active tape.
- Immediate cancellation or exit when the futures stop condition is reached.

## Contract Selection Inputs

A future options module should require these inputs before creating an order
candidate:

- `signal`: the `TradeSignal` emitted by the futures strategy.
- `proxy_symbol`: mapped equity or ETF proxy, such as `SPY` or `QQQ`.
- `direction`: call for long, put for short.
- `dol_target`: futures target translated to proxy price when applicable.
- `expected_delivery_points`: signal target distance.
- `session`: London, Macro, NY AM, or NY PM.
- `expiry_policy`: `0DTE`, `1DTE`, or next liquid expiry.
- `delta_range`: desired option delta range.
- `max_spread_pct`: spread-quality guard.
- `max_premium`: premium-risk cap.
- `min_volume` and `min_open_interest`: liquidity guards.
- `risk_cap`: account-level maximum loss allowed for the order.

## Execution Priority

1. Use high-delta short-dated long contracts as the default options proxy because
   they most closely preserve the original futures signal behavior.
2. Use target-defined debit spreads when the DOL target maps cleanly to a liquid
   strike and reduced premium outlay is more important than uncapped upside.
3. Use 0DTE momentum scalps only when the window, expected delivery, and tape
   quality justify the extra convexity risk.

## Implementation Status

Current repository support:

- The strategy emits `TradeSignal` objects with target, stop, direction, session,
  and signal time.
- The context gate can accept or reject signals before execution.
- The Alpaca proxy adapter can preview or guard order submission.

Not implemented yet:

- Options chain ingestion.
- Greeks-based contract selection.
- Spread construction.
- Bracket or exit-order synchronization.
- Paper/live options fills and reconciliation.
