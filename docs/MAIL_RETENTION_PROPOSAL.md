# Proposal: provenance-based mail retention

**Status:** Direction approved by Friedl on 2026-08-30. Implementation and
deletion are **not** authorised. This is the revised design for his decision.
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

## The honest guarantee

An earlier draft said bodies are "never written" and later admitted the Core
may copy one into a goal. Both cannot be true. The guarantee is:

> **Provider-supplied raw bodies are never automatically persisted.**
> **Model-derived copies are bounded by provenance retention, not prevented.**

If Friedl wants model-derived copies prevented outright rather than bounded,
that is a different and harder decision, and this proposal does not deliver it.

## Design

### 1. Provenance is mechanical, never judged

Any record produced during a reasoning turn whose context included
mail-derived content inherits mail provenance. This is propagation, not
classification: no keyword, model judgement, or content matcher decides it.

Over-tagging is deliberate. A record wrongly tagged expires earlier than it
needed to; a record wrongly untagged persists private content indefinitely.
The first is a nuisance, the second is the failure this replaces.

### 2. Retention is per record and per turn

Each conversation turn carries its own provenance and expiry, rather than one
timestamp for the conversation. Expiring a conversation wholesale would delete
unrelated context, and the current renew-on-append behaviour means mail turns
in an active conversation never expire at all.

### 3. Active goals never expire

Law 8 requires an unfinished goal to continue. An active mail goal keeps its
reference and structured work state until it completes, is cancelled, or is
genuinely blocked. Sensitive prose inside it may expire; the goal itself may
not silently disappear.

Retention begins when the goal becomes terminal.

### 4. Expired content leaves a tombstone

Removing a turn must not orphan the records that cite it. A tombstone retains
the identifier, timestamp, provenance, and deletion reason, and deletes the
content. References resolve; the content is gone.

Without this, purging breaks goal evidence and memory grounding, as verified
above.

### 5. Purging is reliable and inspectable

- runs at startup and periodically;
- idempotent, so a repeated run is harmless;
- reports failures rather than skipping silently;
- can be asked what *would* expire, without deleting anything.

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
- she resumes without the body having been persisted anywhere;
- expiry of the conversational text does not orphan the goal;
- a goal whose message was deleted remains resumable, with the message reported
  unavailable rather than invented;
- a tombstoned turn still satisfies the references that cite it.

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

1. Apply provenance retroactively to existing records.
2. Produce a **dry-run inventory** showing exactly what would be removed,
   including the retained AL/X responses that mention mail.
3. Friedl reviews that inventory.
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

## Decisions still required before code

Friedl has approved the direction and the defaults above. Still outstanding:

1. **Authorisation to implement.** Not yet given.
2. **Authorisation for scheduled deletion**, conditional on the revised design,
   preview controls, failure reporting, tombstones, and tests existing first.
   Automatic destruction of durable state is a stronger act than the current
   Law 15 record describes, and warrants its own decision record.
3. **Confirmation of the first purge**, after reviewing the dry-run inventory.

I would not proceed on any of these without a recorded decision.
