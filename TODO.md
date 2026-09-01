# AL/X To-Do

## Retention

- [ ] Activate scheduled retention deletion for expired mail-derived content.
  Before activation, produce and review the required dry-run inventory, record
  Friedl's authorisation for the first purge, preserve content-free tombstones,
  and verify failure reporting, restart behaviour, and backup scope. This is
  deletion from AL/X's designated durable stores, not deletion from iCloud.

## AL/X research notebook

- [ ] Design and build the persistent research notebook AL/X requested for
  self-directed curiosity. Preserve her requirements and the unresolved product
  decisions in `docs/PERSISTENT_RESEARCH_NOTEBOOK_BRIEF.md`; approve the final
  retention, authority, privacy, resource, and resumption boundaries before
  runtime implementation.

## Expense account coding

- [ ] Revisit posting every supplier bill to one Cost of Sales account.
  Friedl chose this deliberately so capture works unattended: the account is a
  policy choice no invoice contains, so history-based rules fail on suppliers
  whose work varies and a model asked to pick one guesses. VAT is unaffected,
  but the P&L loses expense-category detail.
  A candidate for AL/X's own sandbox capability invention once enough real bills
  exist to learn from. Not a blocker; revisit before year-end.

## Reasoning ceiling gaps

- [ ] The budget window and its recovery state live only in memory, so a
  restart mid-bill loses the ceiling until the next bill capability is
  reached. Recorded rather than fixed: it cannot post a wrong bill, but it
  weakens the guardrail across a restart.
- [ ] Recovery state is in memory with the window, so a restart mid-recovery
  drops the task back to an unbudgeted conversation rather than resuming the
  remaining allowance. Same root cause as the item above; it fails open on the
  ceiling, never on the deadlock.
- [ ] Mail search and read calls made before the first Xero capability fall
  outside the ceiling, which is how two bills reached twelve Core calls. The
  ceiling covers the bill, not the work of finding it.

## Conversation context growth

Fixed by a deterministic reasoning projection: the Core sends the last
`REASONING_TURN_WINDOW` turns plus every older turn the active goal still
cites. The complete conversation stays stored and unrewritten, and grounding,
provenance and retention still validate against all of it.

- [ ] Older casual conversation is no longer silently present in the model's
  context. A turn outside the window that nothing cites has to be retrieved,
  and there is no retrieval capability for the conversation thread yet — only
  durable memories are retrieved. AL/X will not know she is missing it, so she
  may answer from the window alone rather than saying she needs to look. Worth
  a conversation-search primitive once real behaviour shows it matters.
- [ ] The window is a fixed count, not a token measure. Twelve short turns and
  twelve long ones cost very differently, so this bounds growth without
  bounding size.

## Provider limitations

- [ ] ALX_SPECIALIST_EFFORT has no effect on xAI: that transport accepts no
  reasoning-effort parameter. Non-default values are logged as ignored, and
  medium passes silently because it is the effective default. The setting only
  changes behaviour on OpenAI.
