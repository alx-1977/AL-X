# AL/X Foundation Technical Plan

**Status:** Selected implementation approach for the accepted foundation
**Scope:** Foundation and proof only; no live integrations or frontend

## Recommendation

Build the foundation as a small Python service with an independently owned SQLite goal store and a provider-neutral model adapter. Begin without a web framework or frontend. Prove the Core Agent from automated tests and a local command-line harness before adding transport or presentation layers.

This choice keeps the first proof small, observable, inexpensive to run, and suitable for later document, engineering, accounting, and local-machine capabilities.

## Initial technology choices

- **Python 3.12 or newer:** one language for the agent loop, structured tools, persistence, engineering automation, and enforcement scripts.
- **SQLite:** transactional, durable local goal/context storage without operating another server. Storage is accessed through a repository interface so another database can replace it without changing AL/X's reasoning.
- **Python standard library for the first gates:** the guardrails run without downloading dependencies.
- **xAI Responses API behind a model adapter:** Grok is evaluated first, but no module outside `providers` may depend on xAI.
- **Provider-owned conversation state is optional acceleration only:** AL/X's inspectable goal records remain authoritative.
- **Frontend technology deferred:** a frontend is unnecessary to prove reasoning, pivoting, persistence, and safety. When introduced, it will remain a thin interface and reuse the approved video asset.

Versions, model identifiers, database locations, usage limits, and environment values are configuration. They are not embedded as product behaviour.

## Code boundaries

Runtime source will live under `src/alx` with these top-level boundaries:

- `contracts` — shared structured types and interfaces, with no implementation authority;
- `core` — the single AL/X reasoning loop;
- `conversation` — the single conversational ingress;
- `goals` — durable goal/context storage;
- `capabilities` — primitive registry and broker;
- `safety` — deterministic authority and approval checks;
- `tools` — primitive external-capability implementations;
- `providers` — replaceable language-model adapters;
- `interfaces` — transport and presentation adapters only;
- `bootstrap` — the sole composition root that connects implementations;
- `config` — environment and configuration loading without behavioural decisions.

Internal imports must follow the machine-readable rules in `architecture/boundaries.toml`. The composition root is the only place allowed to know all concrete parts.

## Implementation sequence

### Phase 0 — Enforcement first

- activate governance and architecture checks;
- test the checks against deliberate violations;
- run them in GitHub Actions;
- make the law-gate check required on `main`.

No runtime source is accepted before Phase 0 is complete.

### Phase 1 — Durable contracts and goals

- define structured goal, event, evidence, action, result, approval, and terminal-state contracts;
- implement the SQLite goal store with schema migration from its first version;
- prove create, inspect, correct, retain, delete, stop, and restart behaviour.

### Phase 2 — Capability and safety boundaries

- implement the primitive capability contract, registry, and broker;
- implement configurable side-effect and approval rules;
- prove that raw user language cannot enter tools and that unauthorised actions cannot execute.

### Phase 3 — Single Core Agent

- implement one provider interface and the single reasoning loop;
- route every result back through the loop;
- persist state before and after consequential transitions;
- enforce legitimate terminal conditions without prescribing a domain workflow.

### Phase 4 — Grok foundation proof

- add only artificial primitive tools and artificial data;
- run paraphrase, multi-tool, correction, interruption, failure, pivot, approval, and restart tests;
- record accuracy, tool behaviour, tokens, latency, and estimated cost.

### Phase 5 — Datasheet benchmark

- run Friedl's verified MPS case and additional unseen components through the same Core Agent;
- evaluate retrieval, source quality, revision accuracy, engineering interpretation, uncertainty, and citations;
- accept Grok only if it passes every law-critical condition; otherwise run the same benchmark with OpenAI.

### Phase 6 — First real capabilities

Only after the foundation passes, propose primitive email and calendar capabilities for Friedl's plain-language acceptance. No workflow-specific email or calendar path is created.

## Evidence before each phase advances

The implementation report must show what was built, which gates ran, deliberate violations the gates caught, behavioural results, usage measurements, unresolved risks, and exceptions or `none`. Friedl is shown observable outcomes and meaningful trade-offs; technical certification remains the implementers' responsibility.

