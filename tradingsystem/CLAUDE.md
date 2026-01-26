# CLAUDE.md — Binary CLOB Market-Making System (Polymarket)

This repository implements a production-oriented market-making framework for binary YES/NO event markets on polymarket, using a reference market (e.g. Binance BTC) for regime detection and adverse selection control.

The market goes for one hour and the resolution definition is:
"
This market will resolve to "Up" if the close price is greater than or equal to the open price for the BTC/USDT 1 hour candle that begins on the time and date specified in the title. Otherwise, this market will resolve to "Down".

The resolution source for this market is information from Binance, specifically the BTC/USDT pair. The close « C » and open « O » displayed at the top of the graph for the relevant "1H" candle will be used once the data for that candle is finalized.

Please note that this market is about the price according to Binance BTC/USDT, not according to other sources or spot markets.
"

This file exists to orient an LLM quickly and prevent unsafe or conceptually wrong changes.

---

## What this system actually does

The system market-makes a binary event with complementary tokens (YES / NO) under a **no-short constraint**.

Key idea:
- The **Strategy** thinks in one canonical instrument: synthetic YES probability/price and posts its **intent** -  the desired bid/ask prices and sizes. 
- The **Executor** is the single writer that converts those synthetic intents into **legal orders**
  on YES and/or NO, respecting inventory, complement mapping, and risk constraints.

Strategy never places orders.
Executor may override or ignore Strategy when safety requires it.

---

## Architectural truth (do not get this wrong)

- `executor/` is the core of the system taking in **Strategy** intents and converts them to actual trades.
- `strategy/` is the actualy trading strategy only thinking in YES/NO space.
- `gateway/` is a thin I/O layer with no business logic that actually posts the order to Polymarket through REST.
- Feeds update caches; caches are read-only to Strategy.
- The Executor owns:
  - open orders
  - pending cancels / placements
  - inventory state
  - cooldowns and kill switches

There must only ever be **one writer** to trading state.

---

## Canonical instrument model

All strategy intent is expressed in **YES space**.

- A synthetic YES bid = desire to buy YES exposure.
- A synthetic YES ask = desire to sell YES exposure.
- NO orders exist only as a *materialization choice* made by the Executor.

Complement mapping:
- YES price `p` ↔ NO price `1 - p`
- Executor prefers to **sell existing inventory first** before buying complement,
  to avoid unnecessary gross exposure.

If logic related to complement mapping, no-short enforcement, or inventory-aware routing
appears outside the Executor, that is a bug.

Important to note: the YES (UP) and NO (DOWN) markets are complementary in the sense that the **bid** on YES is always the **ask** on NO, and vice versa. This is important when considering *selling YES* when the inventory of YES is 0. Then an economically equivalent trade is *BUY NO*. However, the executor optimized for inventory minimization.

---

## Hard invariants (non-negotiable)

1. Strategy code must not:
   - track open orders
   - assume fills
   - issue cancels or placements
2. Gateway code must not:
   - infer intent
   - retry intelligently
   - mutate state
3. Executor code must:
   - remain single-threaded or actor-like
   - reconcile desired vs actual state deterministically
   - be able to cancel-all and stop safely at any time
4. No component may assume that WS messages are complete, ordered, or timely.

---

## Risk and safety philosophy

This system prioritizes **not dying** over capturing every tick.

Executor-level overrides exist for:
- stale feeds
- gateway errors
- cancel/ack timeouts
- reference-market shocks
- near-expiry instability

Strategy output is always treated as *optional advice*.

---

## What not to “clean up”

- Do not merge Strategy and Executor responsibilities.
- Do not add shared mutable state between components.
- Do not move complement logic into Strategy.
- Do not add clever abstractions inside `gateway/`.

If something feels redundant, it is probably deliberate.

---

## Entry points and scripts

Files like `run_*.py`, `debug_*.py`, and ad-hoc tests are operational tooling.
They are not the architecture. Do not infer design constraints from them.

---

## How to run scripts

If you actually want to run some code, make sure to always use `source .venv/bin/activate` to activate the virtual environment.

---

## If you are changing this system

Before modifying logic, ask:
- Does this preserve the single-writer model?
- Does this increase gross exposure in edge cases?
- Does this make failure modes harder to reason about?

After refactoring code make sure to:
- Always run the tests in the /tests folder to make sure you didn't break core logic.
- Consider changes to relevant README.md files to keep the documentation up-to-date.
