# Autonomous occasion recovery — the persisted state machine

**Status:** design note for the restart-safe recovery path. No time constants,
no leases, no heuristics: recovery reads durable state and nothing else.

## The question recovery must answer

After a restart, for each claimed occasion whose request is still pending:
**can this occasion be replayed without any possibility of a duplicate paid
provider call?** Replay is permitted only when the persisted records *prove*
the answer is yes. Where they cannot, the occasion is retained for inspection
rather than replayed or dropped.

## What the records held before this change

`cognition_opportunities.outcome` moved `created → reserved → <terminal>`.
`autonomous_spend.outcome` moved `reserved → settled | unmeasured`.

That is not enough. A spend row left at `reserved` is **indistinguishable**
between two histories:

- the process died after `reserve()` and before `model.complete()` — nothing
  was sent, replay is free and safe;
- the process died after `model.complete()` began — the provider may have run,
  been billed, and possibly answered.

Both leave exactly the same bytes on disk. So one mechanical fact is missing,
and exactly one is added: **whether dispatch was started.**

## The added state

`autonomous_spend.outcome` gains `dispatched`, written durably *immediately
before* `model.complete(request)` and committed before the call is made.

Ordering matters and is the whole guarantee. Writing it before the call means a
crash at any instant during the call leaves `dispatched` on disk. The converse
is what makes recovery decidable: a row still at `reserved` proves the provider
was never reached, because the write always precedes the call.

This is one mechanical fact with a single objectively correct value. It
interprets nothing.

## The legal states

| # | Opportunity row | Spend row | Proven | Recovery |
| --- | --- | --- | --- | --- |
| 1 | `created` | none | claimed, nothing reserved, nothing sent | **reclaim** — delete the row; the request matures again |
| 2 | `reserved` | `reserved` | reserved, provider never reached | **reclaim** — the reservation stays withdrawn for the day; the request matures again |
| 3 | `reserved` | `dispatched` | provider may have run | **never reclaim** — retained as `unreconciled` for inspection |
| 4 | terminal (`finished_silently`, `responded`, `error`, `refused_*`) | any | the turn happened, or was refused before spend | **never reclaim** |
| 5 | released by the ordinary failure path | n/a | the turn failed before or during a handled error | unchanged — already reclaimed at the time |

State 3 is the conservative case D-024 requires. A duplicate paid cognition
turn for a request AL/X made once is worse than a cognition she asked for and
did not get, because the second is visible and the first is not.

## Why the reservation is not returned on reclaim

A reclaimed occasion in state 2 leaves its withdrawal standing for the day.
That is deliberate. The spend ledger already refuses to return money it cannot
account for, and an unreconciled reservation staying withdrawn is the direction
a ceiling may safely fail in. The alternative — returning it on the strength of
a state we are recovering from — would let a crash loop refund itself.

## What recovery does not do

It has no notion of age, timeout, lease, staleness or priority. It never reads
the note, the thought, the goal, the origin's meaning, or anything semantic. It
compares two persisted `outcome` values and acts on the table above.
