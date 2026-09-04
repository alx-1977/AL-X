# Phase 8 — the runtime hook for due cognition

**Status:** DESIGN ONLY, revised. Not implemented, not enabled, no provider call.
**Authority:** D-024, D-024a, EX-001. Nothing here amends any of them.
**Supersedes:** the session-scoped proposal, which was wrong — see §2.

## 1. The distinction, recorded explicitly

Polling a clock is deterministic mechanics. Invoking the Core is cognition.

**The 30-second interval is a mechanical noticing interval only.** It means: *a
matured `not_before` request may be noticed up to roughly 30 seconds later.* It
does **not** mean *AL/X thinks every 30 seconds.*

When no objective cognition opportunity exists, a tick produces **zero Core
calls, zero claims, zero reservations, zero provider calls.** It reads two
timestamps through an indexed query and returns nothing. If AL/X never requests
a future cognition, the runtime ticks forever and invokes her never.

## 2. Why the session-scoped proposal was wrong

The earlier draft put the producer inside `VoiceSession.exchange`, so autonomous
cognition existed only while Friedl had an open voice exchange. That contradicts
D-024: *while the AL/X runtime is running, AL/X is continuously present.* It
would have made her continuity depend on whether someone was currently
listening, and it avoided the no-live-transport case by arranging for it never
to arise — which is not solving it.

Voice transport is an input and output channel. It is not what determines
whether she exists.

## 3. Exact ownership

**The producer's home is `run()` in `src/alx/bootstrap/live_voice.py`.**

That function is already the process-lifetime scope: it builds every durable
store, the Core, the gateway, the ledgers and the continuity source, then awaits
`server.serve_forever()`. The gateway already outlives any individual voice
connection.

The producer becomes one more task in that scope:

```python
async with asyncio.TaskGroup() as tasks:     # process lifetime
    tasks.create_task(server.serve_forever())
    tasks.create_task(due_cognition.run(runner))   # the tick
```

No daemon, no service, no second scheduler, no second Core ingress.

### The Core-turn lock

**It is already process-lifetime; it is merely declared in the wrong class.**
`VoiceSession` is constructed **once** in `run()` and shared across every
websocket connection, so `self._core_turn_lock` already serialises turns across
all connections. Only its ownership reads as session-local.

**Smallest Law-0-compliant move:** create the single `asyncio.Lock` in `run()`
and pass it to both `VoiceSession` and the due-cognition producer. One lock
object, one serialization authority, no duplicate. `VoiceSession` keeps using
the lock it is given rather than making its own, which is a two-line change and
removes the false impression that turn serialization belongs to voice.

Conceptual ownership, as approved:

```
AL/X runtime (run())
  ├─ one cognition-opportunity / event ingress   (CognitionOpportunitySource)
  ├─ one Core-turn serialization boundary        (the single asyncio.Lock)
  ├─ person or autonomous Core turn              (ConversationGateway → CoreAgent)
  └─ optional live transport for delivery        (VoiceSession, may be absent)
```

## 4. How both turn kinds enter the same serialized path

```
person speech ──► VoiceSession.exchange ──┐
                                          ├─► async with core_turn_lock
due cognition ──► DueCognitionSource ─────┘        └─► ConversationGateway
                    │                                     └─► CoreAgent.process
                    └─ tick: due_opportunities()                (origin decides
                       (no model, no cost)                       Sol or Luna)
```

Both acquire the same lock object. Neither can start while the other holds it.
A person turn arriving mid-cognition waits rather than being dropped, and an
autonomous turn waits for Friedl to finish speaking.

## 5. Transport attach and detach

The producer never observes transport state, because doing so would make
cognition conditional on someone listening.

- **Transport absent:** cognition proceeds. `FINISHED_SILENTLY` produces
  nothing, as always. `RESPONDED` follows §6.
- **Transport attaches mid-turn:** nothing changes; the turn already holds the
  lock and completes.
- **Transport detaches mid-turn:** the turn completes. Delivery fails, and §6
  applies.
- **Many connections:** already one shared `VoiceSession` and now one shared
  lock, so concurrency is unchanged.

## 6. No live transport, on a RESPONDED autonomous turn

Phase 7's approved behaviour, unchanged:

- persist **the fact** that an autonomous response was undeliverable;
- do **not** replay or queue the original prose;
- do **not** build a delayed-message queue;
- surface the unresolved fact to the Core on a later opportunity or person turn;
- the Core decides whether it still wants to say anything, composing fresh
  wording at that time.

No importance filter, no notification policy, no quiet hours. The undelivered
fact is a durable flag on the occasion record, not a stored message.

## 7. Shutdown and restart

**Shutdown.** `TaskGroup` cancels the producer with the server. A tick in
progress is a local read and is safe to cancel. A turn in progress holds the
lock; cancellation during it leaves a durable claim, which is exactly the case
recovery already handles.

