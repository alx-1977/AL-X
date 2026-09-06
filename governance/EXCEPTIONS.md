# Approved exceptions to the Laws of AL/X

An exception is valid only when Friedl explicitly approves it before implementation and this register records:

- a unique identifier;
- the exact law and exact code or behaviour affected;
- why the exception is necessary;
- alternatives considered;
- risks and safeguards;
- the narrow approved scope;
- approval date;
- expiry date or mandatory review condition.

Silence, prior implementation, model recommendation, technical convenience, and approval of a broader feature do not constitute approval of an exception.

---

## EX-001 — Origin-selected Core for the D-024a Luna evaluation

### Register metadata

- **Law:** Law 0 (one outcome, one production path) and Law 1 (AL/X decides meaning).
- **Scope:** `OriginSelectedReasoner` in `src/alx/bootstrap/reasoning.py` and its single composition site in `src/alx/bootstrap/live_voice.py`; `PERSON_TURN` → OpenAI `gpt-5.6-sol` / `medium`, and `EXTERNAL_EVENT`, `WORK_COMPLETED`, `SELF_REQUESTED` → OpenAI `gpt-5.6-luna` / `max`. Nothing else.
- **Necessity:** the evidence for choosing a permanent Core topology for autonomous turns does not exist, and cannot be produced without running both configurations under one identity. See "Why this is necessary" below.
- **Alternatives:** Sol for all turns; Luna for all turns; delaying the experiment. All considered and set out below.
- **Risks and safeguards:** inconsistent judgement between configurations, drift into semantic routing, an unconcluded experiment becoming architecture, and procedural drift; guarded by an origin-only architecture gate, one CoreAgent/broker/gate, no fallback, off-by-default operation and a hard spend fuse. Set out in full below.
- **Approved by Friedl:** yes, explicitly, for this exact wording and scope.
- **Approval date:** 2026-09-03.
- **Expiry or review condition:** conclusion of the D-024a Luna experiment, requiring an explicit Friedl-approved decision recording the permanent Core topology. Not renewable by silence.

| Field | Value |
| --- | --- |
| **Exception ID** | **EX-001** |
| **Status** | **APPROVED** |
| **Approval date** | **2026-09-03** |
| **Approved by** | Friedl |
| **Laws affected** | **Law 0** (one outcome, one production path) and **Law 1** (AL/X decides meaning) |
| **Affected code** | `OriginSelectedReasoner` in `src/alx/bootstrap/reasoning.py`, and its single composition site in `src/alx/bootstrap/live_voice.py` |
| **Affected behaviour** | `PERSON_TURN` → OpenAI `gpt-5.6-sol` / `medium`; `EXTERNAL_EVENT`, `WORK_COMPLETED`, `SELF_REQUESTED` → OpenAI `gpt-5.6-luna` / `max` |
| **Mandatory review / expiry** | Conclusion of the D-024a Luna experiment. Not renewable by silence. |
| **Related decision** | D-024a in `governance/DECISIONS.md` |

### Procedural history

The experimental split was initially implemented before the required exception
was raised. That sequencing violated the exception procedure recorded at the
top of this register.

That implementation was removed in commit `2037eb4`, before this exception was
approved and before any merge or live activation. This approval therefore does
**not** retroactively legitimise the earlier implementation. The experiment may
only be re-implemented after this approved exception is committed.

The removal is recorded in the branch history rather than erased from it.

### Laws affected

**Law 0 — One outcome. One production path.** The production outcome "a Core
reasoning decision for one turn" is reached through one of two `ModelReasoner`
instances, selected before reasoning begins. Two instances of the same
authoritative path exist where the law requires one.

**Law 1 — AL/X decides meaning.** Deterministic code selects which reasoning
authority produces the decision, before that authority has reasoned. The
selection reads only provenance and never content, so no interpretation of
Friedl occurs; the exception is recorded against Law 1 regardless, because the
choice of which mind answers is made outside the mind.

### Exact code and behaviour affected

- `OriginSelectedReasoner` in `src/alx/bootstrap/reasoning.py`, and only its
  `decide` method, whose entire selecting logic is:

  ```python
  if context.origin.is_autonomous:
      if self._autonomous is None:
          raise AutonomousReasonerUnavailable(context.origin.value)
      return self._autonomous.decide(context)
  return self._conversational.decide(context)
  ```

- Its single construction in `src/alx/bootstrap/live_voice.py`.

Nothing else. No other module may select, construct, or reference both
reasoners.

### Approved behaviour

| Origin | Model | Effort |
| --- | --- | --- |
| `PERSON_TURN` | OpenAI `gpt-5.6-sol` | `medium` |
| `EXTERNAL_EVENT`, `WORK_COMPLETED`, `SELF_REQUESTED` | OpenAI `gpt-5.6-luna` | `max` |

Selection is strictly by `CognitionOrigin`, strictly in composition, and
nowhere else. Both paths use the same `CoreAgent`, Laws, identity, contracts,
continuity context, goals, memory, notebook, `CapabilityBroker`, `SafetyGate`
and capability set. The only permitted differences are provider, model,
reasoning effort, and the provider-side token bounds the autonomous
reservation is computed against.

