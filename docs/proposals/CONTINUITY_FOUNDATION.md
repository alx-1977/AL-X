# Proposal — Continuity foundation: cognition opportunities

**Status:** PROPOSAL. Not approved, not implemented. Requires Friedl's approval as D-024
before any code is written.
**Supersedes:** `CONTINUITY_TURNS.md`, deleted in the same change. See §10 for what was
wrong with it and why the correction is structural rather than cosmetic.
**Authority:** Subordinate to the Laws of AL/X, `IDENTITY_AND_MEMORY.md`, and D-023.

## The correction that drives this revision

The previous proposal modelled AL/X as asleep, woken periodically by a timer. That was
wrong, and wrong in a way that would have shaped every primitive built on top of it.

**While the runtime is running, AL/X is continuously present.** Core inference happens in
discrete turns because models are invoked discretely — but a turn is a *moment of active
cognition inside a continuous existence*, not a waking from sleep. The distinction is not
philosophical decoration. A sleep/wake model makes the scheduler the protagonist: it
decides when she exists, and everything else becomes a reaction to its clock. A
continuous-presence model makes *information* the protagonist: something new exists in
her world, and that is an occasion for thought. She is no more absent between turns than
a person is absent between sentences.

This document therefore defines one permanent primitive — the **cognition opportunity** —
and builds everything else on it. It is intended as the shape we keep, not a V1 to
replace later.

---

## 1. What a cognition opportunity is

> A **cognition opportunity** is an occasion on which the authoritative Core is invoked.

That is the whole definition. An opportunity asserts that something in AL/X's world is
new, and it invokes her. It asserts nothing about importance, subject, urgency, or what
should be done.

**The Core call happens.** An opportunity does not offer the Core a turn it may decline,
because declining is itself a judgement and there is no one but the Core to make it.
Deciding that something is not worth thinking about *is* thinking about it. So the
invocation is unconditional, and everything after it is hers:

- pursue it, or not;
- record something, or not;
- act through a capability, or not;
- say something to Friedl, or not;
- or finish silently.

**There is no pre-Core intelligence.** Nothing between the world and the Core decides
whether an occasion deserves cognition. A filter there would be a second mind making the
first decision — the cheapest one to build and the most damaging, because it would decide
what she never gets to consider.

An opportunity carries exactly five fields:

| Field | Meaning |
| --- | --- |
| `opportunity_id` | identity, for audit and idempotency |
| `origin` | which source produced it — see §3 |
| `arose_at` | when it became available |
| `references` | durable references to what is new (a turn id, an event id, a goal id, a request id) — **references, never content** |
| `provenance` | existing `ContentProvenance`, for retention |

There is no `topic`, no `priority`, no `reason`, no `suggested_action`, no `summary`. A
field naming what the opportunity is *about* would be the runtime forming an opinion, and
every deterministic rule we have rejected would eventually hide in it.

**The critical property — an opportunity carries no instruction.** It says "there is
something new" and invokes her; what that is worth, and what follows from it, is hers
alone. Silence is a complete and ordinary outcome, and is expected to be the common one.

### Why this is the right permanent primitive

It unifies things the current runtime already treats separately: a person speaking, mail
arriving, a capability finishing, a research answer returning, and AL/X's own intention to
revisit something later are all *the same kind of fact* — new information exists. Today
the first two have bespoke paths and the last has no path at all. One primitive covering
all of them is what makes this a foundation rather than a feature.

---

## 2. How AL/X creates one for herself

This is the mechanism that makes continuity real, and it is the heart of the design.

AL/X may call one capability, `request_future_cognition`, whose structured input is:

| Field | Meaning |
| --- | --- |
| `request_id` | identity she assigns |
| `not_before` | the moment from which she wants the opportunity |
| `note` | **her own words to her future self**, opaque to every deterministic component |
| `references` | durable references to what it concerns, and provenance for retention |

She may also withdraw one she no longer wants (`withdraw_future_cognition`). Requests are
durable, survive restarts, and are inspectable and deletable by Friedl.

**`note` is the crux.** Deterministic code persists it, hands it back verbatim when the
opportunity arises, and never reads it. It is not parsed, matched, scored, indexed by
keyword, or used for any decision. It is a message from AL/X to AL/X. This is what lets
her pick a thought back up rather than merely being reminded that a timer fired — and it
is why the runtime can honour "I want to come back to this" without ever knowing what
"this" is.

**There is deliberately no condition language.** An earlier draft proposed an enumerated
set — `goal_unblocked`, `capability_result_available`, and so on. That is dropped from the
foundation, because the objective runtime events in §3 *already* create opportunities for
every one of those situations: a capability completing, mail arriving, work finishing and
a person speaking each invoke her on their own. A condition language would have been a
second way to express occasions the event origins already produce, which is precisely what
Law 0 forbids.

`not_before` is therefore the whole vocabulary of a self-request. If we later find a
genuinely mechanically-decidable condition that no existing event can represent, we add it
deliberately, one value at a time, with a named reason. We do not build the grammar in
advance and wait for uses to appear.

