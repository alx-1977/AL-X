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
| 1 | Static checks reject phrase/keyword/regex intent routing and embedded environment-specific behaviour outside approved constant/config boundaries. | Explain every newly encoded constant and why it is universal, protocol-mandated, configurable, or explicitly approved. |
| 2 | Agent-loop tests prove multi-step planning, tool composition, result evaluation, replanning after failure, verification, and constructive challenge. | Demonstrate that AL/X can advance a goal without Friedl prescribing each step. |
| 3 | Architecture tests prove all conversational inputs reach the same ingress and agent authority; no feature may emit an independent assistant response. | List every input and output surface and trace it through the single path. |
| 4 | Boundary tests reject raw user language in tool, route, integration, and frontend domain interfaces. | Explain any component that processes domain documents and prove it does not interpret Friedl's intent. |
| 5 | Capability-schema checks require structured inputs/results and reject trigger vocabulary and bundled journey definitions. | Justify each new capability as a reusable primitive. |
| 6 | Dependency and integration tests prove application code cannot privately sequence domain workflows outside the agent loop. | Describe how AL/X dynamically chooses the demonstrated sequence. |
| 7 | Behavioural tests inspect persisted goal objective, context, decisions, progress, blockers, and outstanding work across turns. | Show how a request creates or updates a goal. |
| 8 | Continuation tests prove AL/X keeps working after intermediate results and stops only for an allowed terminal condition. | Identify the terminal condition and evidence for it. |
| 9 | Tool-loop tests prove every result re-enters AL/X before another action or final conclusion. | Trace at least one multi-tool result chain. |
| 10 | Frontend boundary tests reject domain capability selection, workflow orchestration, and authoritative goal/conversation state in client code. | Review frontend changes for presentation/input responsibility only. |
| 11 | Paraphrase tests use meaning-equivalent wording, follow-ups, corrections, and different styles without code-path-specific expectations. | Confirm no production change was needed for new wording. |
| 12 | Registry checks require a capability justification and reject duplicate or workflow-shaped tools. | Approve the primitive boundary of every new tool. |
| 13 | Tests prove requests are not limited to a fixed intent enumeration; the catalogue describes capabilities only. | Review schemas and prompts for hidden intent menus. |
| 14 | Agent tests prove zero, one, and multiple tool calls are possible in one active goal, including replanning. | Demonstrate a genuine multi-capability goal. |
| 15 | Restart tests prove durable recovery; data-policy tests cover inspection, correction, deletion, and retention controls. | Approve the context data model and retention policy. |
| 16 | CI verifies that every exception identifier exists in `governance/EXCEPTIONS.md` and matches its exact scope and expiry/review condition. | Friedl explicitly approves each exception before implementation. |
| 17 | Test-policy checks reject suites whose behavioural coverage depends only on exact trigger phrases or intent labels. | Review scenarios for varied language, interruptions, corrections, and restarts. |
| 18 | CI makes all implemented law gates required and prevents silent skipping. | Record evidence for any law that cannot yet be mechanically verified. |

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
