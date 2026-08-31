# Proposal: provenance-based mail retention

**Status:** Policy approved as D-013 by Friedl on 2026-08-30. Provenance is
wired through the schemas and authoritative write paths for new records.
Scheduled deletion and a first purge are **not** authorised or implemented.
**Supersedes:** the transient-content similarity guard, which is abandoned.

## Why the previous approach failed

The guard tried to detect whether durable text *resembled* a mail body, using
string similarity. Six versions were attempted. Each was either defeated by
fragmenting the text or refused ordinary summaries, and twice I claimed the
boundary was closed when it was not.

The cause is not tuning. A faithful summary of a short message legitimately
shares long runs with it, while a leak can be split into single characters. No
threshold admits the first and rejects the second.

## What is actually true today, verified

- `purge_expired` exists in the goal, conversation, and memory stores, and is
  covered by tests, but **no runtime code calls it**. Only tests do.
- Memory retrieval **does** exclude expired records (`retention_until > as_of`),
  so retention is not entirely decorative. Physical deletion never happens.
- Goals and conversations load expired records normally.
- Conversation retention is **one timestamp for the whole conversation**,
  renewed whenever any turn is appended. In an active conversation, old mail
  turns effectively never expire.
- Deleting a turn today would break references: evidence citing `turn:<id>`
  fails `evidence_source_unknown`, and memory fails `source_reference_unknown`.

That last point is why tombstones are a correctness requirement, not a nicety.

## The honest guarantee, and its exact scope

An earlier draft said bodies are "never written" and later admitted the Core
may copy one into a goal. Both cannot be true. The guarantee is:

> **A provider-supplied raw body is never automatically written to AL/X's**
> **designated durable application records: the goal, conversation, and memory**
> **stores.**
> **Model-derived copies are bounded by provenance retention, not prevented.**

"Never persisted anywhere" would be a broader claim than this design can
support. It says nothing about:

- what the model provider retains of a request containing the body;
- application logs, exception traces, or diagnostics;
- temporary files or process memory;
- SQLite write-ahead logs and freed pages, where deleted bytes can survive;
- any backup of those files.

Each is a separate surface. Before activation this design must state, for each,
whether it is in scope and what happens there. Claiming otherwise would repeat
the mistake of the similarity guard: asserting a boundary wider than the code
actually holds.

If Friedl wants model-derived copies prevented outright rather than bounded,
that is a different and harder decision, and this proposal does not deliver it.

## Design

### 1. Provenance is mechanical and transitive

Provenance is the union of the provenance carried by every input to a record:
the events, tool results, prior goal revisions, referenced artifacts, and
conversation turns in scope. It is not merely whether mail content happened to
appear in the current model context, because that is lost across a restart or
behind an intermediate structured record.

This is propagation, not classification: no keyword, model judgement, or
content matcher decides it.

Over-tagging is deliberate. A record wrongly tagged expires earlier than it
needed to; a record wrongly untagged persists private content indefinitely.
The first is a nuisance, the second is the failure this replaces.

### 2. Retention is per record and per turn

Each conversation turn carries its own provenance and expiry, rather than one
timestamp for the conversation. Expiring a conversation wholesale would delete
unrelated context, and the current renew-on-append behaviour means mail turns
in an active conversation never expire at all.

### 3. Active goals never expire, but prose inside them does

An earlier draft said both that prose inside an active goal may expire and that
retention begins only at terminal state. Those conflict. They are two lifetimes,
not one:

- **The goal container and its structured reference state** — identifiers,
  participants, threading, attempts and outcomes — survive while the goal is
  active, as Law 1 requires. Retention on the container begins only when the
  goal becomes terminal.
- **Mail-derived prose within that goal** — objective wording, progress
  summaries, evidence attributes — expires on its own schedule regardless of
  whether the goal is still active, and is replaced by a tombstone.

An active goal therefore ages into a reference-only record: still workable,
because AL/X re-reads the message, but no longer carrying its content.

### 4. Expired content leaves a tombstone, which is not evidence

Removing a turn must not orphan the records that cite it. A tombstone retains
the identifier, timestamp, provenance, and deletion reason, and deletes the
content. References resolve; the content is gone.

Without this, purging breaks goal evidence and memory grounding, as verified
above.

A tombstone preserves **identity, not evidence**. Referential integrity is not
sufficiency: once the content is gone the citation no longer supports any claim
that rested on it. AL/X must treat a tombstoned source as *"this existed, its
content is unavailable"*, re-read the message where she can, and never complete
a goal or assert a conclusion resting solely on unavailable evidence. The
existing completion rule already requires sourced evidence for every success
criterion; that rule must count a tombstone as unsourced.

