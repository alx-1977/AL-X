# AL/X

This is the clean rebuild of AL/X.

## Current phase

The governed foundation runtime is under incremental implementation. Domain integrations and workflows remain out of scope until the foundation proof passes. The first voice work is limited to the single Conversation Gateway and replaceable reasoning, transcription, and synthesis provider boundaries approved by Friedl.

The founding **Laws of AL/X**, foundation architecture, and initial voice boundary are owner-approved. Every increment must pass the executable governance and architecture gates before review.

## Mandatory instructions

Every model and contributor must begin with `AGENTS.md`, which requires the complete reading of the canonical laws and their enforcement specification. Model-specific instruction files only point to that authority; they do not maintain separate copies of the rules.

## Current foundation

- `docs/ARCHITECTURE_BLUEPRINT.md` describes the accepted single-path, model-independent AL/X foundation.
- `docs/FOUNDATION_PROOF.md` defines the approved behavioural demonstration that must pass before real integrations are accepted.
- `docs/TECHNICAL_PLAN.md` defines the minimal implementation sequence.
- `IDENTITY_AND_MEMORY.md` contains Friedl-approved identity principles, origin memories, and autobiographical-memory boundaries subordinate to the Laws.
- `src/alx/conversation` contains the only conversational ingress.
- `src/alx/providers` isolates external reasoning, STT, and TTS APIs behind provider-neutral contracts.
- `src/alx/core/model_reasoner.py` is the single provider-neutral bridge from approved identity, durable state, and primitive schemas to an authoritative Core decision.
- `src/alx/memories` persists Core-selected factual, relationship, and autobiographical memories with provenance, revision history, person isolation, and retention controls. The Core may retrieve a narrowly scoped selection through structured metadata, while the store never interprets the conversation or decides semantic relevance.
- `.env.example` lists provider selection and model settings; provider and model choices are never embedded into Core behaviour.

## Source of authority

Friedl is the product owner and final authority for what AL/X is, how she behaves, and how she may be built. Models and implementers may propose changes, but may not weaken, reinterpret, or silently create exceptions to an approved law.

## Previous system

The previous JARVIS/AL/X repository is reference material only. This project must not import, depend on, or copy its orchestration architecture. Useful domain behaviour may be studied and reimplemented later, one primitive capability at a time, after the new foundation is proven.