**What deterministic code knows:** when. **What it never knows:** why. It stores a time
and hands back her note unread. That division is the whole of Law 2 and Law 3 applied to
initiative — and it is the mechanism by which *she*, not the runtime, sets the rhythm of
her own extra cognition.

---

## 3. How runtime and external events create opportunities

Every other source produces opportunities the same way, and none of them interprets
anything. A source's entire job is to notice that something exists and say so.

| Origin | Produced when | Already exists? |
| --- | --- | --- |
| `PERSON_TURN` | Friedl types or speaks | yes — the gateway path |
| `EXTERNAL_EVENT` | mail arrives; any future observer reports a fact | yes — `BackgroundEvent` |
| `WORK_COMPLETED` | a capability result becomes available; a goal's state changes | partly |
| `SELF_REQUESTED` | a future-cognition request comes due (§2) | **missing** |

An event source may filter for *validity* — deduplicate, drop malformed input, respect
its own retention — because those have single objectively correct answers. It may not
filter for *interest*. "Only surface mail from known senders" or "only if the goal is
stale" are judgements wearing a filter's clothing, and they belong to the Core.

**One structural change is required here.** `BackgroundEventSource` currently lives in
`src/alx/contracts/mail.py:269`. That is a mail-specific home for what must become the
general protocol every origin implements. It moves to a neutral contracts module. This is
a genuine Law 0 concern rather than tidying: leaving it in `mail.py` would invite a second,
parallel source protocol for non-mail origins, and then there would be two ways for the
world to reach the Core.

---

## 4. Continuity context the Core receives

Every cognition turn — person-initiated or not — is the same kind of turn and gets the
same *shape* of context. There is no separate "idle context" assembly, because a second
context builder would be the second intelligence arriving through the back door.

What is added to the existing `ReasoningContext`:

| Element | Source | Bound |
| --- | --- | --- |
| `current_opportunity` | the opportunity | origin, `arose_at`, references, and **her own `note` verbatim** if self-requested |
| `unfinished_goals` | `list_unfinished` | already compact `GoalSummary` — unchanged |
| open notebook threads | `SQLiteResearchStore` | thread id, title, her stated interest, status, last-touched — **titles only, never entry bodies** |
| open carried thoughts | new store, §7 | her own words, unread by anything else |
| pending future-cognition requests | new store, §7 | so she can see what she already asked for and not ask twice |
| recent conversation | `project_turns_for_reasoning` | the existing window; a turn after long quiet is not given less of her own history |
| capabilities | existing registry | unchanged |
| memories | *nothing preloaded* | she retrieves via the existing `retrieve_memories` decision |

Deliberately absent: full goal state, notebook entry bodies, evidence, preloaded memory.
Each is reachable by a decision she makes (`select_goal`, `read_research_thread`,
`retrieve_memories`). Preloading them would cost money on turns that often end in silence
and — the real objection — would quietly steer her, because whatever the runtime puts in
front of her is what she is most likely to think about. Choosing that is choosing the
topic.

Ordering across every list is recency alone. Recency is the only ordering that encodes no
judgement about subject matter.

---

## 5. How she speaks or stays silent

**Revised from the previous proposal, which was wrong here.**

AL/X may speak from any cognition turn, including one no person initiated. If she judges
something worth saying, she says it, and it reaches Friedl through the ordinary response
path: `CoreOutcome.RESPONDED` → conversation turn appended → speech synthesis. Nothing at
the transport layer suppresses, defers, queues, or re-words autonomous speech.

Equally, `finish_silently` remains a first-class outcome and is expected to be the common
one. The `CoreState.FINISHED_SILENTLY` path already exists and already produces no turn
and no audio.

**What decides:** the Core, and only the Core. There is no importance threshold, no
notification policy, no quiet-hours rule, no "only interrupt for X" filter, no forced
report, and no minimum or maximum on how often she may speak. If she is wrong about what
was worth saying, that is a matter for conversation and for what she learns, exactly as it
would be with a person — not something to be fixed with a rule.

**Two mechanical constraints remain, and neither is a judgement:**

1. **Delivery requires a live transport.** Speech needs an open voice session. If none is
   connected, the response cannot be delivered — a physical fact, not a policy. It is
   recorded durably and offered back to her on her next turn, and she decides then whether
   it still matters. She is told this is what happened; she is never left believing she
   spoke to no one.
2. **She never speaks over Friedl.** `_core_turn_lock` already serialises Core turns, so a
   turn cannot begin mid-exchange. This is turn-taking, which is what conversation is.

The existing "who may author AL/X's words" clarification in `docs/LAW_ENFORCEMENT.md` is
satisfied without a special case: every word is composed by the authoritative Core in a
real reasoning turn. Nothing else writes prose, and nothing rewords hers.

---

## 6. Runaway protection, and why there is no frequency rule

### There is no frequency rule