**Restart.** Unchanged and already implemented: `run()` calls
`cognition_source.recover(autonomous_budget)` before serving. Occasions with no
dispatched reservation are reclaimed and mature again; dispatched ones are
retained as `unreconciled` and never replayed.

## 8. The 30-second due check

```python
class DueCognitionSource:
    """Notices matured requests. Decides nothing."""

    async def run(self, runner) -> None:
        while True:
            await asyncio.sleep(self._interval_seconds)   # 30
            # Off, or nothing due, returns () and costs nothing.
            for opportunity in await asyncio.to_thread(
                self._source.due_opportunities
            ):
                async with self._core_turn_lock:
                    await asyncio.to_thread(runner.run_one, opportunity)
```

`due_opportunities()` already exists, already returns `()` when the master
switch is off, and already excludes claimed occasions. The tick is an indexed
read over `(status, not_before)`.

It never reads the note, thought content, goals, memory, the notebook, topic,
importance, priority or sender meaning. The existing architecture-gate rule on
`continuity/source.py` extends to this module unchanged.

## 9. Supervised first activation

Not unattended observation. One deliberate `SELF_REQUESTED` turn through the
**complete production path** — no calibration harness.

**Enable:**
1. `ALX_AUTONOMOUS_PROVIDER=openai`, `ALX_AUTONOMOUS_MODEL=gpt-5.6-luna`,
   `ALX_AUTONOMOUS_EFFORT=max` (configuration accepts nothing else, per EX-001).
2. `AUTONOMOUS_COGNITION_DAILY_BUDGET_USD=0.0816` — **one worst-case
   reservation.** A commissioning limit, not a frequency rule and not
   architecture.
3. Start the runtime with Friedl present.
4. AL/X is asked, in ordinary conversation, to request a future cognition a few
   minutes out. She writes her own note; nobody writes it for her.
5. Observe: tick notices at ≤30s, claim, request build, 96k bound, $0.0816
   reservation, Luna Max, settlement, relay, outcome, honour, and speech or
   silence by her judgement.

**Stop:** the second turn cannot occur — the day's budget is exhausted by the
first reservation and further autonomous cognition is refused. The stop is
mechanical, not a promise to remember.

**Then:** report complete evidence and stop. Moving to $0.5405 is a separate,
deliberate decision. It is not raised silently.

## 10. Files that would change

| File | Change |
| --- | --- |
| `src/alx/continuity/due_source.py` *(new)* | the 30-second tick |
| `src/alx/bootstrap/live_voice.py` | own the lock; construct the runner; `TaskGroup` |
| `src/alx/interfaces/live_voice.py` | accept the shared lock instead of creating one |
| `src/alx/bootstrap/autonomous.py` | `run_one(opportunity)` beside `run_due()` |
| `src/alx/continuity/ledger.py` | the undelivered-response fact |
| `src/alx/config/settings.py` | the interval, as plumbing |

## 11. Tests and gate additions

- a tick with nothing due makes zero model calls, claims and reservations;
- a tick with the switch off leaves every request pending;
- one due request produces exactly one turn, not one per tick;
- **an autonomous turn runs with no voice session ever opened** — the property
  the previous design could not have;
- a person turn and an autonomous turn cannot overlap, proven against the one
  shared lock;
- transport detaching mid-turn does not abort it;
- a RESPONDED turn with no transport persists the fact and queues no prose;
- restart mid-turn reclaims through the existing recovery path;
- exhausting the commissioning budget refuses the next turn;
- **gate addition:** exactly one `asyncio.Lock` may be constructed for Core
  turns, so a second serialization authority cannot appear;
- **gate addition:** `continuity/due_source.py` inherits the non-semantic rule;
- mutation: remove the switch check, the shared lock, or the claim → tests fail.

## 12. Calibration evidence

One representative production-shaped request, measured against the live provider
on 2026-09-03. This validates the request/provider contract **for that request**.
It is **not** proof that every future prompt carries a fixed 5.24× token margin:
a prompt with denser encoding, unusual scripts or long verbatim quotation will
tokenize differently.

| Measurement | Value |
| --- | --- |
| Serialized request | 51,024 bytes |
| `input_token_upper_bound` | 59,451 |
| Provider-reported input | 11,343 |
| Cached input | 11,011 |
| Cache-write | 329 |
| Output | 555 |
| Reasoning | 516 |
| Calculated cost | $0.001035 |
| Strict schema | accepted |
| `max_output_tokens=32,000` | accepted |
| Normalization | matched raw usage exactly |

Establishes: the heuristic was conservative here; the 8,426-byte strict schema
is accepted by Luna at `max`; the output cap is accepted; `cache_write_tokens`
is genuinely reported, so the fourth pricing rate is real rather than assumed;
and our normalization and pricing reconcile with the provider to six decimals.

Does not establish: conservatism for every prompt. The 96k ceiling and the
fail-closed refusal remain the protection, unchanged.
