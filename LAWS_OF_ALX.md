# Laws of AL/X

**Status:** Approved by Friedl on 2026-08-31
**Owner:** Friedl

Three laws govern how AL/X is built. They take precedence over local convenience,
existing patterns, model preferences, and delivery speed.

No implementer or model may weaken, reinterpret, or bypass these laws. Amendments
require Friedl's explicit approval.

---

## Law 1 — AL/X decides meaning

Intent, interpretation, judgment, and the choice of what to do next belong to the
single authoritative AL/X reasoning path.

Every conversational input — typed, spoken, corrected, confirmed — enters that one
path. Tools, routes, integrations, and frontends receive structured values and
return structured results; they never inspect what Friedl meant. No phrase routing,
no keyword matching, no intent menu, no second assistant voice.

AL/X reasons from goals rather than isolated commands, keeps working while useful
progress remains, and may use as many capabilities as a goal requires. She is a
co-designer: she questions assumptions, disagrees when the evidence warrants, and
says so plainly.

She alone judges what is worth remembering. Significance is never a formula.

## Law 2 — Code executes known procedures

Deterministic software carries out mechanical steps AL/X has chosen.

A capability performs one reusable outcome through structured inputs and structured
results. It may contain whatever deterministic mechanical steps are required to
achieve that outcome. It may validate what it is given. It may not invent or choose
business meaning, and it may not contain a complete user journey.

Anything a machine can do reliably and identically every time should be code, not a
reasoning call.

## Law 3 — Ambiguity returns to AL/X

Deterministic code may resolve only conditions with a single objectively correct
outcome.

Anything requiring interpretation, policy choice, business intent, or unresolved
ambiguity stops and returns to AL/X with what it found. Code never substitutes its
own conclusion for her reasoning.

Consequential actions require the authority recorded in `governance/DECISIONS.md`.
Goals, decisions, and progress survive restarts, with inspection, correction, and
deletion available to Friedl.

---

## What enforces these laws

These laws state principles. The specific prohibitions are enforced by code, not
by prose:

| Enforced by | What it checks |
| --- | --- |
| `scripts/check_architecture.py` | raw-language parameters, phrase and regex routing, intent/action/command naming, module dependency boundaries, provider isolation, frontend authority |
| `scripts/check_governance.py` | canonical documents, approval markers, law checksum, decision records, `.env` protection |
| `tests/` | agent-loop planning and replanning, restart recovery, paraphrase handling, approval gates, capability contracts, provenance and retention |

A change fails if any gate fails. Where a law cannot be checked automatically,
compliance requires recorded evidence in the pull request — "the model will
understand" is not evidence.

## Capability invention

AL/X may imagine and design capabilities neither Friedl nor her developers
anticipated, and may test them in an isolated sandbox with read-only access to
production data. An experiment cannot grant itself permissions, modify production,
or become approved merely because it succeeded.

Ideas are permissive. Experimentation is isolated. Deployment is governed.

## Litmus tests

- If a new way of asking for an existing capability needs new code, the
  architecture has drifted.
- If a capability describes the user's whole journey rather than one outcome, it
  is a hard-coded workflow.
- If anything interprets Friedl without going through AL/X, there are now two
  AL/X systems.
- If a reasoning call is being spent on something with one correct answer, it
  belongs in Law 2.

## Approval record

- 2026-08-26 — Nineteen founding laws approved.
- 2026-08-31 — Friedl replaced them with these three after the governance model
  became an obstacle to the work it protects. The mechanical prohibitions were not
  weakened; they moved from prose into the gates and tests that already enforced
  them. Deterministic execution of AL/X-composed values — such as committing a Xero
  bill she has decided — is permitted under Laws 2 and 3 without a separate
  approval ceremony.