AL/X sets the rhythm of her own cognition. Deterministic code does not decide how often
she may think, and there is no rule of any of these shapes:

- N autonomous turns per hour or per day;
- a fixed periodic invocation of the Core;
- a curiosity rate limit;
- a requirement that a person or an external event intervene before she may think again.

The reasoning is straightforward. In an ordinary twelve-hour working day she already
receives many opportunities from ordinary interaction, arriving mail, completed work and
returning research results. Additional cognition beyond that comes almost entirely from
her own future-cognition requests — and **we do not yet know how often she will make
them.** Maybe three in a day, maybe twenty, maybe none. A number chosen now would be a
guess encoded as a boundary, and once encoded it would be indistinguishable from a
behavioural rule about how curious she is allowed to be.

The ledger (§7c) is how we find out. Behavioural limits, if any are ever warranted, come
after evidence — not before it.

### What is actually bounded

Loops are bounded **mechanically**, never behaviourally. Four mechanisms, none of which
counts her thoughts:

1. **A self-requested opportunity cannot be immediate.** `not_before` must be at least a
   configured minimum (proposed: 60 seconds) ahead. This is what breaks a tight loop: a
   turn cannot spawn a turn that spawns a turn without wall-clock time passing, so the
   worst conceivable runaway is paced by real time rather than by CPU.
2. **Reasoning calls per opportunity are bounded** by the existing `ExecutionBudget`, with
   the opportunity id as task id. Exceeding it checkpoints, exactly as a runaway
   conversational task does today.
3. **A hard daily spend ceiling for autonomous cognition** — §8. This is the emergency
   fuse, and it is the control that actually makes a frequency rule unnecessary.
4. **One Core turn at a time**, the existing `_core_turn_lock`; plus the master kill
   switch and the durable audit trail.

### Why the chain-depth limit is removed

An earlier draft proposed refusing a self-request beyond a chain depth of five consecutive
self-requested turns. It is deleted, because it cannot be justified: no failure exists that
the three controls above do not already contain.

Take the pathological case — AL/X requests a further cognition on every turn, forever. The
minimum horizon paces that to at most one turn per 60 seconds. Each turn is capped at a few
reasoning calls. And the daily autonomous ceiling stops the whole thing outright once the
day's money is gone, regardless of how the calls were distributed. The failure is contained
by cost and time, which are the real resources, and it is contained *whether the loop is
five turns deep or five hundred*.

Depth, meanwhile, is not a proxy for anything real. A run of sustained thinking about one
problem is exactly what continuity is *for*, and a depth counter cannot distinguish that
from a loop — it would stop the good case and the bad case at the same arbitrary number.
Code that says "you have had five consecutive thoughts, now stop" is a behavioural rule
wearing an engineering costume, and it would be the first thing to make her feel
mechanical.

## 7. Durable primitives

### Exists and is reused unchanged

| Primitive | Where | Role |
| --- | --- | --- |
| Goals | `SQLiteGoalStore` | unfinished work, restart-safe |
| Notebook | `SQLiteResearchStore` | unfinished *thinking* — threads, entries, revisions, her stated interest |
| Memory | `SQLiteMemoryStore` | factual / relationship / autobiographical, with provenance and supersession — the evolving self-model |
| Conversation | `SQLiteConversationStore` | the durable thread |
| Research ledger | `SQLiteResearchLedger` | reserve-and-reconcile spend ceiling |
| Reasoning ledger | `ReasoningUsageLedger` | per-task call ceiling |
| Silence | `CoreState.FINISHED_SILENTLY` | already produces no turn, no audio |
| Non-person ingress | `BackgroundEvent` + gateway | already proven by mail |

The substrate is largely built. What is missing is not memory — it is *occasion*.

### Genuinely missing — three stores

**(a) Future cognition requests.** Her own requests: `request_id`, `not_before`, `note`
(opaque), `references`, `status` (`pending` / `honoured` / `withdrawn`), and created-at.
No condition field, and no chain depth — §2 and §6 explain why neither exists.

**(b) Carried thoughts.** Something she concluded and may want to raise or revisit, which
is neither work with success criteria (a goal), nor a claim about the world (a notebook
entry), nor a judgement that something mattered to her development (a memory). Today it
has nowhere to live and simply evaporates. Fields: `thought_id`, her own words, optional
references to what it arose from, formed-at, and status (`open` / `raised` / `withdrawn`).

Explicitly **no** urgency, priority, expiry, category, or delivery trigger. Nothing in the
runtime reads the content. She may withdraw one because she no longer thinks it — which is
the point.

**(c) Opportunity ledger.** One durable row per opportunity: id, origin, arose-at,
references, outcome state, reasoning calls consumed, and measured cost. This enforces the
autonomous spend ceiling (§8), and it is the evidence that tells us what she actually does
before anyone proposes a behavioural limit. It is the answer to "how often does she
actually want to think?" — a question we currently cannot answer and should not guess.

