# D-024 implementation plan

**Status:** PLAN ONLY. Nothing implemented. Autonomous cognition is not enabled and no
live provider call is made by any phase below.
**Design:** `CONTINUITY_FOUNDATION.md` — this plan adds no design decisions.
**Law 0:** every phase names its production outcome and its single authoritative path.

## Sequencing principle

Phases are ordered so that **money cannot be spent before it can be bounded, and nothing
autonomous can run before it can be refused.** Each phase is independently verifiable and
independently revertible. Phases 1–6 are all inert: the switch is off, and autonomous
cognition cannot occur however they are composed.

The output bound precedes the ledger, because the worst case is computed from it. The
ledger precedes the opportunity source, because a source with no fuse is a spending path
with no ceiling. Speech comes last, because it is the only phase a person can observe.

---

## Phase 0 — Governance

**Production files:** `governance/DECISIONS.md`.

**Content:** D-024 as worded in `CONTINUITY_FOUNDATION.md`, plus the recorded Luna
experiment: temporary, origin-selected, concluding in a deliberate decision (Luna / Terra /
Sol / one universal Core).

**Tests:** `scripts/check_governance.py` already verifies decision-record structure; it
runs unchanged.

**Independently verifiable:** the governance gate passes and D-024 is readable as approved
authority. No code exists yet.

---

## Phase 1 — Autonomous output bound

**Outcome:** an autonomous Core request carries a finite, provider-enforced output ceiling.

**Production files:**
- `src/alx/core/model_reasoner.py` — `ModelReasoner` gains an optional
  `max_output_tokens` given at construction and applied to the `ModelRequest` it builds
  (`model_reasoner.py:812`). It is not read from the context and not chosen per turn.
- `src/alx/bootstrap/reasoning.py` — `build_model_reasoner` accepts and forwards it.

**New primitives:** none. `ModelRequest.max_output_tokens` already exists and is already
enforced provider-side at `openai.py:94`.

**Tests:**
- an autonomous-configured reasoner sets `max_output_tokens` on its request;
- a user-configured reasoner leaves it `None`, so the Sol path is untouched;
- the bound changes no other field: same prompt, capabilities, context, effort, model;
- mutation: removing the bound fails the first test.

**Gates:** none changed.

**Independently verifiable:** request construction is inspected directly, with no provider
and no network. Nothing autonomous exists yet, so this phase is inert on its own.

---

## Phase 2 — Autonomous cognition ledger

**Outcome:** a hard daily USD ceiling on autonomous Core reasoning, reserved before
dispatch and reconciled after.

**Production files:**
- `src/alx/observability/autonomous_budget.py` *(new)* — `SQLiteAutonomousLedger`, built
  on the same reserve-then-settle shape as `SQLiteResearchLedger`, which it deliberately
  mirrors rather than shares: research and cognition are different ceilings and one table
  serving both would let either raise the other.
- `src/alx/config/settings.py` — `AUTONOMOUS_COGNITION_DAILY_BUDGET_USD` via the existing
  `_number_in_range`, defaulting to `0.0` so an unconfigured runtime cannot spend.
- `src/alx/observability/__init__.py` — export.

**New primitives:** one durable store. No new capability: AL/X never calls this; it is a
rail, not an action.

**Reservation rule** (as specified in the design, restated as the acceptance criteria):
resolve exact identity → refuse if unpriced → require finite input and output bounds →
worst case charges input at uncached **plus cache-write** rate and output at the output
rate → withdraw or refuse, under one lock and one transaction.

**Tests:**
- worst case for Luna at 32k/32k is exactly `$0.0528`;
- `$0.5405` admits 10 worst-case reservations and refuses the 11th;
- an unpriced model refuses **before** dispatch;
- a request without a finite output bound refuses;
- measured usage reconciles and returns the difference;
- **missing or malformed usage retains the full reservation** — never settles at zero;
- an unreconciled reservation stays withdrawn across a reopen (restart safety);
- two concurrent reservations cannot both pass the same remaining balance;
- the ledger never raises its own ceiling;
- the research ledger is untouched by autonomous spend, and vice versa;
- mutation: settling missing usage at zero fails the conservative-settlement test.

**Gates:** `architecture/boundaries.toml` unchanged — `observability` stays a leaf.

