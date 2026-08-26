# AL/X Foundation Architecture Blueprint

**Status:** Accepted by Friedl on 2026-08-26
**Scope:** The AL/X foundation only; no email, calendar, Xero, production, or design capability is implemented here

## The promise

AL/X is given a goal, not a prescribed route. She chooses an approach, uses available capabilities, evaluates what happened, changes course when necessary, and continues until the goal is complete or genuinely blocked.

We structure AL/X's capabilities, safety boundaries, and memory—not her reasoning path.

## The single path

```text
Typed input ─┐
Voice text ──┼──> Conversation Gateway ──> AL/X Core Agent ──> Final AL/X response
Events ──────┘                              │       ▲
                                           │       │
                                      proposed   structured
                                       action      result
                                           │       │
                                           ▼       │
                                  Capability Broker ──> Safety Gate ──> Primitive Tool
                                           ▲
                                           │
                                    Durable Goal Store
```

There is no email conversation path, schematic-review path, coding path, or Grok path. Every user message reaches the same AL/X Core Agent. Every result returns to that agent. Only that agent decides what to do next and what AL/X says.

## The six foundation parts

### 1. Conversation Gateway

This is the only entrance for user conversation. Typed text and speech transcripts are represented in the same form. A confirmation, correction, or follow-up is another turn in the same conversation—not a special action route.

Background events may enter through the gateway as labelled facts. They cannot speak to Friedl or choose an action by themselves.

### 2. AL/X Core Agent

This is AL/X's only reasoning authority. It receives the active goal, relevant durable context, available primitive capabilities, results, and safety constraints. It can reason again as many times as necessary.

The Core Agent may propose a capability call, update its understanding of the goal, request genuinely necessary information or approval, challenge an assumption, or declare a goal complete with evidence. It cannot claim completion merely because a tool returned successfully.

The language model sits behind a replaceable model adapter. Exactly one configured model acts as AL/X for a reasoning turn. Changing from one model provider to another does not create a new conversation route, tool set, workflow, memory system, or personality.

### 3. Durable Goal Store

This is AL/X's authoritative working memory. It survives restarting the backend, browser, or computer process.

For each active goal it records, in inspectable form:

- the objective and current status;
- what success means and the evidence collected;
- relevant context, referents, and artifacts;
- decisions, corrections, and approvals;
- completed actions and their results;
- blockers, unresolved questions, and useful next possibilities;
- retention and deletion information.

A provider's conversation history or hidden reasoning is not the authoritative goal record. Provider storage may assist continuity, but AL/X must be able to reconstruct the goal from her own durable records.

### 4. Capability Registry and Broker

The registry describes the reusable things AL/X can do. Each capability has a structured input, structured result, permission level, side-effect classification, and failure information.

The broker validates a proposed call, invokes the selected capability, records the outcome, and returns it to the Core Agent. It cannot select a workflow or interpret Friedl's words.

A capability such as `read_message` or `create_draft` can be composed into many goals. A capability such as `process_DHL_invoice_workflow` would encode a journey and is prohibited unless Friedl explicitly approves it as an exception.

### 5. Safety and Authority Gate

This deterministic boundary decides whether a proposed structured action is permitted now. It enforces identity, permissions, data boundaries, validation, and required approval for consequential actions.

It does not interpret conversational language. AL/X interprets Friedl's response and creates an explicit approval record; the gate checks that record against the proposed action's exact scope before allowing execution.

Reading information, preparing a draft, sending a message, paying a bill, and changing source code can therefore carry different configurable authority requirements without becoming different conversation paths.

### 6. Frontend

The frontend captures input and displays the authoritative conversation, active goals, approvals, progress, artifacts, and errors. It may perform genuine interface work such as recording audio or selecting a file.

It cannot interpret meaning, choose tools, advance workflows, own the only copy of a goal, manufacture AL/X responses, or bypass the safety gate.

## How AL/X pivots

The Core Agent follows a general reasoning cycle, not a domain workflow:

1. Load the active goal and current evidence.
2. Decide the most useful next step from the capabilities currently available.
3. Submit any proposed action through the broker and safety gate.
4. Observe and persist the result, including partial success or failure.
5. Reassess the goal in light of the new evidence.
6. Continue, revise the approach, request essential input or approval, or finish with evidence.

No application code defines what step 2 must be for email, datasheets, coding, or schematic review. If a source is blocked, a tool fails, or an assumption proves wrong, the result becomes new evidence for AL/X to reason about.

## Model independence and Grok

Grok is the leading initial model candidate because the demonstrated product behaviour matches an important AL/X need: independently locating an accessible datasheet, extracting the required engineering values, explaining the reasoning, and citing sources.

The model choice remains configuration, not architecture. Candidate models must use the same Core Agent, goals, tools, safety gates, and proof tests. The winning model is the one that most reliably satisfies AL/X's behavioural requirements at acceptable cost and latency; switching it must not require feature code.

Server-side model memory is never AL/X's durable memory. This prevents provider retention limits, model retirement, or a provider change from erasing unfinished goals.

## Resource use

Usage is controlled without reducing AL/X to a dumb router:

- send only goal-relevant context rather than the entire history;
- persist concise decisions and evidence instead of replaying every token;
- use provider caching or compaction where it does not become authoritative memory;
- choose reasoning depth based on the difficulty and risk of the current decision;
- avoid repeating tool results already captured in durable state;
- record token, tool, latency, and failure measurements in the proof tests.

Cost controls may cause AL/X to checkpoint or request permission to continue an unusually expensive operation. They may not silently truncate an active goal or replace reasoning with phrase routes.

## Not decided by this blueprint

This document deliberately does not select a programming language, database product, web framework, model provider, or visual design. Those implementation choices must support these boundaries and will be recommended with technical evidence. They cannot alter the approved behaviour.

## Acceptance record

Friedl confirmed on 2026-08-26 that this describes the AL/X he wants. This is acceptance of the behaviour and meaningful trade-offs, not technical certification. Models and implementers remain responsible for building the architecture correctly and proving it with the foundation tests.
