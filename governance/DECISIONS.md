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

## D-004 — Voice providers and final activation behaviour

- **Date:** 2026-08-27
- **Decision owner:** Friedl
- **Decision:** Use Cartesia as the initial speech-to-text provider and ElevenLabs, with the configured preferred voice, for text-to-speech. Both remain replaceable adapters. When AL/X moves to her own machine, local speech-to-text may be restored with a stronger local model without changing the conversation, Core, memory, or frontend authority boundaries.
- **Single path:** Speech-to-text transcribes only. Every final transcript enters the same Conversation Gateway and AL/X Core used by every other conversational input. Text-to-speech receives only AL/X's authoritative response.
- **Activation:** A temporary click control is acceptable for the first executable slice. It is not the final interaction model and must not become a separate conversation route.
- **Prohibition:** Wake words and phrase-based voice activation or routing are not part of AL/X. The eventual ambient system may use local voice activity and speaker evidence, but only the AL/X Core may infer from the full conversational context whether Friedl is addressing her.
- **Initial configured models:** Friedl selected `grok-4.6` for reasoning, Cartesia `ink-2` for transcription, and ElevenLabs `eleven_v3` with his existing configured voice for synthesis. These are initial configuration choices, not permanent architecture.
- **Initial turn profile:** Friedl selected Cartesia's Responsive starting profile: start threshold `0.7`, eager-end threshold `0.5`, end threshold `0.4`, and end timeout `4500 ms`. The values remain configurable and will be tuned from observed conversations.