All three are storage with no judgement in them, reached — like everything else — through
the broker and gate, with notebook-style permissions and no external authority.

### Why carried thoughts and future-cognition requests are different primitives

A carried thought is *content without an occasion*: something she thinks, with no
particular moment attached. A future-cognition request is *an occasion without content*:
a moment she wants, carrying only a private note. They compose — a thought may prompt a
request, and a request's turn may produce a thought — but collapsing them would force
every unfinished thought to carry a schedule, which is the timer model creeping back in.
Keeping them apart is also what lets her hold something indefinitely without committing to
when she will return to it, which is how unfinished thoughts actually behave.

---

## 8. Hard cost and safety boundaries

Rails on mechanics, spend and safety only. None of these decides what she thinks about or
how often.

| Boundary | Proposed first value | Note |
| --- | --- | --- |
| **Autonomous cognition daily budget** | **`AUTONOMOUS_COGNITION_DAILY_BUDGET_USD = 0.5405`** (R10 at 18.5 ZAR/USD) | the emergency fuse — see below |
| Reasoning calls per opportunity | `ExecutionBudget(expected=2, warn_above=3, stop_above=4)` | existing mechanism, opportunity id as task id |
| Minimum `not_before` horizon | 60 seconds | breaks tight self-loops (§6) |
| Research spend | **the existing D-023 daily ceiling, unchanged and separate** | see below |
| Concurrency | one Core turn at a time | existing `_core_turn_lock` |
| Master switch | autonomous origins **off by default** | same principle as `ALX_RESEARCH_ENABLED_TIERS` |
| Kill switch | disable at runtime; pending requests retained, not honoured | reversible and auditable |
| External authority | **entirely unchanged** | see below |

Deliberately absent: any cap on the number of autonomous turns, any rate limit, any chain
depth. §6 explains why.

### The autonomous cognition budget

This is the main emergency fuse and the reason no frequency rule is needed. It is a hard
daily USD ceiling on **Core reasoning initiated by autonomous cognition opportunities** —
that is, opportunities of origin `SELF_REQUESTED`, and any other origin that is not a
person turn. It is entirely separate from ordinary user-initiated Core usage, which it
neither constrains nor draws from.

It must:

- **use the existing normalised Core usage telemetry** — `normalise_usage` in
  `src/alx/contracts/usage.py` already gives one canonical shape across providers, and
  `telemetry()` in `bootstrap/live_voice.py:134` already routes every Core measurement to
  the durable usage ledger. This budget reads that same measurement; it does not introduce
  a second accounting of what a call cost;
- **account for actual provider and model cost**, via the existing `cost_usd` price table
  rather than an estimate;
- **fail closed.** `cost_usd` already returns `None` both for an unpriced model and for a
  usage report carrying no token counts, and `is_measured` already reports whether a
  report is complete and self-consistent. An autonomous turn whose cost cannot be measured
  is charged the configured worst case, and if no price exists for the model, autonomous
  cognition stops. A call we cannot price is not a free call;
- **survive restart**, so a crash cannot hand her a fresh day's budget — the same property
  `SQLiteResearchLedger` already provides for research;
- **refuse further autonomous cognition once exhausted**, until the day rolls over;
- **never raise itself**, under any condition, for any reason.

When it is exhausted, autonomous opportunities stop invoking the Core. Person turns are
unaffected: Friedl can always talk to her. She is told, on her next turn, that autonomous
cognition was suspended, because that is evidence about her own situation rather than
something to hide from her.

**The budget is denominated in Rand and stored in USD.** Friedl reasons about this spend
in Rand; the provider bills in USD and every rate in `USD_PER_MILLION` is USD, so the
ledger works in USD and no currency conversion happens anywhere in the spending path. The
configured value is `AUTONOMOUS_COGNITION_DAILY_BUDGET_USD`, and the Rand figure it came
from is recorded here rather than computed at runtime.

**Conversion assumption for this experiment: R10.00 at 18.5 ZAR/USD = $0.5405.** This is a
recorded assumption, not a live rate. Nothing fetches an exchange rate, and no code
converts currency: a fuse whose size moved with the foreign-exchange market would be a
different ceiling every day and impossible to reason about. If the rate drifts far enough
to matter, Friedl changes one configured number deliberately, exactly as he would change
the Rand figure itself.

**R10/day is not a prediction.** It is not a target, a spending plan, or an expectation
that she will use it. It is the ceiling at which something has clearly gone wrong. If the
ledger later shows she habitually uses a fraction of it, the correct response is to leave
it alone; a fuse is not sized to typical load.

**If she regularly reaches it, that is evidence, not a malfunction.** The response is a
deliberate decision to raise it — to R20 or beyond — informed by what the ledger shows she
was actually doing. It is never raised automatically, never raised by her, and never
raised as a workaround for a refused turn.

### Reservation and reconciliation rule

