# AL/X Decision Record

This file records approved product and architecture decisions that guide implementation without altering the Laws of AL/X. Decisions cannot create exceptions to a law.

## D-001 — Foundation architecture accepted

- **Date:** 2026-08-26
- **Decision owner:** Friedl
- **Decision:** The six-part, single-path architecture in `docs/ARCHITECTURE_BLUEPRINT.md` describes the required AL/X foundation.
- **Meaning:** Friedl accepted the observable behaviour and meaningful trade-offs in plain language. Models and implementers retain responsibility for technical correctness and proof.

## D-002 — Runtime model evaluation order

- **Date:** 2026-08-26
- **Decision owner:** Friedl
- **Decision:** Test Grok first as the AL/X runtime model. If it fails the foundation requirements, test OpenAI next.
- **Constraints:** Model selection remains configuration behind the single model adapter. Changing the runtime model cannot create another conversation path or require feature code.
- **Development distinction:** OpenAI/Codex may be used for implementation and code review regardless of which model runs AL/X. Development-tool usage does not make that provider a second AL/X authority.
- **Evaluation:** Correctness, pivoting, tool use, continuity, safety, citations, conversational character, usage, and cost are measured. A law-critical failure is disqualifying.

## D-003 — Reuse the original video background

- **Date:** 2026-08-26
- **Decision owner:** Friedl
- **Decision:** Reuse the original AL/X video background when frontend work begins. Do not spend time or usage creating a replacement now.
- **Boundary:** Only the visual media asset may be copied after its exact source is verified. Old frontend logic, orchestration, handlers, routes, and state are not authorised for migration.
- **Timing:** No visual or frontend work occurs during the foundation proof.

