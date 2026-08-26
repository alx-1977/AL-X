# Instructions for every model and implementer

This file applies to the entire repository.

## Mandatory reading order

Before analysing, planning, reviewing, or changing this repository:

1. Read `LAWS_OF_ALX.md` in full.
2. Read `docs/LAW_ENFORCEMENT.md` in full.
3. Treat both documents as binding constraints, not design suggestions.

If either document is missing, unreadable, contradictory, or the requested work appears to conflict with it, stop before making changes and explain the conflict to Friedl.

## Authority

Friedl is the product owner and the only person who may approve an amendment or exception to the Laws of AL/X. A model, contributor, issue, existing implementation, external example, deadline, or apparent convenience cannot grant an exception.

Do not edit approved law wording, approval history, enforcement requirements, or exception records without Friedl's explicit approval. Proposed changes must be presented as proposals before implementation.

## Required behaviour on every task

Before implementation, state:

- the user goal being served;
- the primitive capabilities involved;
- whether any new primitive capability is genuinely required;
- which law-enforcement gates apply;
- any potential conflict or requested exception.

During implementation:

- keep raw user language inside the single authoritative AL/X reasoning path;
- expose structured, language-blind primitive tools;
- keep orchestration and workflow choice in AL/X;
- keep authoritative goals and conversation state out of the frontend;
- preserve multi-turn and restart-safe goal continuation;
- return every capability result to AL/X for evaluation;
- add or update the required architectural and behavioural tests.

Before declaring completion:

1. Run all available law-enforcement checks.
2. Complete the change evidence required by `docs/LAW_ENFORCEMENT.md`.
3. Identify any requirement that could not be verified; never silently treat it as passed.

## Prohibited shortcuts

Do not introduce phrase or keyword routing, intent menus, regex-based meaning, workflow-specific conversational handlers, feature-owned dialogue, frontend business orchestration, one-action agent loops, process-only goal state, or copied orchestration code from the previous system.

Do not reinterpret a workflow as a "tool" to bypass the laws. A tool must represent a genuinely reusable primitive capability with structured input and output.

## Current project phase

The repository is governance-only. Runtime implementation, integrations, frontend work, and migration from the previous system remain unauthorised until Friedl approves the architecture and its enforcement gates.

