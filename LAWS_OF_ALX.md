# Laws of AL/X

**Status:** Draft — owner approval required  
**Owner:** Friedl  
**Current phase:** Construction laws only

These laws govern how AL/X may be designed and built. They take precedence over local feature convenience, existing implementation patterns, model preferences, and delivery speed.

No implementer or model may weaken, reinterpret, bypass, or silently introduce an exception to an approved law. Proposed amendments and exceptions require Friedl's explicit approval before implementation.

## Founding laws

### Law 1 — Absolutely no hard-coding unless Friedl explicitly asks for it

No phrase matching, keyword routing, scripted conversational paths, fixed workflow sequences, special-case conversational handlers, predetermined responses, or hidden assumptions about how a task must proceed may be introduced unless Friedl explicitly requests and approves that deterministic behaviour.

### Law 2 — No dumb systems; AL/X must be agentic

AL/X is a co-designer. She understands goals, reasons from context, chooses and composes capabilities, evaluates results, asks intelligent questions when genuinely blocked, and continues working toward the goal.

AL/X must not be reduced to a command router, menu system, workflow engine, or voice-operated collection of buttons.

### Law 3 — One voice and conversation path only

Every conversational input, whether spoken or typed, enters the same authoritative AL/X conversation and reasoning path.

Features, tools, integrations, routes, and frontend surfaces may not create their own language interpreters, phrase handlers, confirmation logic, response generators, conversational memory, or parallel conversational paths.

## Proposed supporting construction laws

The following laws were proposed during the founding discussion. They are not approved merely because they appear here; each must be reviewed with Friedl before its status changes.

### Law 4 — Only AL/X may interpret the user

Raw user language may be interpreted only by the single authoritative AL/X agent. Tools, frontend code, routes, and integrations receive structured instructions and may not inspect the user's wording to decide what it means.

### Law 5 — Tools are language-blind primitives

A tool exposes one reusable external capability through structured inputs and structured results. It contains no trigger language, conversational interpretation, or complete user journey disguised as a tool.

### Law 6 — Workflows belong to AL/X

AL/X decides dynamically which capabilities to use and in what order. A workflow sequence may not be encoded in application code unless Friedl explicitly requests and approves that deterministic sequence.

### Law 7 — AL/X works toward goals, not isolated commands

A meaningful request establishes or updates a goal. AL/X maintains the objective, context, decisions, progress, blockers, and outstanding work across turns.

### Law 8 — An unfinished goal remains active

AL/X stops only when the goal is complete, genuinely blocked, awaiting required user input or approval, or explicitly cancelled. A tool result does not end a task while useful, authorised progress remains possible.

### Law 9 — Every tool result returns to AL/X

AL/X evaluates tool results and decides the next step. Tools and frontend handlers may not privately select the next workflow step or substitute their own final conclusion for AL/X's reasoning.

### Law 10 — The frontend has no business authority

The frontend displays state, gathers input, and performs genuine interface operations. It may not interpret user meaning, select domain capabilities, orchestrate business workflows, or own authoritative conversational or goal state.

### Law 11 — New wording must not require new code

If AL/X already has the capabilities needed for a request, a new phrase, synonym, language, conversational style, or follow-up wording must work without adding an intent, route, action, regular expression, or handler.

### Law 12 — New tools require a genuinely new primitive capability

A new tool is justified only by an external capability AL/X does not already possess. New wording or another step in a user journey is not justification for a new tool.

### Law 13 — No fixed intent menu may limit AL/X's reasoning

A capability catalogue may describe available tools, but it may not define the only goals AL/X is allowed to understand. AL/X must not be reduced to selecting one workflow from a predefined intent menu.

### Law 14 — One request may use any number of tools

The architecture may not impose a one-turn, one-route, or one-action limit. AL/X may reason, call a tool, evaluate its result, and continue until the goal reaches a legitimate stopping condition.

### Law 15 — Context must be authoritative and durable

Goals, referents, decisions, blockers, approvals, and completed work may not exist only in browser state, prompt text, or short-lived process memory. Restarting AL/X must not erase unfinished work.

### Law 16 — Exceptions require explicit owner approval

Any proposed hard-coded or deterministic behaviour must be identified before implementation, justified, and explicitly approved by Friedl. It may not be introduced quietly as an implementation detail.

### Law 17 — Tests verify goals and behaviour, not trigger phrases

Acceptance tests use varied wording, follow-ups, corrections, interruptions, and restarts. Tests may not define correctness merely as a particular phrase returning a particular intent or action.

### Law 18 — Architectural violations fail automatically

Approved laws must have objective enforcement wherever technically possible. Phrase routers, parallel conversation paths, frontend domain orchestration, unapproved workflow tools, and other prohibited structures must fail automated checks rather than rely on memory or review promises.

## Founding litmus tests

- If a reasonable new way of asking for an existing capability requires application code, the architecture has drifted.
- If a tool describes the user's entire journey rather than one reusable capability, it is probably a hard-coded workflow.
- If a feature can interpret the user without going through AL/X, multiple AL/X systems have been created.

## Approval record

No supporting law is approved until Friedl reviews it. Approval status, amendments, and explicitly authorised exceptions will be recorded here before implementation proceeds.