**Independently verifiable:** the ledger is exercised directly against fakes. It is wired
to nothing, so it cannot yet affect any turn.

---

## Phase 3 — Origin, and the Core the origin selects

**Outcome:** a cognition turn declares where it came from, and an autonomous turn is
answered by the autonomous reasoner.

This is the phase most at risk of becoming a router, so its boundaries are stated as
prohibitions rather than intentions.

**Production files:**
- `src/alx/contracts/cognition.py` — `CognitionOrigin` enum: `PERSON_TURN`,
  `EXTERNAL_EVENT`, `WORK_COMPLETED`, `SELF_REQUESTED`.
- `src/alx/contracts/core.py` — `ReasoningContext` gains `origin`.
- `src/alx/core/loop.py` — `CoreAgent.process` accepts the origin and passes it through.
- `src/alx/bootstrap/live_voice.py` — composition builds **two** `ModelReasoner`s (Sol
  unbounded / Luna bounded) and selects between them on origin alone.

**The selection is one expression over a closed enum, in composition, and nothing else.**
It reads no goal, no notebook, no memory, no conversation, no capability, no content, and
no field of the opportunity except `origin`. There is no table, no registry, no strategy
object and no per-turn model choice — those are the shapes a router takes, and a second
selector anywhere would be a competing production path under Law 0.

**Both reasoners are constructed from the identical laws, identity, capability catalogue,
contracts, goals, memory, notebook, broker and gate.** The only differences are model,
effort and output bound. `CoreAgent`, the broker and the gate never learn which model
answered.

**Tests:**
- a person-origin turn is answered by the Sol reasoner; an autonomous origin by Luna;
- both receive byte-identical laws, identity and capability catalogue;
- both reach the same broker, gate and stores;
- origin selects **only** the reasoner — no capability, permission, prompt or context
  differs by origin;
- `CoreAgent` exposes no way to choose a model, and the decision contract carries none;
- mutation: a second selection site, or selection on any field other than origin, fails.

**Gates:** a new `scripts/check_architecture.py` rule — **no module outside composition
may import both reasoners, and no selection may key on anything but `CognitionOrigin`.**
This is the guarantee that stops the experiment growing into a router, and it belongs in a
gate rather than in review memory.

**Independently verifiable:** with the opportunity source absent, no autonomous origin can
occur in production, so this phase is inert while being fully testable through fakes.

---

## Phase 4 — Future cognition requests

**Outcome:** AL/X can ask for a later cognition opportunity, in her own words.

**Production files:**
- `src/alx/contracts/continuity.py` *(new)* — `FutureCognitionRequest`.
- `src/alx/continuity/store.py` *(new)* — `SQLiteContinuityStore`; new `continuity`
  boundary in `architecture/boundaries.toml`, allowed to import `contracts` only.
- `src/alx/tools/continuity.py` *(new)* — `request_future_cognition`,
  `withdraw_future_cognition`.
- `src/alx/bootstrap/continuity.py` *(new)*, wired in `bootstrap/live_voice.py`.

**Shape:** `request_id`, `not_before`, `note` (opaque), `references`, `status`, created-at.
No condition field. No chain depth. No priority, urgency or expiry.

**Tests:**
- a request round-trips and survives restart;
- `not_before` below the minimum horizon is refused, and the refusal reaches AL/X as
  evidence rather than silence;
- **`note` is never read**: no store, capability, source or gate branches on its content —
  proven by a request whose note is adversarial text that must change no behaviour;
- withdrawal works and is inspectable;
- capabilities reach the broker and gate like any other and grant no external authority;
- mutation: any deterministic read of `note` fails.

**Gates:** the new boundary is added to `boundaries.toml`; the dependency test proves
`continuity` imports only `contracts`.

**Independently verifiable:** requests can be created and stored while nothing consumes
them. Still inert.

---

## Phase 5 — The opportunity source

**Outcome:** one deterministic source turns new information into cognition opportunities.

**Production files:**
- `src/alx/contracts/continuity.py` — `CognitionOpportunity`; the five fields, no more.
- `src/alx/contracts/mail.py` → neutral contracts — **`BackgroundEventSource` moves.**
  This is a Law 0 deletion, not a copy: the mail-specific home is removed so a second
  source protocol for non-mail origins cannot appear.
