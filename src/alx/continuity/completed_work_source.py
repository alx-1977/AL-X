"""Turn a finished external task into a cognition occasion.

This is the sibling of the matured-request source, and deliberately as thin.
That one notices a time has passed; this one notices a task has finished. Both
produce an occasion and neither decides anything about it.

It does not read the result, rank it, judge whether the finding matters, or
skip a completion because of what it might be about. It does not know what a
review is. Anything of that kind would be a rule about what deserves thought,
which is the second mind arriving without anyone deciding to build one.

Existing production ingress is reused rather than extended: the occasion goes
to the same runner, through the same claim, the same ledger and the same lock
as every other origin. There is no second path to a Core turn.
"""

from __future__ import annotations

from typing import Any

from alx.contracts.cognition import CognitionOrigin
from alx.contracts.continuity import CognitionOpportunity


class CompletedWorkSource:
    """The one place a finished external task becomes an occasion.

    Idempotent by construction, like its sibling: an occasion's identity comes
    from the task, the ledger refuses a repeat, and the task is marked handed
    over once the turn has run. A restart therefore replays nothing, which
    matters because a replayed occasion is a second paid Core call for one
    result.
    """

    def __init__(self, store: Any, ledger: Any, enabled: bool = False) -> None:
        self._store = store
        self._ledger = ledger
        # Off by default, for the same reason the matured-request source is:
        # a runtime never told it may think unprompted does not, and finished
        # work simply waits to be looked at.
        self._enabled = enabled

    @property
    def enabled(self) -> bool:
        return self._enabled

    @staticmethod
    def opportunity_id_for(task_id: str) -> str:
        return f"task:{task_id}"

    def due_opportunities(self) -> tuple[CognitionOpportunity, ...]:
        """Every completion the Core has not yet been given.

        Ordered by completion time alone, because that is the only ordering
        there is. Returns nothing when the master switch is off, and in that
        case touches no task.
        """
        if not self._enabled:
            return ()
        opportunities = []
        for task in self._store.completed_unhandled():
            opportunity_id = self.opportunity_id_for(task.task_id)
            if self._ledger.exists(opportunity_id):
                continue
            opportunities.append(
                CognitionOpportunity(
                    opportunity_id=opportunity_id,
                    origin=CognitionOrigin.WORK_COMPLETED,
                    arose_at=task.completed_at,
                    # The occasion returns to the thread the work was asked
                    # for in.
                    conversation_id=task.conversation_id,
                    # What the work was about, in the external system's terms.
                    # Never the result: AL/X reads that from the source.
                    references=(f"external_task:{task.task_id}",),
                )
            )
        return tuple(opportunities)

    def owns(self, opportunity: CognitionOpportunity) -> bool:
        """Whether this producer made the occasion."""
        return any(
            reference.startswith("external_task:")
            for reference in opportunity.references
        )

    def claim(self, opportunity: CognitionOpportunity) -> bool:
        """Take an occasion exactly once, before anything is spent on it."""
        return self._ledger.record_created(opportunity)

    def release(self, opportunity: CognitionOpportunity) -> None:
        """Return an occasion that produced nothing, so it can arise again."""
        self._ledger.release(opportunity.opportunity_id)

    def mark_honoured(self, opportunity: CognitionOpportunity) -> None:
        """Close the task behind an occasion that has been acted on."""
        for reference in opportunity.references:
            if reference.startswith("external_task:"):
                self._store.mark_handed_over(reference[len("external_task:"):])
