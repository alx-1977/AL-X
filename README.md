# AL/X

This is the clean rebuild of AL/X.

## Current phase

Governance only. No application implementation, integration, workflow, agent runtime, frontend, or migration from the previous JARVIS repository is authorised yet.

The founding **Laws of AL/X** are owner-approved. The repository contains the initial model-instruction and enforcement specification layer, a proposed foundation architecture, and a proposed behavioural proof. Runtime implementation may begin only after Friedl accepts the architecture's behaviour and meaningful trade-offs in plain language and the executable enforcement gates defined in `docs/LAW_ENFORCEMENT.md` exist.

## Mandatory instructions

Every model and contributor must begin with `AGENTS.md`, which requires the complete reading of the canonical laws and their enforcement specification. Model-specific instruction files only point to that authority; they do not maintain separate copies of the rules.

## Current foundation proposals

- `docs/ARCHITECTURE_BLUEPRINT.md` describes the single-path, model-independent AL/X foundation in plain language.
- `docs/FOUNDATION_PROOF.md` defines the behavioural demonstration that must pass before real integrations are accepted.

## Source of authority

Friedl is the product owner and final authority for what AL/X is, how she behaves, and how she may be built. Models and implementers may propose changes, but may not weaken, reinterpret, or silently create exceptions to an approved law.

## Previous system

The previous JARVIS/AL/X repository is reference material only. This project must not import, depend on, or copy its orchestration architecture. Useful domain behaviour may be studied and reimplemented later, one primitive capability at a time, after the new foundation is proven.