### 5. Purging is reliable and inspectable

- runs at startup and periodically;
- idempotent, so a repeated run is harmless;
- reports failures rather than skipping silently;
- can be asked what *would* expire, without deleting anything.

**"Physical deletion" needs a precise promise.** Deleting a SQLite row does not
erase the bytes: they can persist in freed pages, the write-ahead log, and any
backup. This design promises **logical inaccessibility** through the
application's own interfaces. Secure erasure of underlying storage, and the
expiry of backups, are separate commitments that must be stated explicitly
before activation rather than implied by the word "deleted".

A purge that silently fails is worse than none, because the retention would
then be believed rather than merely absent.

## Retention periods

Friedl's proposed defaults, recorded for approval:

| Category | Retention |
| --- | --- |
| Provider-supplied raw body | never automatically stored; re-read by reference |
| Mail-derived conversation turn | 30 days, per turn |
| Draft and approval wording | 10 minutes, or immediately on send, refusal, or abandonment |
| Active mail goal | no expiry; reference and structured state only |
| Terminal mail goal | 90 days after completion or cancellation |
| Non-content audit facts | normal system retention |
| Mail-derived memory | outlives its source only with Friedl's explicit approval, showing exactly what would be retained; thereafter normal memory policy, inspectable and deletable |

## Restart and continuity

Required evidence before this is accepted:

- an unfinished mail goal survives a restart and AL/X resumes it by re-reading
  the referenced message;
- she resumes without the body having been written to the designated durable
  stores named in the scope below;
- expiry of the conversational text does not orphan the goal;
- a goal whose message was deleted remains resumable, with the message reported
  unavailable rather than invented;
- a tombstoned turn still resolves the references that cite it, while no
  longer satisfying their evidentiary claim.

## Scope: where mail content can reach

Every surface below was inspected on this machine. "In scope" means this design
commits to a boundary there; "out of scope" means it does not, and says so
rather than implying protection it has not built.

### Model provider

- **Does mail content reach it?** Yes. The body is sent in the reasoning
  request so the Core can reason about it. This is unavoidable while a model
  does the reasoning.
- **Current behaviour:** no retention or zero-data-retention flag is sent. What
  OpenAI keeps is governed by their account defaults, which AL/X neither sets
  nor observes.
- **Proposed boundary:** send an explicit retention preference where the
  provider supports one, and record in configuration which provider setting is
  relied upon. AL/X cannot delete provider-side data, so this is a
  configuration and disclosure commitment, not a deletion one.
- **Deletion meaning:** neither. Out of AL/X's control.
- **Configured and tested:** the request payload is asserted to carry the
  configured retention preference. Provider-side behaviour cannot be tested
  from here and must not be claimed.

### Application logs and exceptions

- **Does mail content reach it?** Not through logging. Every inspected log
  statement carries codes, identifiers, and durations. Provider failures are
  reduced to an exception type or error code before being logged.
- **But:** a provider exception is raised `from error`, chaining the underlying
  httpx exception, which retains the request object. Verified: the body is
  reachable as `__cause__.request.content`, and a traceback formatted with
  `capture_locals=True` prints it.
  **Correction:** I previously wrote that any formatted traceback exposes the
  body. That was wrong, and came from a test whose own source line contained
  the literal, which tracebacks print. An ordinary `format_exception` does not
  expose it, and normal AL/X execution catches the failure and logs only a
  sanitised code. The defect is that the object retains the content, so richer
  diagnostics or an error-reporting integration would surface it.
  **Second correction:** I then overclaimed in the other direction, writing
  that breaking the chain leaves "no retained frame holding the payload". That
  is false. Severing `__cause__` and `__context__` cleans the exception
  *object*, but a traceback built with `capture_locals=True` walks every frame
  in the stack, and any frame that was processing a mail body still holds it.
  Clearing the adapter's own locals would not close this: the calling Core
  frame holds the same content independently, as a test now demonstrates.
  Diagnostics that capture locals cannot be made safe at the provider boundary
  for a stack that is, by design, processing private material.
