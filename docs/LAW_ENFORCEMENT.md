# Enforcement of the Laws of AL/X

**Status:** Initial enforcement specification; plain-language architecture acceptance required before runtime implementation

`LAWS_OF_ALX.md` is the canonical statement of the laws. This document defines the evidence and gates required to prove that an implementation follows them. It may clarify how compliance is demonstrated, but may not weaken or reinterpret a law.

## Division of responsibility

Friedl defines and approves what AL/X must be, how she must behave, what authority she may exercise, what information she may retain, and which product trade-offs or exceptions are acceptable. Technical proposals must present those decisions to him in plain language.

Models and implementers are responsible for translating those decisions into sound architecture, executable tests, and accurate evidence. They may not transfer technical validation to Friedl, treat his approval as proof that an implementation is correct, or require him to assess software mechanics he has not been given a reasonable way to evaluate.

Friedl's acceptance means that the described behaviour and meaningful trade-offs match his intent. Implementers remain accountable for proving that the system actually delivers that behaviour and complies with every law.

## Central design test

We structure AL/X's capabilities, safety boundaries, and memory—not her reasoning path.

AL/X must understand the goal, choose an approach, act, examine the evidence, adjust the approach, and continue until complete or genuinely blocked. If an expected step is unavailable or produces surprising evidence, AL/X must be able to pivot without a new phrase handler, route, or hard-coded fallback workflow.

## Gate policy

- A change fails if any applicable automated gate fails.
- A change fails if required review evidence is absent.
- "The model will understand" and "the reviewer will notice" are not evidence.
- Runtime implementation may not begin until Friedl accepts the architecture's behaviour and meaningful trade-offs in plain language and the implementers have produced the initial executable enforcement suite.
- A capability may not be declared complete until its architectural, behavioural, restart, safety, and paraphrase tests pass.
- Any unavoidable false positive in an automated gate must be resolved by improving the gate or by an explicitly approved, narrowly scoped exception. Disabling a gate is not a workaround.

## Required architecture boundaries

The architecture must provide these identifiable boundaries before domain capabilities are added:

1. One authoritative conversation ingress for typed input, speech transcripts, clarifications, corrections, and confirmations.
2. One AL/X agent loop that owns goal interpretation, planning, capability selection, result evaluation, replanning, and the final response.
3. One structured capability registry containing primitive capability schemas, permissions, and results—not trigger phrases or user journeys.
4. One durable goal/context service that survives process and frontend restarts and records objectives, decisions, approvals, blockers, progress, and outstanding work.
5. One safety/authority layer for authentication, permissions, validation, and approval of consequential actions without interpreting user intent.
6. Frontends that transport input and render authoritative state but cannot select domain actions or orchestrate workflows.

Any proposal must name the concrete module that owns each boundary. Two owners for conversation interpretation or orchestration are a design failure, not redundancy.

## Law-by-law gates

| Law | Automated evidence required | Human evidence required |
| --- | --- | --- |
| 1 — AL/X decides meaning | Static checks reject raw-language parameters, phrase and regex routing, intent/action/command naming, frontend domain authority, and parallel conversation paths. Agent-loop tests prove planning, replanning after failure, and multi-capability goals. Paraphrase tests use meaning-equivalent wording without code-path-specific expectations. | Trace every input surface through the single ingress. Confirm no production change was needed for new wording. Explain any component that reads domain documents and show it does not interpret Friedl's intent. |
| 2 — Code executes known procedures | Capability-schema checks require structured inputs and results and reject trigger vocabulary. Dependency and architecture tests prove module boundaries and provider isolation. | Justify each capability as one reusable outcome. A deterministic sequence longer than a single external call requires Friedl's recorded decision naming it. |
| 3 — Ambiguity returns to AL/X | Tests prove every condition without one objectively correct outcome returns rather than resolving itself, that consequential actions are gated by the recorded authority, and that goals, decisions and progress survive restart. | Identify what the capability refuses to decide and why. Approve the retained data and its retention policy. |

A change fails if any applicable gate fails. Where a law cannot be checked
automatically, compliance requires recorded evidence in the pull request.

## Required test scenarios for every conversational capability

Each capability must be exercised through AL/X, not by directly testing a hidden conversational handler:

- multiple natural paraphrases with identical underlying goals;
- a follow-up that relies on prior context without restating the goal;
- a correction that changes an earlier assumption;
- an interruption followed by resumption;
- a process restart followed by resumption;
- an intermediate tool failure that requires evaluation and replanning;
- a goal requiring more than one primitive capability where relevant;
- a consequential action that pauses for the correct approval;
- a result that AL/X verifies before claiming completion.

Exact-string unit tests may be used for protocols, schemas, or fixed external formats. They cannot serve as proof that AL/X understands a user goal.

## Change evidence

Every runtime or domain-capability change must include a short compliance record in the pull request or commit review containing:

- goal and scope;
- architecture boundaries affected;
- primitives reused, added, or changed;
- reason a new primitive is necessary, if applicable;
- raw-language data-flow trace;
- goal-state and restart behaviour;
- safety/approval impact;
- tests added and gates run;
- exceptions requested or `none`;
- unresolved evidence or `none`.

The author and reviewing model must both assess technical compliance. Friedl's explicit approval is required for laws and exceptions. His plain-language acceptance is required for observable architecture behaviour, meaningful product trade-offs, retained information, authority boundaries, and the purpose of a genuinely new primitive capability.

## Enforcement rollout

### Active now

- `AGENTS.md` is the repository-wide entry instruction.
- Model-specific entry files point to the same canonical instructions without duplicating the laws.
- `LAWS_OF_ALX.md` remains the sole canonical law text.
- `governance/EXCEPTIONS.md` is the only valid exception register.
- `governance/DECISIONS.md` records approved product and architecture decisions without creating exceptions.
- The pull-request template requires explicit compliance evidence.
- `scripts/check_governance.py` verifies the canonical documents, approval markers, law checksum, decision/exception records, model entry points, and `.env` protection.
- `scripts/check_architecture.py` enforces machine-readable module dependencies, provider isolation, prohibited source structures, raw-language tool boundaries, and detectable phrase routing.
- The enforcement tests inject deliberate violations and must prove that the gates reject them.
- GitHub Actions runs all available law gates on pushes and pull requests to `main`.

### Required before runtime code

- A plain-language architecture accepted by Friedl and identifying the six mandatory boundaries.
- An executable static-analysis suite covering structural prohibitions.
- An executable architecture-test suite proving dependency boundaries.
- CI that runs those suites and cannot silently skip them.
- Protected-branch required checks configured on GitHub.

### Required before the first integration is accepted

- Durable-goal restart tests.
- Agent-loop multi-step and failure-recovery tests.
- Single-conversation-path tests for text, voice transcripts, and confirmations.
- Safety and approval-gate tests.
- Paraphrase and conversational-continuity evaluations.

## Drift response

If a violation is found, stop feature work, record the failing law and evidence, remove or redesign the violating path, add a regression gate, and only then resume. Existing code, sunk effort, or schedule pressure does not grandfather a violation.