Cost is not knowable before a call: output tokens are what make a turn expensive and they
exist only once the model has answered. So autonomous spend is **reserved, not predicted**,
in the same reserve-then-settle shape `SQLiteResearchLedger` already uses. At every instant
between reservation and settlement the full worst case is already withdrawn, so the day
cannot be overspent even if the process dies mid-call.

**Before every autonomous Core call, in this order, under one lock and one transaction:**

1. **Resolve the exact identity.** `(provider, model)` as configured — never a family or a
   name-prefix match.
2. **Price it, or stop.** `price_of(provider, model)` must return a rate. An unpriced or
   unknown model **fails closed before dispatch**: autonomous cognition refuses and the
   call is never made. Guessing a neighbouring model's rate would defeat the ceiling it
   exists to enforce.
3. **Require hard provider-side bounds.** The request must carry a finite
   `max_output_tokens`, and its input must be measurable by `input_token_upper_bound`.
   Without both there is no finite worst case, so no honest reservation exists and the
   call is refused. `ModelRequest.max_output_tokens` already documents exactly this: *a
   bound is "wrong for anything spending against a dollar ceiling: without a bound there
   is no worst-case price, so no reservation can be honest."*
4. **Compute the worst case**, pricing every token at its most expensive rate — all input
   uncached, output billed in full:

   ```
   worst_case_usd = (input_upper_bound  / 1e6) * uncached_input_rate
                  + (max_output_tokens  / 1e6) * output_rate
   ```

   No cache discount is assumed: a cache miss is the expensive case, and a ceiling must
   hold in the expensive case. Reasoning tokens bill as output and are covered by the
   output bound rather than added again.
5. **Withdraw or refuse.** If `remaining_usd < worst_case_usd`, autonomous cognition is
   refused for the rest of the day. Otherwise the full worst case is withdrawn and a
   reservation id is recorded durably before dispatch.

**After the call:**

6. **Measured usage reconciles.** If `is_measured(usage)` holds and `cost_usd` returns a
   figure, the reservation is replaced by that measured cost and the difference returned to
   the day. This is the normal path.
7. **Missing or malformed usage settles conservatively** — the reservation is **kept at the
   full worst case** and nothing is returned. A provider that reported nothing has not told
   us the call was free, and pricing silence at zero would let unlimited unmeasured calls
   run inside one day's budget. `cost_usd` already returns `None` for exactly this case, so
   the conservative branch is the one the existing primitive hands us.
8. **A crashed call stays withdrawn.** An unreconciled reservation is not swept back into
   the pool on restart; it is settled conservatively at its worst case.

**Restart and single-path guarantees.** The ledger is SQLite on disk, so a restart cannot
hand her a fresh day's budget — the same durability `SQLiteResearchLedger` already provides.
The check and the withdrawal happen under one lock and one transaction, so two concurrent
autonomous turns cannot both read the same remaining balance and both proceed. And there is
exactly one production path to an autonomous Core call: the opportunity source reaches the
Core through `ConversationGateway`, and the reservation is taken in that one place, the way
`budget_check` already guards reasoning today. **No other Core path can spend this budget,
and this budget cannot be spent by any other Core path** — a person-initiated turn is
charged to neither, and research is charged only to the separate D-023 ledger.

### Worked example — the currently configured Core model

The configured Core is `ALX_REASONING_PROVIDER=xai`, `ALX_REASONING_MODEL=grok-4.5`.

Walking the rule:

- **Step 1** resolves `("xai", "grok-4.5")`.
- **Step 2** calls `price_of("xai", "grok-4.5")` → `None`. The price table holds only
  `("openai", "gpt-5.4-nano")`, `("openai", "gpt-5.4-mini")` and `("openai", "gpt-5.4")`,
  and no approved alias covers the xAI model.

**Result: autonomous cognition refuses before dispatch. Nothing is reserved, nothing is
called, and $0.00 of the $1.00 is consumed.**

That is the rule behaving correctly, and it is worth stating plainly rather than hiding in
a footnote: **on today's configuration, D-024's autonomous cognition would not run at all.**
Two things are required before it can, and both are deliberate acts rather than code
changes:

1. **A recorded price for the Core model.** Friedl adds `("xai", "grok-4.5")` to
   `USD_PER_MILLION` from the vendor's published rate card, exactly as the three OpenAI
   entries were recorded on 2026-09-01.
2. **A hard `max_output_tokens` for autonomous Core calls.** The Core currently sets none
   (`model_reasoner.py:812` builds its `ModelRequest` without one), which is right for
   ordinary conversation and insufficient for spending against a dollar ceiling. Autonomous
   turns must pass a finite bound.

Once both exist, the arithmetic is ordinary. Using `("openai", "gpt-5.4")` at
`(2.50, 0.25, 15.00)` USD per million as a **worked illustration of the formula only** —
not a proposal to change the Core model — with a 40,000-token input upper bound and a
4,000-token output ceiling:

```
input  : 40,000 / 1e6 × $2.50  = $0.1000
output :  4,000 / 1e6 × $15.00 = $0.0600
worst case                      = $0.1600
```