- `src/alx/continuity/source.py` *(new)* — `CognitionOpportunitySource`.
- `src/alx/continuity/ledger.py` *(new)* — the opportunity ledger.
- `src/alx/bootstrap/live_voice.py` — composes the source; applies the autonomous
  reservation before an autonomous Core call, injected the way `budget_check` already is,
  because `core` may import only `contracts`.

**Tests:**
- a due request produces exactly one opportunity, and never twice;
- an opportunity carries references and **no content**;
- the source reads no goal, notebook or memory — proven structurally, not by inspection;
- an exhausted budget refuses autonomous opportunities while person turns still work;
- the minimum horizon prevents immediate recursion;
- one Core turn at a time is preserved;
- the master switch disables autonomous origins entirely;
- every opportunity is recorded with outcome, calls and measured cost;
- mutation: a filter on interest, recency, staleness or sender fails.

**Gates:** dependency rule — `continuity.source` may not import `goals`, `memories`,
`research` or the notebook. This is the structural guarantee that the source cannot form
an opinion, and it is checkable rather than aspirational.

**Independently verifiable:** end-to-end with fakes, switch off. **This is the last phase
before behaviour becomes observable.**

---

## Phase 6 — Carried thoughts

**Outcome:** AL/X can hold a thought that is neither a goal, a notebook entry, nor a
memory.

**Production files:** `contracts/continuity.py`, `continuity/store.py`,
`tools/continuity.py`, `bootstrap/continuity.py` — all extended, none added.

**Shape:** `thought_id`, her words, optional references, formed-at, status. No urgency,
priority, expiry, category or delivery trigger.

**Tests:** round-trip and restart; open thoughts reach context; withdrawal; nothing reads
the content; mutation — adding a priority or expiry field fails a test that asserts the
stored shape.

**Independently verifiable:** available in ordinary conversation, independent of any
autonomous turn.

---

## Phase 7 — Speech from an autonomous turn

**Outcome:** AL/X may speak when she judges something worth saying, or stay silent.

**Production files:** `src/alx/interfaces/live_voice.py` — an autonomous outcome carrying a
response reaches the existing speech path; an undeliverable response is retained and
offered back to her.

**Almost nothing changes here, deliberately.** `RESPONDED` already appends a turn and
synthesises; `FINISHED_SILENTLY` already produces neither. The previous design's
transport-layer suppression is *not* implemented — it was deleted from the design, and
implementing it would contradict D-024.

**Tests:**
- an autonomous response is spoken and appended like any other;
- an autonomous `finish_silently` produces no turn and no audio;
- **no threshold, policy, quiet-hours rule or forced report exists anywhere** — proven by
  the absence of any branch on content, length, topic or time of day;
- with no live transport, the response is retained and offered back, not queued;
- a turn never begins mid-exchange;
- mutation: any importance filter on autonomous speech fails.

**Gates:** the existing "who may author AL/X's words" checks apply unchanged — every word
is Core-authored, so no special case is needed.

**Independently verifiable:** transport behaviour with a fake synthesiser. Still no live
provider call.

---

## Phase 8 — Enable and observe

**Not an implementation phase.** Friedl sets the master switch on, with the R10/day fuse
live. The deliverable is evidence from the opportunity ledger: how often she requested
cognition, what she chose, speech versus silence, continuity and drift between Sol and
Luna, and real cost.

**This phase concludes the Luna experiment** with the deliberate decision D-024 requires:
Luna remains, move to Terra, move to Sol, or one universal Core. Until that decision is
recorded, the dual-model arrangement has no standing as architecture.

---

## Summary

| Phase | Outcome | Inert? |
| --- | --- | --- |
| 0 | D-024 recorded | yes |
| 1 | autonomous output bound | yes |
| 2 | autonomous spend ledger | yes |
| 3 | origin selects the reasoner | yes |
| 4 | future cognition requests | yes |
| 5 | opportunity source | yes (switch off) |
| 6 | carried thoughts | yes |
| 7 | autonomous speech | yes (switch off) |
| 8 | enable, observe, decide | **live** |

Phases 1–2 and 4/6 can proceed in parallel; 3 depends on 1, 5 depends on 2–4, 7 depends
on 5. Nothing before Phase 8 can spend money or speak.
