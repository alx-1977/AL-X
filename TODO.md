# AL/X To-Do

Status ledger for AL/X. Reorganized 2026-09-15 to separate proven capability
from MVP, known problems, and plans. Evidence: code inspection at HEAD
`795b23e`.

## Working

Implemented and proven in real use.

- [x] **Supplier bill processing / Xero supplier invoices** — invoice
  extraction and validation, bill creation, original document attachment,
  and duplicate/TOCTOU protections (`src/alx/tools/xero.py`,
  `tests/test_xero_bill_primitives.py`). Resumes an existing DRAFT by
  invoice number + contact instead of duplicating, compares full draft
  content before reuse, verifies attachments byte-for-byte by SHA256 before
  and after authorisation. Actively hardened via #29/#30.
- [x] **DHL document processing** — classification of customs worksheet vs.
  duty-tax-paid invoice vs. freight (`src/alx/providers/dhl.py`,
  `tools/dhl.py`); attachment verification via Xero's own `AttachmentID`
  rather than trusting the upload response (#26). DDP/customs evidence
  documents explicitly stay out of the payable-bill path — only a
  reconciled duty-tax-paid invoice posts; freight returns unposted.
  Fixture-backed tests in `tests/test_dhl_reconciliation.py`.
- [x] **Web search** — Brave-backed search wired into a capability
  (`ASK_WEB_SEARCH`, `providers/web_search.py`, `BraveWebSearchProvider`),
  fails closed if unusable. Live-tested per Friedl.

## Partial / MVP

Work in meaningful ways but not complete enough to call finished.

- [ ] **Email** — mailbox status/search/read, current-message content
  extraction, and thread reconstruction are implemented and registered
  under `MAIL_READ_PERMISSION` (not approval-gated) and working.
  Important/unread announcement behaviour exists. Write actions
  (`SEND_MAIL_REPLY`, mark-seen, move-to-trash, file-processed) are
  implemented (`icloud_mail_send.py`) and approval-gated per
  D-011/D-014/D-015 — an intentional policy choice, not a gap. Partial
  because the complete conversational email-management workflow (read,
  triage, approval-gated write, in normal day-to-day use) still needs
  proving in regular use.
- [ ] **Coding Agent** — bounded coding jobs, plan/execution split, code
  edits, tests/gates, local review loop, and bounded Git commit authority
  (D-029) are implemented (`providers/coding_agent.py`,
  `providers/coding_git.py`). Grok subscription path proven live end to end
  (#23, #24, #33). Still MVP: orchestration (see Needs Rework below) needs
  substantial work before this runs unsupervised.

## Needs Rework / Known Problems

Existing functionality whose architecture or behaviour is known to be
inadequate.

- [ ] **Diary / self-directed research** is not working as intended.
  Storage and tooling exist — `tools/notebook.py`, `bootstrap/notebook.py`,
  `contracts/notebook.py`, four test files, and a live
  `research-notebook.sqlite3` — but `docs/PERSISTENT_RESEARCH_NOTEBOOK_BRIEF.md`
  still reads as pre-implementation and has not been reconciled with the
  code. The intended end-to-end autonomous behaviour has not been
  demonstrated. Track verification of the full chain explicitly:
  trigger → self-directed pursuit → diary/notebook entry →
  later retrieval/resumption. Not confirmed whether the notebook is
  actually invoked from Core's main reasoning loop or only registered as
  available.
- [ ] **Coding/autonomous-work orchestration** needs:
  1. Progress visibility during long-running jobs (partial today via
     `_report_activity`; not confirmed sufficient for a human watching live).
  2. Immediate Pause / Stop-after-current-job control that does not depend
     on waiting for normal Core conversation processing. **Not found in the
     codebase** — confirmed absent, not just unconfirmed.
  3. Goal/step-level retry and provider-spend limits, not merely per-job
     planning limits.
  4. Explicit PLAN vs EXECUTION runtime telemetry, including start/end
     events.
  5. Bounded GitHub workflow authority so verified work can be pushed to
     branches, PRs opened/updated, checks/reviews read, and Qodo requested
     without manual Git intervention. No automatic merge authority.
  6. Coding execution must occur in isolated job worktrees rather than
     risking edits in the live main repository.
  7. Simplify the coding reasoning loop so Core does not micromanage CA/
     reviewer work. Core should provide scope/authority once and re-enter
     only for genuine judgment, blockers, authority escalation, exhausted
     budgets, or completion.
  8. Reassess review layering. Coding Agent + Core + internal reviewer +
     external Qodo currently all participate; review should become
     risk-based so multiple expensive reasoning models are not all
     commenting on every routine change.
  9. Background/asynchronous coding jobs so Core remains available
      conversationally while CA work continues.
  10. Usage-aware scheduling/budgeting so AL/X can avoid beginning work that
      cannot be completed with available provider capacity.

## Planned

Not yet usable.

- [ ] Calendar / scheduling.
- [ ] Altium / Nexar ECAD integration.
- [ ] JLC/BOM matching and sourcing assistance.
- [ ] Particle/device tooling as a general AL/X capability.
- [ ] Courier scheduling/tracking beyond current DHL document processing.
- [ ] Product conceptualization/planning/R&D tooling.
- [ ] Future hardware interfaces.
- [ ] Completed self-directed Diary system (see Needs Rework above for
  current state).
- [ ] Activate scheduled retention deletion for expired mail-derived
  content. Before activation, produce and review the required dry-run
  inventory, record Friedl's authorisation for the first purge, preserve
  content-free tombstones, and verify failure reporting, restart behaviour,
  and backup scope. This is deletion from AL/X's designated durable stores,
  not deletion from iCloud.
- [ ] Revisit posting every supplier bill to one Cost of Sales account.
  Friedl chose this deliberately so capture works unattended: the account
  is a policy choice no invoice contains, so history-based rules fail on
  suppliers whose work varies and a model asked to pick one guesses. VAT is
  unaffected, but the P&L loses expense-category detail. A candidate for
  AL/X's own sandbox capability invention once enough real bills exist to
  learn from. Not a blocker; revisit before year-end.
- [ ] Reasoning ceiling gaps:
  - The budget window and its recovery state live only in memory, so a
    restart mid-bill loses the ceiling until the next bill capability is
    reached. Recorded rather than fixed: it cannot post a wrong bill, but
    it weakens the guardrail across a restart.
  - Recovery state is in memory with the window, so a restart
    mid-recovery drops the task back to an unbudgeted conversation rather
    than resuming the remaining allowance. Same root cause as above; it
    fails open on the ceiling, never on the deadlock.
  - Mail search and read calls made before the first Xero capability fall
    outside the ceiling, which is how two bills reached twelve Core calls.
    The ceiling covers the bill, not the work of finding it.
- [ ] Conversation-search primitive: older casual conversation outside the
  reasoning window and not cited by an active goal has no retrieval path
  today. AL/X will not know she is missing it. Worth building once real
  behaviour shows it matters.
- [ ] Token-based (not fixed-count) reasoning window measure — twelve short
  turns and twelve long ones currently cost very differently.
- [ ] ALX_SPECIALIST_EFFORT has no effect on xAI: that transport accepts no
  reasoning-effort parameter. Non-default values are logged as ignored, and
  medium passes silently because it is the effective default. The setting
  only changes behaviour on OpenAI.
- [ ] iCloud mail reconciliation (raised 2026-09-03, deferred). AL/X only
  learns a message is handled when she handles it; mail Friedl clears
  himself in iCloud stays observed on her side, so the two views diverge,
  wider the longer the runtime is down. Not a bug in D-014's one-at-a-time
  attention, which is deliberate. Open question: whether server-side state
  (Seen, moved, deleted) should reconcile into her observations — needs its
  own design decision, not a patch.

## Recently Completed

Kept visible for a while even though done.

- [x] PR #34 — Surface local-review failures as `review_failed` and
  `local_review_material_findings` (merged).
- [x] PR #32 — Stop a failing activity sink from destroying a valid coding
  outcome; correct retry/cache behaviour (merged).
- [x] PR #33 — Extract a provider-neutral subscription coding-session base
  (merged).
- [x] PR #31 — Give the Coding Agent bounded Git workspace authority
  (D-029) (merged).
- [x] D-029 amendment — deterministically create the first available repair
  branch suffix (`-2`, `-3`, …) without touching existing branches.
- [x] Dirty-path progress-evidence issue investigated and confirmed already
  correct; no code change required.
- Current `main` after the above merges: `795b23e`.

**Control-test resource baseline** (observed benchmark, not a permanent
cost estimate):

- Approximately the entire Claude 5-hour allowance consumed.
- Approximately 11% of the weekly OpenAI allowance consumed, despite the
  reviewer using Luna.
- Approximately $8 of Grok extra-usage credit.
- Total autonomous run roughly around an hour.
- Significant waste came from repeated Core↔CA reasoning and avoidable job
  retries such as branch-name collisions.

**Architectural conclusion from this run:** Core should not be the Coding
Agent's supervisor on every implementation step. Desired normal shape:

> Core scopes/authorizes once → CA owns implementation/test loop →
> deterministic gates → reviewer as appropriate → publish/review

Core re-enters only when real judgment is required. This motivates the
orchestration items under Needs Rework above.

## Architecture / Infrastructure

Cross-cutting foundations, not user-facing tools themselves.

- [x] Voice conversation (`interfaces/live_voice.py`,
  `bootstrap/live_voice.py`).
- [x] Durable goals (`goals/store.py`).
- [x] Memory (`memories/store.py`).
- [x] Provider abstraction (`providers/` — many provider implementations
  behind a common interface).
- [x] Approval/authority model (`safety/gate.py`, `AuthorityPolicy`).
- [x] Governance/law gates (`scripts/check_architecture.py`,
  `scripts/check_governance.py`, `governance/DECISIONS.md`).
- [x] TTS vocabulary/pronunciation preprocessing
  (`config/pronunciation/`, `providers/elevenlabs_pronunciation.py`,
  `docs/PRONUNCIATION_ACCEPTANCE.md`).