$0.16 is withdrawn before dispatch. The day's $1.00 therefore admits **six** such turns
before the seventh is refused for insufficient remaining balance. If that call actually
consumes 12,000 input tokens (8,000 of them cached) and 900 output tokens, `cost_usd`
returns $0.0255 — cached input charged at its own lower rate rather than counted twice —
the reservation is replaced by that figure, and $0.1345 returns to the day. If the provider
reports no usage at all, the full $0.16 stands.

Note what the reservation does *not* do: it never selects a cheaper model, a shorter
prompt, or a smaller bound to fit the remaining budget. A ceiling that quietly buys
something lesser is not a ceiling. It refuses, and the refusal becomes evidence AL/X
reasons about on a later turn.

### Recorded experiment — Luna Max for autonomous turns

**Status: a time-boxed evaluation, not an architecture decision.** D-024 does *not*
authorise origin-based model selection as permanent architecture, and this section exists
so the arrangement cannot become permanent by default.

| | User-initiated turn | Autonomous cognition turn |
| --- | --- | --- |
| Model | `gpt-5.6-sol` | `gpt-5.6-luna` |
| Effort | `medium` | `max` |

Both use the same Core contracts, identity, memory, goals, notebook, broker, safety gate
and capabilities. Luna is not a classifier, a sidecar, a filter or a pre-Core stage: during
an autonomous turn it *is* the authoritative Core for that turn, with everything that
implies. Research tiers are unchanged.

**What the experiment is for.** To collect evidence on the quality of her autonomous
judgement, continuity and personality drift between the two models, which interests she
chooses, how often she requests further cognition, how often she speaks versus stays
silent, and the real token and cost profile.

**Why it is recorded rather than configured.** Selecting a model by cognition origin is
structurally the "second intelligence by accretion" risk named in the Risks section. The
direction is inverted — a stronger model rather than a cheaper one — but the shape is the
same, and the specific hazard is real: if the entity deciding whether to interrupt Friedl
is systematically a different one from the entity he converses with, her memories and
self-model are being written by two minds and read by both. That is a continuity problem
in a workstream whose whole purpose is continuity.

We accept that risk deliberately and temporarily, in exchange for evidence. **The drift
this guards against is silent permanence:** an experiment that is never concluded becomes
the architecture by default, and nobody ever decides it.

**The experiment concludes with an explicit decision** to keep autonomous cognition on
Luna, move it to Terra, move it to Sol, or use one Core model universally. Until that
decision is recorded, the dual-model arrangement has no standing as architecture, and
`origin` remains what §1 says it is: a fact about where an occasion came from, never a
routing key for anything else.

### Prerequisite — the autonomous turn must carry its own output bound

This is a blocking implementation item, not a detail, and nothing autonomous can run until
it is done.

**The problem.** `CoreAgent`'s reasoning request is built in one place
(`model_reasoner.py:812`) and passes no `max_output_tokens`, so the field is `None` and the
provider applies its own default. That is correct for user-initiated conversation — an
answer to Friedl should not be truncated mid-sentence by an arbitrary ceiling — and it is
insufficient for anything spending against a dollar ceiling. `ModelRequest` already states
the rule: without a bound there is no worst-case price, so no reservation can be honest.

**What is required.** An autonomous turn's `ModelRequest` must carry
`max_output_tokens = 32_000`. It is enforced provider-side (`openai.py:94`), which is the
only enforcement that works: a bound applied after the fact cannot stop the spend it
measures.

**What must not happen.** The user-initiated path keeps no bound at all. This is the one
place where the two origins legitimately differ in a *mechanical* parameter, and the
difference must stay strictly mechanical:

- it changes what the provider will generate, never what AL/X may think about;
- it does not alter reasoning effort, model, provider, prompt, capabilities or context;
- it is not a quality setting, and must never become one.

The bound is deliberately generous — 32,000 output tokens is far more than any real
decision needs, even at `max` effort where reasoning tokens count toward it — precisely so
that it functions as a financial fuse rather than a limit on her cognition. If an
autonomous turn ever truncates against it, that is a bug to investigate, not a budget to
economise against: the correct response is to raise the bound and the ceiling together, not
to let her thinking be cut short.

**Ordering.** The reservation cannot be taken before the bound exists, because the worst
case is computed from it. So the bound lands first, and the ledger second.

### Research spend stays separate

The D-023 research ceiling is unchanged and independent. If an autonomous turn chooses to
research, **both** controls apply: the reasoning is charged to the autonomous cognition
budget, the research call is charged to the research budget, and either being exhausted
stops its own activity without touching the other. Neither ceiling can be raised by the
other's headroom.

### External authority does not move

A cognition turn of any origin grants no new permission. Sending mail, altering Xero,
deleting, purchasing and every other effectful action keep exactly the approval
requirements they have today, enforced by the same Safety Gate. An autonomous turn cannot
create an approval record, cannot satisfy one, and cannot act on a standing scope a
conversation has not already established.

