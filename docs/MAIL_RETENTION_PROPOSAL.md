# Proposal: provenance-based mail retention

**Status:** Proposal for Friedl's decision. No code implements this.
**Supersedes:** the transient-content similarity guard, which is abandoned.

## Why the current approach failed

The guard tried to detect whether durable text *resembled* a mail body, using
string similarity. It was attempted six times. Every version was either
defeated by fragmenting the text, or refused ordinary summaries. Both failures
were found by review, twice after I had claimed the boundary was closed.

The reason is not a tuning error. A faithful one-line summary of a short
message legitimately shares long runs with it, while a leak can be split into
single characters. No threshold admits the first and rejects the second.
Similarity cannot distinguish *describing* from *copying*.

Two facts make the guard beside the point anyway:

- **`retention_until` is recorded but never enforced.** No scheduled deletion
  exists in any store. Nothing has ever expired.
- **The conversation store already holds mail content.** Of 60 retained AL/X
  responses on this machine, 45 mention mail; they are kept indefinitely and
  the guard never applied to them.

So the guard policed one door while the content walked through another, into
storage with no expiry.

## The proposal in one sentence

Stop trying to detect mail content, and instead give everything derived from
mail a recorded provenance and an explicit lifetime, so it expires on its own
unless Friedl decides otherwise.

## What changes

### 1. Raw bodies stay transient and are re-read on demand

A body reaches the Core to be reasoned about and is never written to a store.
When AL/X needs it again she re-reads it by `MailReference`, exactly as she does
today. This is already true of the read path; the proposal keeps it and stops
building around it.

**Consequence:** if the message is gone, the body is gone. That is the point.

### 2. Long-lived goals carry references and structured state, not prose

A goal about a message keeps the `MailReference`, the participants, the
threading identifiers, the actions attempted and their outcomes. It does not
keep a free-text retelling of the message as its objective or progress.

AL/X may still describe the message in conversation. The distinction is between
what she *says* and what the goal *stores* as its durable record.

### 3. Everything mail-derived carries provenance and a lifetime

Each record that exists because of a message is marked as such and given a
retention period, configurable and separate from ordinary goal retention:

| What | Suggested lifetime | Why |
| --- | --- | --- |
| Raw body | never stored | re-read on demand |
| Conversation turns mentioning a message | short, days | AL/X's own words, but about private content |
| Draft reply artifact | until sent, refused, or abandoned | it exists only to be approved |
| Approval record scope | until consumed or expired | already ten minutes by D-011 |
| Goal referencing a message | medium, weeks | the work outlives the message |
| Memory formed from a message | **only with Friedl's approval** | this is the one that should outlive |

The numbers are placeholders. Choosing them is Friedl's decision, not mine.

### 4. Draft and approval artifacts expire on use

An approved send binds to exact arguments including the body. Once consumed,
refused, or expired, that exact artifact is deleted rather than left in goal
state. The evidence that a send happened is kept: identifiers, recipients,
timestamp, outcome. The wording is not.

### 5. Long-term promotion is an explicit decision

Anything mail-derived that AL/X judges worth keeping beyond its lifetime is
proposed to Friedl and kept only if he agrees. Nothing promotes itself, and no
threshold or score decides it. This is the Law 15 inspection and correction
control made real rather than nominal.

## Restart and continuity

The concern with removing stored bodies is that AL/X forgets what she was doing.
She does not, provided she can re-read.

Required evidence before this is accepted:

- an unfinished mail goal survives a restart and AL/X resumes it by re-reading
  the referenced message;
- she resumes without the body having been stored anywhere;
- expiry of the conversational text does not orphan the goal;
- a goal whose message has been deleted is resumable as a goal, with the message
  reported as unavailable rather than invented.

## Deletion and unavailable messages

These must be explicit, because they will happen routinely.

- **Friedl deletes the message.** The reference remains valid until the mailbox
  is re-read. On re-read, AL/X reports it as unavailable and says so plainly.
  She does not reconstruct it from memory or from her earlier description.
- **The message is moved.** `UIDVALIDITY` or the identifier changes. The
  existing `identifier_stale` failure already covers this and must not be
  silently retried against a different message.
- **Retention removes the conversational text but the goal remains.** She
  re-reads to recover detail, or reports that she cannot.
- **A memory outlives the message.** Permitted only where Friedl approved the
  promotion, and it must remain inspectable and deletable by him.

## Trade-offs, stated plainly

**In favour**

- Solves the actual problem. Content expires because it is mail-derived, not
  because a matcher recognised it.
- No false positives. AL/X can summarise, draft, and describe freely.
- Removes a guard I have got wrong six times.
- Makes `retention_until` mean something. Today it is decorative.

**Against**

- **AL/X will forget things.** A conversation from three weeks ago about an
  email may be gone. That is the intended behaviour and it is a real loss.
- **Re-reading costs time and can fail.** A message deleted since is
  unrecoverable, where a stored copy would have survived. This is a deliberate
  privacy-over-continuity choice.
- **More moving parts than a guard.** Provenance marking, scheduled deletion,
  and a promotion path are more code than a matcher, and each can be wrong.
- **Deletion must be reliable.** A purge that silently fails is worse than
  today, because the retention would then be believed rather than merely absent.
- **It does not stop the Core writing a body into a goal.** It bounds how long
  that survives. If Friedl wants that prevented outright, that is a different
  and harder decision.

## What is not proposed

- No similarity matching, in any form.
- No classification of messages by sender, subject, or content.
- No automatic promotion of anything to long-term memory.
- No change to the read, reply, acknowledge, or trash capabilities.

## Decisions that require Friedl's approval before any code

1. **Adopt this approach** in place of the similarity guard, accepting that a
   body written into a goal is time-bounded rather than prevented.
2. **The retention periods** for each category above. These determine how much
   AL/X forgets and when.
3. **Whether memory may outlive a message at all**, and if so on what approval.
4. **Whether scheduled deletion is authorised**, since it destroys durable state
   automatically. Law 15 requires inspection, correction, and deletion controls;
   automatic deletion is a stronger act than the current record describes.
5. **What happens to existing retained content**, including the 45 stored
   responses on this machine that mention mail. Leaving them, expiring them, or
   deleting them now are three different decisions.

Items 1, 4, and 5 are the ones I would not proceed on without a recorded
decision. Items 2 and 3 are product judgements where I can propose values but
should not choose them.
