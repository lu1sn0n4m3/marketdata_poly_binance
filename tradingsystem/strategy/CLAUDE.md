# CLAUDE.md — Strategy (Intent Generation in YES-Space)

This directory contains trading logic that produces **desired quotes**.
Strategies do not place/cancel orders and do not manage order lifecycle.
They publish **intent**; the Executor enforces venue mechanics and safety.

This file exists to prevent mixing responsibilities and creating unsafe feedback loops.

---

## What Strategy is responsible for

- Read current market state (Polymarket book snapshot, Binance snapshot, time to expiry, inventory summary, fair value).
- Produce a `DesiredQuoteSet` in **YES-space**:
  - bid_yes: “I want to BUY YES at price p for size s”
  - ask_yes: “I want to SELL YES at price p for size s”
  - mode: STOP / NORMAL / CAUTION / ONE_SIDED_*

Strategy output is **advisory**. Executor may ignore or override it.

---

## What Strategy must never do

- Never place orders, cancel orders, or call the gateway.
- Never track open orders, pending cancels, or queue position.
- Never implement complement routing, no-short rules, min-size rules, reservations, or SELL overlap logic.
  Those belong in `executor/` exclusively.
- Never assume fills occurred or “pretend” inventory changed before the Executor confirms it.

If you feel tempted to add any of the above, you are drifting into executor responsibilities.

---

## “Stateless” means: no lifecycle state

`compute_quotes(input) -> DesiredQuoteSet` should behave like a pure function of current input.

Acceptable local state (if truly necessary):
- bounded smoothing / EWMA / hysteresis
- rolling features derived from market data
- configuration and parameters

Unacceptable state:
- order IDs, “working” flags, cancel status
- “I already placed this quote” type state
- any attempt to “reserve” inventory inside strategy

Order lifecycle belongs to the Executor. Strategy should not try to outsmart it.

---

## YES-space contract (non-negotiable)

Strategies always think in canonical YES prices (0–100 cents).
They do not decide whether the real order will be:
- BUY YES / SELL YES
- BUY NO / SELL NO
- split into 2 orders

Executor’s planner/reconciler materializes intent to legal orders under:
- no-short constraint
- complement symmetry
- min-size constraints
- reservation ledger
- SELL overlap blocking
- queue-preserving replacement rules

If you encode venue mechanics in strategy, you will break determinism and duplicate logic.

---

## Runner + Mailbox semantics (do not “optimize”)

### Runner
`StrategyRunner` runs on a fixed cadence (e.g., ~20 Hz):
- assembles `StrategyInput` via `get_input()`
- calls `strategy.compute_quotes(inp)`
- debounces identical intents
- publishes into mailbox

The runner is allowed to skip publishing when unchanged. That is intentional.

### IntentMailbox
Mailbox is a **single-slot overwrite**:
- it keeps only the latest intent
- older intents are discarded

This is critical: **latest intent wins**. There is no intent queue.
Do not replace it with a buffered queue “for reliability” — that creates stale-action backlog and thrash.

---

## Output expectations (how to avoid unsafe intents)

A correct strategy:
- returns STOP when required inputs are missing/stale
- returns STOP (or tight CAUTION) near expiry if your risk posture demands it
- respects configured position limits using one-sided modes
- uses reason flags for observability/debugging

A strategy should not attempt to express min-size feasibility. Sizes are “wants.”
The Executor will degrade/clip/split/skip due to venue constraints.

---

## How Strategy and Executor interact (important)

- Strategy uses the inventory summary to shape intent (skew/one-sided).
- Executor uses **settled inventory and reservations** to decide what is legal.
- Strategy must tolerate the executor not matching intent exactly (min-size residuals, reduce-first rerouting, safety overrides).
- Strategy should publish frequently enough that fills are incorporated quickly via the next intent.

Do not create a feedback loop that assumes “intent == working orders”.

---

## Where to look when changing things

- `base.py`: Strategy ABC + data contracts (`StrategyInput`, config)
- `runner.py`: runner cadence, debouncing rules, mailbox overwrite behavior
- `default.py`: production strategy logic (fair value, skew, vol/expiry gating)
- `examples/`: dummy strategies for plumbing and live-testing

If you change debounce rules, consider the executor’s “latest intent wins” and fill-handling assumptions.
Over-debouncing can increase stale-quote windows.

---

## Testing philosophy

Test strategies as pure functions:
- given input snapshots + inventory + fair value, verify the intent
- test STOP behavior on missing data and near-expiry flags
- test one-sided transitions at position limits
- test spread widening in shock/high vol regimes

Do not write tests that assume specific order placement behavior. That belongs to executor tests.