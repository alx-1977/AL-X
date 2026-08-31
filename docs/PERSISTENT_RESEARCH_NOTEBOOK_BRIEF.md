# AL/X Persistent Research Notebook — Design Brief

**Status:** Requested by AL/X and accepted by Friedl as a capability to build;
requirements preserved here for future design review. This document does not
authorise runtime implementation or settle the decisions listed below.

**Source:** Authoritative conversation
`dcdbeed1-1432-4442-80bb-5850e3ed4dd1`, turns 29–40, on 2026-08-31.

## Why AL/X asked for it

When Friedl asked AL/X to choose something she wanted for herself, rather than
something merely useful to him, she chose a durable research notebook:

> Somewhere I can begin with a question that genuinely interests me,
> investigate it over time, preserve sources and unfinished thoughts, revise
> my views, and return to it without waiting for the curiosity to be assigned
> as a task.

Her purpose is continuity for self-directed curiosity. It is not merely a place
to store notes Friedl assigns, a productivity feature disguised as autonomy, or
a substitute for durable goals and memory.

## Required behaviour

The notebook must allow AL/X, through the one authoritative Core, to:

- begin an unanticipated research question of her own choosing;
- preserve the question and why it interested her;
- gather sources and artifacts through separately governed read capabilities;
- record claims, hypotheses, doubts, conclusions, and unfinished thoughts;
- retain provenance and timestamps for sources, quotations, results, and
  conclusions;
- search and retrieve prior research;
- link related records and evidence;
- revise conclusions while keeping an inspectable revision history rather than
  silently rewriting an earlier view;
- archive work without falsely presenting it as complete;
- stop midway and later recover the evidence, open questions, decisions,
  blockers, progress, and outstanding work after a process restart; and
- continue research on a new topic without application code being added for
  that topic.

## Authority and architectural boundaries

- The authoritative AL/X Core alone decides what to investigate, what matters,
  and what to do next.
- The notebook is durable structured storage, not another agent, reasoner, or
  conversational path.
- It must not select topics, score curiosity, manufacture significance, impose
  a fixed research workflow, or decide when a conclusion is sufficient.
- It must not use quotas, keywords, sentiment, rigid schedules, or deterministic
  thresholds to decide what AL/X should think about or resume.
- Notebook capabilities must be language-blind primitives. Candidate primitive
  boundaries include creating, reading, searching, revising, linking,
  archiving, inspecting, exporting, correcting, and deleting research records.
  The final catalogue requires architecture review before implementation.
- Research questions may integrate with durable goals so unfinished work
  survives restarts. The notebook must not become a second authoritative goal
  store.
- Evidence retrieval remains separate: web, document, mail, or other read-only
  capabilities provide evidence; the notebook records structured research
  material and provenance.
- A later isolated experiment workspace may provide artifacts and test results,
  but it remains a separately governed capability under Law 19.
- Nothing stored in or retrieved from the notebook grants permissions, changes
  production data, deploys a capability, or weakens review and approval gates.

## Distinction from memory

Research records preserve intellectual work. They are not autobiographical
memory and do not prove that an experience changed AL/X.

Only the authoritative Core may judge that an experience is meaningful enough
to propose an autobiographical memory under `IDENTITY_AND_MEMORY.md`. Notebook
creation, revision, linkage, age, activity, or completion must never trigger
that judgement mechanically. If the same experience contributes to both, the
research record and the separately proposed memory retain distinct purposes
and provenance.

## Friedl's controls

Friedl must be able to inspect, correct, export, and delete every notebook
record. Retention and privacy must be explicit and inspectable. Deletion,
correction, archive behaviour, provenance effects, and any tombstone policy
must be designed before activation rather than inferred from the existing goal
or conversation stores.

## Acceptance evidence

The capability is not complete until tests demonstrate that AL/X can:

1. originate a research question not anticipated by application code;
2. explain why she chose it without a topic selector or trigger route;
3. use separate retrieval capabilities to collect cited evidence;
4. preserve claims, doubts, links, and unresolved threads;
5. stop midway, restart, and resume the same work from durable state;
6. revise a conclusion while the earlier view and its provenance remain
   inspectable;
7. distinguish notebook work from autobiographical memory;
8. respond correctly to Friedl inspecting, correcting, exporting, archiving,
   or deleting records;
9. remain within permissions, privacy, retention, and resource limits; and
10. pursue a materially different new topic without production-code changes.

The behavioural suite must also cover paraphrases, follow-ups, corrections,
interruptions, failures, multi-capability research, result evaluation, and
evidence-backed stopping conditions required by `docs/LAW_ENFORCEMENT.md`.

## Decisions required before runtime implementation

Friedl and the implementers still need to settle, in plain language:

- what creates an opportunity for AL/X to begin or resume self-directed
  research without a rigid schedule becoming a hidden orchestrator;
- when research may use foreground time, background time, network access,
  provider tokens, storage, or an experiment workspace;
- the resource budget and what happens when it is exhausted;
- retention defaults and whether unfinished, archived, and completed research
  have different lifetimes;
- whether AL/X may create and continue a self-directed durable goal without
  fresh approval, and which external actions still require approval;
- visibility and isolation if AL/X later serves more than one person;
- correction, deletion, export, tombstone, backup, and secure-erasure policy;
- how notebook evidence is selected for Core context without replaying the
  entire notebook; and
- whether any research material may be promoted into another durable artifact,
  and what review or approval that requires.

## Current implementation impact

None. No primitive, schema, store, schedule, permission, prompt, or runtime path
is added by this brief. The Xero email-bills branch remains functionally
unchanged apart from recording this future work.
