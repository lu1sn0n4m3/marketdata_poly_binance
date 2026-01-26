# CLAUDE.md — Gateway (Order I/O + Priority + Rate Limiting)

This directory is the ONLY boundary that talks to the exchange API.
It must remain **thin, deterministic, and safety-biased**.

The gateway does not decide what to trade. It only executes requested actions
(PLACE / CANCEL / CANCEL_ALL) under strict concurrency and rate-limit rules.

If you change gateway behavior casually, you can place stale orders after an emergency stop.

---

## What the Gateway is responsible for

- Accept non-blocking, thread-safe submissions:
  - `submit_place(spec)`
  - `submit_cancel(order_id)`
  - `submit_cancel_all(market_id)` (highest priority)
- Enforce action ordering and priority via a single internal queue (`ActionDeque`).
- Enforce rate limiting for *normal* actions.
- Execute actions in a dedicated worker thread.
- Emit results (success/failure + retryable flag) back to the Executor via queue/callback.

---

## What the Gateway must NEVER do

- Never contain strategy logic, pricing, inventory logic, or reconciliation.
- Never “help” by reordering actions beyond the explicit priority rules.
- Never retry actions silently with hidden state (retries are an Executor decision).
- Never block the caller thread on network I/O.
- Never assume the exchange is consistent (idempotency and failures exist).

If behavior depends on “the exchange usually…”, you are writing a bug.

---

## The safety contract (the reason this module exists)

### CANCEL_ALL is a safety interrupt
When CANCEL_ALL is enqueued:
1. It must jump to the **front** of the queue.
2. It must clear pending PLACE actions (configurable, but default must be True).
3. It must not be rate-limited.

Rationale:
- A PLACE executed after a CANCEL_ALL decision is a real risk event.
- A redundant CANCEL after CANCEL_ALL is harmless.

This contract must be preserved even under high load.

### Dropping actions is allowed (and must be explicit)
If queue limits are enforced, the gateway may drop actions (especially PLACEs).
If an action can be dropped, the rest of the system must not assume:
- “returned action_id implies it will execute”
- “every submission produces a result event”

The Executor must be robust to dropped actions and reconcile state from acks/sync.

Do not “fix” this by making submissions block; blocking callers can deadlock the trading system.

---

## Concurrency model (non-negotiable)

- Public `submit_*()` methods:
  - must be thread-safe
  - must be non-blocking (no network I/O)
- Exactly one worker thread performs network calls.
- `ActionDeque` is the synchronization point; it must remain correct under concurrency.
- Worker must be stoppable and must not hang on shutdown.

---

## Rate limiting rules

- Normal actions (PLACE, CANCEL) are rate-limited by `min_action_interval_ms`.
- CANCEL_ALL is NOT rate-limited.
- Rate limiting happens in the worker, not in submitters.

Do not add “smart batching” unless you can prove it preserves CANCEL_ALL interrupt semantics.

---

## Error handling contract

Gateway normalizes errors into:
- success/failure
- retryable vs non-retryable

Gateway must not:
- decide retry policy
- silently retry without telling the executor
- swallow errors

Executor decides what to do with retryable failures.

---

## Idempotency and ordering assumptions

- Network calls may time out and still succeed server-side.
- Duplicate cancels and cancel-alls must be safe to send.
- The system must not rely on strict in-order exchange processing beyond what the API guarantees.

Gateway should prefer idempotent identifiers where supported (e.g., client order IDs).
If you add new action types, consider idempotency first.

---

## Where changes usually go wrong

Common regressions:
- allowing PLACEs to remain queued after CANCEL_ALL
- rate limiting CANCEL_ALL
- adding retries inside the gateway
- making submitters block on queue/network
- “optimizing” queue logic in a way that breaks priority semantics
- assuming every submission emits a result event (not true if dropping is enabled)

If you modify queue behavior, add tests that simulate:
- high backlog + emergency cancel-all + continued submissions.

---

## Files and their intended roles

- `action_deque.py`: correctness-first priority queue.
  - CANCEL_ALL goes to front; optionally drops pending PLACEs.
- `worker.py`: the only place where network I/O happens.
  - enforces rate limiting
  - executes actions serially
  - emits results
- `gateway.py`: public interface.
  - thread-safe submission methods
  - lifecycle control (`start/stop`)
  - exposes stats

Keep these responsibilities separate.

---

## Testing philosophy

Tests should focus on invariants:
- CANCEL_ALL preempts and clears PLACEs
- worker does not execute stale PLACEs after cancel-all
- rate limiting applies only to normal actions
- thread start/stop is reliable
- queue full behavior is deterministic and observable

Do not rely on timing-only tests without deterministic hooks;
racey tests that “usually pass” are worse than no tests.