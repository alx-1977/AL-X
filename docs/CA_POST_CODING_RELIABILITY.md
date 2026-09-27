# Post-coding reliability change

Scope: Friedl's 2026-09-27 instruction to complete external review, merge
readiness, exact-head merge and canonical-main synchronization without repeated
Core calls for waiting or housekeeping. Implementation, local review,
verification ordering, coding retry allowances and checkpoint ancestry are
unchanged. This document records implementation evidence, not an amendment to
the laws or an approval of external review.

## Production path

- `request_external_review` retains its existing single-use approval. Its
  production callback records the exact requested PR/head in the existing task
  store and joins `TaskPoller`. Only `TaskPoller.run/tick` polls the reviewer.
  The dispatch waits on a condition; it does not run another polling loop.
  Polling has a 900-second window. Completion, failure, missing observation or
  timeout returns to the existing Core. No review content is judged by code.
- `read_external_review` retrieves the completed exact-head evidence for AL/X.
  Unavailable content is a concrete failure, not a successful pending result
  inviting another Core poll.
- AL/X reads the external evidence and chooses whether the exact head may
  merge. `merge_pull_request` remains the sole GitHub merge capability.
  `GitHubMergeProvider.merge` checks the current head, base ancestry,
  mergeability and required checks, with at most 30 observations separated by
  30 seconds. Pending checks stay inside that call. GitHub still enforces its
  protection rules and the exact SHA on the final PUT.
- A behind branch is rebased only when the clean canonical checkout and fetched
  source branch match the reviewed head. Existing repository operations do the
  work. The force-push lease is explicitly bound to that head, so an intervening
  fetch cannot widen it. The changed head returns `review_required`, including
  the new SHA and the need for fresh review approval. No review is purchased.
- After merge, the existing repository authority switches to main, fetches and
  fast-forwards it, proves it contains the merge commit, and verifies a clean
  checkout at the fetched revision. Dirty or changed local work is preserved.
  A failed sync reports that the remote merge already happened. Resuming the
  same merged PR can finish sync without another merge PUT.
- A settled review/merge blocker gives Core one decision to record/explain it.
  Goal proposals remain possible. Another capability dispatch in that turn is
  prevented. Pending review/CI states do not use this guard.

No new capability, agent, conversation route or workflow dispatcher is added.
Raw user language still enters Core alone. Review interpretation, conflict
resolution, approval decisions and the final response remain with AL/X.
Repository effects require both merge and repository-operation permissions.

## Superseded paths

Removed the immediate-return production review-request path and the successful
unavailable-review result used for Core-driven polling. Review capabilities are
not registered without their task waiter. Attached review completion returns to
its existing dispatch instead of also scheduling an autonomous completion turn.
The existing observer and exact-head review reader are reused, not copied.

Replaced opaque merge-refusal handling with bounded details and mechanical
readiness handling. Candidate `1833b2f` is not merged; its blanket
`terminal_for_turn` rule and fake lifecycle test are not used.

General repository primitives remain available for other authorised repository
work. They are reused here; neither local merge nor push substitutes for the
GitHub exact-head merge boundary.

## Proof and limits

`tests/test_ca_post_coding.py` exercises the real Core, capability registry,
broker, approval gate, review provider/observer/task runtime, merge provider and
repository authority. Git runs in temporary canonical checkouts against a bare
remote; no worktrees are used. Only model decisions, GitHub transport and CI
sleep are simulated. The clean path takes three Core decisions through merge
and sync, then one to record completion. No planning, coding, verification or
internal-review dispatch occurs.

Focused regressions cover pending review and CI, timeout, changed heads,
conflicts, failed checks, transport failure, one unknown blocker, bounded refusal
detail, a single rebase requiring new review approval, preservation of dirty
work, resuming sync without another merge, and a fixed force-push lease.

Outstanding tasks remain durable. Background completion after a process restart
still uses the existing completed-work source and its autonomy configuration;
that broader recovery configuration is not redesigned here. Tests do not claim
a live GitHub review/merge or a deployment of this change. External review has
not been requested. No law exception is requested.

## Changed files

Production:

- `src/alx/bootstrap/live_voice.py` — composition wiring only: compose the
  existing review waiter before registering review capabilities, and supply
  canonical repository authority to merge composition.
- `src/alx/bootstrap/review.py`, `src/alx/contracts/review.py`,
  `src/alx/tools/review.py`, `src/alx/tools/review_content.py` — bounded request
  completion and removal of successful pending reads.
- `src/alx/interfaces/task_poller.py`, `src/alx/continuity/tasks.py` — join the
  existing polling tick and avoid duplicate completion delivery.
- `src/alx/bootstrap/repository.py`,
  `src/alx/bootstrap/repository_authority.py`,
  `src/alx/providers/github_merge.py`, `src/alx/contracts/repository.py`,
  `src/alx/tools/repository.py` — readiness, refusal details, rebase and sync
  through the existing authority.
- `src/alx/providers/repository_authority.py`,
  `src/alx/contracts/repository_authority.py` — explicit expected-head lease on
  the existing force-push operation.
- `src/alx/core/loop.py` — the scoped, one-decision boundary for settled
  review/merge blockers; no change to general capability selection or CA fuses.

Tests: new `tests/test_ca_post_coding.py`; updated
`tests/test_merge_authority.py`, `tests/test_one_turn_one_review.py`,
`tests/test_read_external_review.py`, `tests/test_review_request.py`, and
`tests/test_task_status.py`.

Verification on the working changes:

- Focused pytest run: **371 passed, 213 subtests passed**, 36.33 seconds.
  Files: the six above plus `tests/test_external_review_handoff.py`,
  `tests/test_repository_authority.py`, and `tests/test_core_loop.py`.
- `python3 scripts/check_architecture.py`: passed.
- `python3 scripts/check_governance.py`: passed.
- `git diff --check`: passed.
- Full suite intentionally not run, as instructed.