An autonomous origin with no autonomous reasoner configured raises
`AutonomousReasonerUnavailable` and is refused. There is no fallback to the
conversational Core.

### Explicitly prohibited by this exception

This exception authorises the arrangement above and nothing adjacent to it.
The following remain full violations and are not covered:

- routing by topic, subject or keyword;
- routing by capability;
- routing by goal or goal state;
- routing by content, intent, importance, urgency, priority or domain;
- any classifier, scorer or other pre-Core intelligence deciding which
  reasoner answers, or whether an occasion deserves cognition at all;
- a sidecar, curiosity, personality or relationship model;
- a generic model-router abstraction, registry, strategy object or lookup
  table, whether or not it currently routes on origin;
- any third or further reasoning-authority path;
- extending origin selection to anything other than the two models named
  above.

### Why this is necessary

D-024 gives AL/X occasions to think when nobody has asked her to. Whether that
produces something worth having is an open question, and the model-and-effort
configuration answering an unprompted turn is one of the few variables likely
to decide it. The evidence for choosing a permanent Core does not exist yet.

The experiment exists to produce that evidence: whether the Luna/`max`
configuration produces better autonomous cognition than the conversational
Sol/`medium` configuration, or worse, or indistinguishable. Nothing is assumed
about which will prove better; that is the question, not the premise.

Evidence sought covers continuity and personality quality, autonomous
judgement, the interests she chooses, how often she requests further cognition,
speech versus silence, and real cost. Running both configurations under one
identity and one capability environment is the only way to compare them without
comparing two different minds.

### Alternatives considered

**Sol for all turns.** Preserves one Core exactly and needs no exception. It
answers a different question than the one being asked: whether a different
model-and-effort configuration performs differently on unprompted turns cannot
be learned by never varying it. Available at any time as the conclusion of the
experiment rather than a substitute for it.

**Luna for all turns.** Also preserves one Core, and removes the split by
moving conversation onto a `max`-effort model. Rejected on latency and cost for
ordinary conversation, and because it changes the Core Friedl actually talks to
in order to answer a question about autonomous turns.

**Delaying the experiment.** Ship Phases 0–7 with one Core and add the second
later. Rejected because the observation period is the point of Phase 8, and the
Luna question would still require this same exception whenever it was asked —
the deferral buys nothing but time.

### Risks

**Inconsistent personality and judgement between configurations.** Her
memories, preferences and self-model would be written by two configurations and
read by both. If Sol and Luna differ materially in temperament, the entity
deciding whether to interrupt Friedl is systematically not the entity he
converses with. This is a continuity problem in a workstream whose purpose is
continuity, and it is among the primary things the experiment is meant to
detect.

**Accidental evolution into semantic routing.** The likeliest regression. A
future change adds "only use Luna when the goal is stale" or "when the topic is
technical," and origin selection becomes topic routing without anyone deciding
to build it.

**Two reasoning authorities becoming permanent architecture.** An experiment
nobody concludes becomes the architecture by default. The failure mode is not a
bad decision; it is no decision.

**Procedural drift.** This exception was itself raised after implementation
once. The safeguard is that the implementation was removed rather than
grandfathered, and that this entry records the fact rather than obscuring it.

### Safeguards

- `scripts/check_architecture.py` enforces that only
  `bootstrap/reasoning.py` and `bootstrap/live_voice.py` may name both
  reasoners, and that the selecting class may read nothing semantic — a
  24-token blocklist covering topic, capability, goal, notebook, research,
  memory, intent, importance, priority, urgency, domain, sentiment, score and
  interest. Each rejection is proven by injecting the violation.
- One `CoreAgent`, one `CapabilityBroker`, one `SafetyGate`, one capability
  registry. `CoreAgent`, the broker and the gate are never told which model
  answered, and tests assert they cannot discover it.
- No fallback: an autonomous origin without a configured autonomous reasoner
  is refused, never answered by the conversational Core. A test asserts zero
  conversational calls across all autonomous origins.
- Autonomous cognition is off by default and separately fused by a hard daily
  spend ceiling.
- The arrangement is recorded in D-024a as explicitly experimental and
  without standing as architecture.

### Narrow scope

This exception covers the two named models, selected by `CognitionOrigin`, in
`OriginSelectedReasoner` and its single composition site, for the duration of
the D-024a evaluation. It authorises no other dual-Core arrangement, no model
routing of any kind, and no reasoning authority beyond the two named.

### Expiry and mandatory review

This exception expires when the D-024a Luna experiment is concluded.

Conclusion requires an explicit Friedl-approved decision recording the
permanent Core topology — Luna, Terra, Sol, or another single authoritative
Core configuration. Until that decision is recorded, the arrangement has no
standing as architecture; once it is recorded, this exception lapses and is not
precedent for any future dual-Core or routing proposal.

The exception does not renew by silence, by continued operation, by the
experiment producing good results, or by the passage of time.