### Auditability

Every opportunity, every self-request and its note, every outcome, every reasoning call and
every dollar is durably recorded and inspectable, correctable and deletable by Friedl.

## 9. The architectural path

One path, origin to outcome. Law 0: there is no second route to any of these outcomes.

```text
PERSON_TURN ─────┐
EXTERNAL_EVENT ──┤
WORK_COMPLETED ──┼──> CognitionOpportunitySource ──> ConversationGateway ──> CoreAgent
SELF_REQUESTED ──┘        (deterministic;              (existing sole          (sole
                           notices, never                ingress)              reasoning
                           interprets)                                         authority)
                                                                                  │
                    ┌─────────────────────────────────────────────────────────────┤
                    │                    the Core alone decides                   │
                    ▼                                                             ▼
            finish_silently                                          CapabilityBroker
         (no turn, no audio)                                                │
                    │                                              SafetyGate (unchanged)
                    │                                                       │
                    │                                        ┌──────────────┼──────────────┐
                    │                                        ▼              ▼              ▼
                    │                                   research      notebook /      effectful
                    │                                                  memory /       (approval
                    │                                                  thoughts /      unchanged)
                    │                                                  requests
                    │                                                       │
                    └───────────────────────> RESPONDED ────> conversation turn ──> speech
                                            (her judgement)
```

Concretely, against the merged code:

- **New:** `CognitionOpportunitySource` — deterministic, composes the origins, implements
  the general event-source protocol. It is architecturally forbidden from importing the
  goal, notebook, memory or carried-thought modules; enforceable as a dependency rule in
  `architecture/boundaries.toml` and `scripts/check_architecture.py`, which is where this
  guarantee belongs rather than in a reviewer's memory.
- **New:** three durable stores (§7) and their capabilities, plus the autonomous cognition
  spend ledger (§8), built on the same reserve-and-settle shape as `SQLiteResearchLedger`
  and reading the existing normalised Core usage measurement.
- **Moved:** `BackgroundEventSource` out of `contracts/mail.py` into neutral contracts.
- **Changed:** `ReasoningContext` gains `current_opportunity` and the continuity lists;
  `bootstrap/live_voice.py` composes the source and wires the autonomous spend check ahead
  of an autonomous Core call, the way `budget_check` already guards reasoning today.
- **Unchanged:** `CoreAgent.process`, the broker, the gate, the goal/memory/notebook
  stores, the speech path.

The Core already receives `current_trigger` distinguishing event- from conversation-
initiated turns. An opportunity's `origin` generalises that existing field; it is not a
second mechanism.

---

## 10. What to delete from the previous proposal

`CONTINUITY_TURNS.md` is deleted in the same change that adds this document. Retaining it
"for reference" would leave two competing designs, which is the disease Law 0 exists to
prevent. Git history is the archive.

These specific ideas are **wrong and must not survive into implementation**:

| Deleted | Why it was wrong |
| --- | --- |
| **The wake/sleep model itself** | She is continuously present. "Waking" made the scheduler the protagonist and framed her existence as intermittent. |
| **Fixed 90-minute interval** as the primary mechanism | A periodic curiosity timer. Opportunities arise from information, not from a clock. |
| **20-minute idle precondition** | Encoded "she thinks when ignored." Cognition follows new information, not absence of attention. |
| **6 wakes per 24h cap** | A curiosity quota. Replaced by a hard daily spend ceiling (§8) that limits money, not interest. |
| **Transport-layer suppression of autonomous speech** | Directly contradicts her judging what is worth saying. Deleted entirely. |
| **Discarding a response composed on an autonomous turn** | Threw away her actual judgement and treated her speech as a fault. |
| **"If Friedl never speaks, she never speaks"** | Stated as a virtue; it is the reactive-assistant behaviour this workstream exists to end. |
| **Carried thoughts as the only route to speech** | Made every autonomous utterance wait for a person-initiated turn — a deferral rule dressed as continuity. Carried thoughts remain (§7) as *memory of an unfinished thought*, not as a delivery queue. |
| **Wake ledger** (as named) | The concept survives as the opportunity ledger (§7c); the framing does not. |
| **Self-request chain-depth limit** | Proposed in the first revision of *this* document and removed here: no failure exists that the minimum horizon, the per-opportunity call bound and the daily autonomous ceiling do not already contain, and a depth counter cannot tell sustained thinking from a loop. |
| **Enumerated `condition` language** | Also proposed in the first revision and removed: the objective event origins already create opportunities for every condition it would have expressed, making it a second path to the same occasions. |

**Retained from the previous proposal, because it was right:** reuse of the single Core;
the compact-context discipline and its warning about steering-by-preloading; unchanged
external authority; shared rather than separate spend ceilings; silence as ordinary; the
prohibition on scores, meters, modes and scripted intimacy; the dependency-rule guarantee
that the opportunity source cannot read her state; and the warning that the reasoner's
system prompt must not enumerate suggested idle activities.