- **Proposed boundary,** stated as four separate promises rather than one:
  1. **Guaranteed:** the exception object retains no provider request. No
     `__cause__`, no `__context__`, no reachable `request.content`.
  2. **Guaranteed:** ordinary diagnostics stay clean. `format_exception` and
     anything AL/X logs render only a provider name and an error code.
  3. **Enforced by the architecture gate.** AL/X produces no tracebacks and
     calls no error-reporting sink. `scripts/check_architecture.py` parses the
     source and rejects traceback rendering and frame extraction, `exc_info`,
     `stack_info` and `capture_locals`, `logger.exception`, assignment to
     `sys.excepthook`, and sinks such as `capture_exception` and
     `record_exception`. Gate tests prove each route is rejected. The gate
     matches names, not meaning: a sink named `report_failure(error)` would
     pass it, so this raises the cost of adding one accidentally rather than
     making it impossible.
  4. **Prohibited by rule, not prevented by code:** exporting a
     locals-capturing traceback from a payload-carrying path. Nothing can stop
     an operator running a debugger. Adding an error-reporting or observability
     integration that receives exception state requires separate privacy review
     and Friedl's approval.

  This boundary is recorded as governance decision **D-012**, which governs
  every private payload AL/X processes, not mail alone.
- **Deletion meaning:** logs rotate by the operator's arrangement; AL/X does
  not manage them.
- **Configured and tested:** tests assert the first three promises, and two
  further tests deliberately prove the frame-locals exposure is real, so the
  limit is recorded in the suite rather than left implicit.

### Temporary and process storage

- **Does mail content reach it?** In process memory while a turn runs, which is
  unavoidable. No temporary files are written by the mail path.
- **Proposed boundary:** no temporary file may carry a body. Backups made
  during maintenance are covered separately below.
- **Deletion meaning:** process memory is reclaimed by the runtime; no promise
  of erasure is made.
- **Configured and tested:** a test asserts the mail path writes no file
  outside the designated stores.

### SQLite databases

- **Does mail content reach it?** The observation store holds references only,
  verified: its records contain `mailbox_id`, `uid_validity`, and `uid`. The
  conversation, goal, and memory stores can hold model-derived text about a
  message, which is what provenance retention will bound.
- **Current behaviour:** journal mode is `delete`, so no `.wal` files persist;
  rollback journals exist transiently during writes. `secure_delete` is off.
- **Verified:** after `DELETE` and commit, the deleted bytes were still present
  in the database file.
- **Proposed boundary:** the designated stores are in scope for provenance
  retention. Deleted rows become logically inaccessible.
- **Deletion meaning:** **logical inaccessibility, not secure erasure.**
  Enabling `secure_delete` and running `VACUUM` after a purge would narrow this
  and both cost time; whether to pay that is Friedl's decision.
- **Configured and tested:** a test asserts a purged record is unreachable
  through every store interface. A test asserting byte-level absence would be
  claiming erasure this design does not promise.

### Backups

- **Does mail content reach it?** Not today, but nothing prevents it tomorrow.
  Four `.bak` files exist under `.alx/runtime/backup/`, created during this
  session's maintenance, and all four sit outside any retention policy.
  Inventoried by schema and field length, without reading any body:
  - Three are copies of the mail observation store. Each observation carries
    `mailbox_id`, `uid_validity`, `uid`, `message_id`, `sender`, `subject`,
    `received_at`, `observed_at` — a reference and its envelope, no body. The
    longest field in any of them is 98 characters.
  - One is a copy of the goal store. Its conversation turns hold identifiers
    and codes only, longest 90 characters. Its goal states are **unclassified
    free text**: objectives, progress notes, decisions and corrections, the
    longest being 135 characters.

  What this inventory does and does not establish. It shows the observation
  store has no body column and cannot hold one, so those three files are
  structurally body-free. It does **not** establish the same for the goal
  store. A 135-character progress note is free text, and a short quotation
  from a mail body would fit inside it comfortably. Length and schema cannot
  tell a written summary from a copied fragment — the same limit that defeated
  the similarity guard. The goal backup therefore remains unclassified; AL/X
  will not infer provenance by comparing its contents against retained mail.
- **Proposed boundary:** backups inherit the retention of what they copy. A
  backup older than the longest retention it contains is itself expired, and
  maintenance copies are either recorded for expiry or not taken.
- **Deletion meaning:** logical inaccessibility, same as the stores.
- **Configured and tested:** the dry-run inventory lists backup files and their
  ages alongside the records they would contain.

### Exports and other integrations

- **Does mail content reach it?** No export path exists today. Speech synthesis
  receives AL/X's response, which may describe a message; ElevenLabs retention
  is governed by their account settings, as with the reasoning provider.
- **Proposed boundary:** any future export must declare its provenance handling
  before it is built. The synthesis provider is disclosed, not controlled.
