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
  A candidate for AL/X's own sandbox work under Law 19 once enough real bills
  exist to learn from. Not a blocker; revisit before year-end.

## Reasoning ceiling gaps

- [ ] The budget window and its recovery state live only in memory, so a
  restart mid-bill loses the ceiling until the next bill capability is
  reached. Recorded rather than fixed: it cannot post a wrong bill, but it
  weakens the guardrail across a restart.
- [ ] Mail search and read calls made before the first Xero capability fall
  outside the ceiling, which is how two bills reached twelve Core calls. The
  ceiling covers the bill, not the work of finding it.

## Provider limitations

- [ ] ALX_SPECIALIST_EFFORT has no effect on xAI: that transport accepts no
  reasoning-effort parameter. Non-default values are logged as ignored, and
  medium passes silently because it is the effective default. The setting only
  changes behaviour on OpenAI.