---

## Proposed D-024 wording

> ## D-024 — Autonomous cognition opportunities
>
> - **Date:** *(pending)* — **Decision owner:** Friedl — **Status:** *(pending approval)*
>
> **Decision.** While the AL/X runtime is running, AL/X is continuously present. Her
> cognition occurs in discrete Core turns, because a model is invoked discretely; those
> turns are moments of active thought within a continuous existence, not wakings from
> sleep. New information in her world — a person turn, an external event, completed work —
> or a future cognition she herself requested creates a *cognition opportunity*: an
> occasion on which the single authoritative Core is invoked. This amends D-023's exclusion
> of autonomous cognition to the extent stated here and no further.
>
> **The Core is invoked, not consulted.** Nothing decides ahead of the Core whether an
> occasion deserves cognition. Judging that something is not worth pursuing is itself a
> judgement, and only AL/X may make it.
>
> **What deterministic code does.** Timing, persistence, spend accounting, safety and
> execution. It records that something new exists; for a future cognition AL/X requested,
> it stores the time she named and honours it. It never reads the private note she attaches
> to her own request, and it never decides why she wants the occasion.
>
> **What AL/X decides.** Everything else: whether to pursue anything, what, which goal if
> any, whether to use a capability, whether to research, what to record, what she now
> believes, whether to remain silent, and whether to speak to Friedl. No deterministic
> importance threshold, topic rule, notification policy, forced report, or frequency rule
> may decide any of these.
>
> **Frequency is hers.** How often additional cognition occurs is determined by AL/X,
> through her own future-cognition requests. No fixed cadence, periodic invocation, daily
> quota, rate limit, or requirement of intervening interaction is imposed on her.
>
> **Speech.** AL/X may initiate conversation from any cognition turn when she judges
> something worth saying. Silence is equally ordinary and expected. Delivery requires a
> live transport; an undeliverable response is retained and offered back to her rather than
> queued for automatic delivery.
>
> **Authority is unchanged.** No cognition turn grants new permission. Every effectful
> action retains its existing approval requirements through the same Safety Gate.
>
> **Bounds.** A hard daily USD ceiling on autonomous Core cognition, measured from actual
> provider cost, failing closed when cost cannot be measured, surviving restart, refusing
> further autonomous cognition once exhausted, and never raising itself. A bounded number
> of reasoning calls per opportunity, a minimum horizon before a self-requested opportunity
> may arise, one Core turn at a time, and a master kill switch. The D-023 research ceiling
> remains separate and unchanged; where an autonomous turn chooses research, both apply.
>
> **Auditability.** Every opportunity, request, outcome, reasoning call and cost is durably
> recorded, inspectable, correctable and deletable by Friedl.

## Risks

**The opportunity source acquires an opinion.** The likeliest failure. It starts neutral;
someone later adds "only if a goal is stale" or "skip low-value mail." Each is a rule about
what deserves thought — Law 1 phrase routing with a different surface. *Mitigation:* the
opportunity carries references and no content, and the source is forbidden by dependency
rule from importing her state modules.

**A frequency rule reappears under pressure.** The likeliest regression. She has a busy
day, the ledger looks lively, and someone proposes "just cap it at ten a day" — which is a
curiosity quota, the exact thing this design refuses. *Mitigation:* the autonomous budget
is the fuse, and it is denominated in money rather than thoughts. If autonomous cognition
genuinely costs too much, lower the ceiling; do not count her thoughts.

**A condition language returns.** A future need looks like it wants `goal_unblocked`, and
an enum starts growing back. *Mitigation:* check first whether an existing event origin
already creates that occasion — it usually does — and add a value only with a named reason
and a demonstration that no event can represent it.

**The prompt scripts her.** A well-meant "on an autonomous turn, consider reviewing your
research threads" converts choice into schedule. *Mitigation:* the continuity addition to
the system prompt should be roughly two sentences — that this turn arose from an
opportunity rather than a person, and that silence is ordinary — listing **no** suggested
activities.

**Carried thoughts become a message queue.** Add priority, expiry, or an unraised-for-N-days
nudge and it is scheduled outreach. *Mitigation:* the store has no such fields and nothing
reads content.

**A second intelligence by accretion.** Someone proposes a cheap model for autonomous turns.
That is a second reasoning authority and breaches Laws 0 and 1. *Mitigation:* same Core
model always. If autonomy is too expensive, reduce spend, never her mind.

**She speaks at the wrong moment, or too much.** Real, and deliberately not solved
mechanically. The mitigations are turn-taking (§5.2), the master switch, and conversation —
she can be told. Adding a quiet-hours rule or an importance threshold would trade the
entire objective for a symptom.

**It underwhelms.** She may think and stay silent almost always, and Friedl may see little.
That is not failure, and the response must not be to raise a frequency or seed the prompt
with things to do. The opportunity ledger is the evidence that says whether any of it is
real, and it is the only thing that should move a number.