- **Deletion meaning:** out of AL/X's control.
- **Configured and tested:** a test asserts no capability writes mail content
  to a path outside the designated stores.

## Deletion and unavailable messages

- **Friedl deletes the message.** On re-read AL/X reports it unavailable and
  says so plainly. She does not reconstruct it from her earlier description.
- **The message moves.** `UIDVALIDITY` or the identifier changes; the existing
  `identifier_stale` failure applies and must not be retried against a
  different message.
- **Conversational text expired, goal remains.** She re-reads for detail, or
  reports that she cannot.
- **Memory outlives the message.** Only where Friedl approved the promotion.

## Migration of content already retained

Nothing is deleted yet. Before any first purge:

1. Produce a **dry-run inventory** of every content-bearing record and backup.
   Legacy records remain explicitly unclassified: provenance cannot be
   reconstructed reliably from their wording.
2. Friedl decides whether those legacy records remain, receive an explicit
   owner-supplied classification, or expire as a whole cohort. AL/X does not
   infer that decision by content comparison.
3. Friedl reviews the resulting purge preview.
4. Only then is a first purge authorised, and only with his confirmation.

## Trade-offs, stated plainly

**In favour**

- Addresses the cause rather than detecting the symptom.
- No false positives: AL/X may summarise, draft, and describe freely.
- Makes retention real. Today only memory retrieval honours it.
- Removes a guard that was wrong six times.

**Against**

- **AL/X will forget things.** A conversation from a month ago about an email
  will be gone. Intended, and a real loss.
- **Re-reading can fail.** A message deleted since is unrecoverable where a
  stored copy would have survived. A deliberate privacy-over-continuity choice.
- **More moving parts.** Provenance propagation, per-turn expiry, tombstones,
  and scheduled deletion are each capable of being wrong.
- **Over-tagging expires innocent context early.** Accepted deliberately.
- **It bounds rather than prevents** model-derived copies.

## What is not proposed

- No similarity matching in any form.
- No classification of messages by sender, subject, or content.
- No automatic promotion to long-term memory.
- No change to the read, reply, acknowledge, or trash capabilities.

## Decisions still required before activation

Friedl approved D-013, the schema migration, and write-path wiring. Still
outstanding:

1. **Authorisation for scheduled deletion**, conditional on the revised design,
   preview controls, failure reporting, tombstones, and tests existing first.
   Automatic destruction of durable state is a stronger act than the current
   durable-context record describes, and warrants its own decision record.
2. **Confirmation of the first purge**, after reviewing the dry-run inventory.

I would not proceed on any of these without a recorded decision.

## Findings resolved separately from retention

1. **The exception chain retained provider requests.** This was fixed
   separately and is governed by D-012; ordinary diagnostics and AL/X's logs
   expose sanitised provider/error codes only. Locals-capturing diagnostics
   remain prohibited rather than made safe.
2. **Four backup files exist outside any policy**, created during
   maintenance in this session. Three copy the observation store, which has no
   body column and so cannot hold one. The fourth copies the goal store, whose
   free-text fields are **unclassified**: short enough to be summaries, long
   enough to hold a quoted fragment, and not separable by inspection. All four
   are listed in the dry-run inventory. The goal backup must remain
   unclassified unless Friedl supplies a classification or chooses a policy for
   the whole legacy cohort; content comparison cannot establish provenance.

## Decisions this scope adds

Beyond the two already outstanding:

3. **Whether to pay for secure erasure.** Enabling `secure_delete` and running
   `VACUUM` after a purge narrows deletion from logical inaccessibility toward
   erasure, at a cost in time on every write and every purge. Verified today:
   deleted bytes remain in the database file without it.
4. **What is relied upon at the model provider.** AL/X cannot delete anything
   there. The commitment is to send a retention preference and record which
   provider setting is being trusted, which is disclosure rather than control.

## Current state, recorded honestly

The similarity guard has been removed from the runtime. Its last committed
version blocked ordinary summaries of a short message, so it was preventing
legitimate work rather than protecting anything.

Provenance now flows mechanically from mail events and read results into new
conversation turns, goal revisions, and memory revisions. Their independent
thirty-day deadline survives restart and cannot be renewed by summarising or
rewriting the record. Legacy rows remain unclassified rather than guessed.

No scheduled purge enforces those deadlines yet. Content therefore remains
physically present and application-reachable after its recorded deadline until
deletion is separately authorised and implemented. This remaining gap is
recorded and tested rather than described as completed retention.
