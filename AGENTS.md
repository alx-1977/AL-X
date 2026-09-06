# Instructions for every model and implementer

This file applies to the entire repository.

## Mandatory reading order

Before analysing, planning, reviewing, or changing this repository:

1. Read `LAWS_OF_ALX.md` in full.
2. Read `IDENTITY_AND_MEMORY.md` in full.
3. Read `docs/LAW_ENFORCEMENT.md` in full.
4. Read `docs/ARCHITECTURE_BLUEPRINT.md` and `docs/FOUNDATION_PROOF.md` in full when they exist.
5. Treat the approved laws, identity principles, enforcement requirements, accepted architecture, and approved foundation proof as binding constraints, not design suggestions. Respect the recorded status of any future proposal; a proposal may guide planning but does not authorise implementation.

If a required document is missing, unreadable, contradictory, or the requested work appears to conflict with it, stop before making changes and explain the conflict to Friedl.

## Authority

Friedl is the product owner and the only person who may approve an amendment or exception to the Laws of AL/X. A model, contributor, issue, existing implementation, external example, deadline, or apparent convenience cannot grant an exception.

Do not edit approved law wording, identity principles or origin memories, approval history, enforcement requirements, or exception records without Friedl's explicit approval. Proposed changes must be presented as proposals before implementation.

## Required behaviour on every task

Before implementation, state briefly what the change is for and whether it needs a
capability AL/X does not already have. Raise a conflict with the laws before
building, not after.

Also name the production outcome, its existing authoritative implementation
path, and every superseded or competing path that must be removed.

During implementation:

- preserve exactly one production path for the outcome and delete every
  superseded entry point, registration, dispatcher, handler, route and callable
  sequence;
- keep raw user language inside the single authoritative AL/X reasoning path;
- keep interpretation and the choice of what to do next in AL/X;
- put mechanical steps with one correct outcome in deterministic code;
- return ambiguity, judgment, and policy choices to AL/X;
- keep authoritative goals and conversation state out of the frontend;
- preserve multi-turn and restart-safe goal continuation;
- add or update the tests that enforce the above.

Before declaring completion, run the law gates and the test suite, and say plainly
what could not be verified. Never silently treat an unverified requirement as
passed.

## Prohibited shortcuts

Do not introduce phrase or keyword routing, intent menus, regex-based meaning, workflow-specific conversational handlers, feature-owned dialogue, frontend business orchestration, one-action agent loops, process-only goal state, or copied orchestration code from the previous system.

Do not retain a replaced or competing production path as hidden, deprecated,
wrapped, redirected, recovery-only, optional, renamed, or unregistered code.
Git history is the archive for removed implementations.

Do not reinterpret a workflow as a "tool" to bypass the laws. A tool must represent a genuinely reusable primitive capability with structured input and output.

## Current project phase

The initial executable gates and protected-branch law check are active. Friedl authorised the first permanent local voice-to-Core runtime and its narrowly scoped provider integrations in `governance/DECISIONS.md` decision D-007. That approval does not authorise domain integrations, production writes, autonomous deployment, or migration of logic from the previous system. Further implementation phases still require their recorded gates and Friedl's approval.
