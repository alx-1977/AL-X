"""Turn this process starting into one cognition occasion.

A restart is something new in AL/X's world that she cannot otherwise notice:
on 2026-10-06 she recorded work as "after Friedl restarts me" and then waited,
because nothing woke her when he did. This producer offers exactly one
occasion per process start. Like its siblings it decides nothing: it does not
look at her goals, choose what to resume, or say anything. What the restart
means, and whether anything was waiting on it, is hers to judge from the
`runtime` facts every turn already shows her.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from alx.contracts.cognition import CognitionOrigin
from alx.contracts.continuity import CognitionOpportunity


# Her thread for occasions about the runtime itself, as each mail thread is
# its own conversation. A restart belongs to no person's conversation.
RUNTIME_CONVERSATION_ID = "runtime"
_PREFIX = "runtime-started:"


class RuntimeStartedSource:
    """The one place a process start becomes an occasion."""

    def __init__(self, ledger: Any, started_at: datetime, enabled: bool = False) -> None:
        self._ledger = ledger
        self._started_at = started_at
        # Off unless the runtime may think unprompted, as for every sibling.
        self._enabled = enabled
        self._opportunity_id = f"{_PREFIX}{started_at.isoformat()}"

    @property
    def enabled(self) -> bool:
        return self._enabled

    def due_opportunities(self) -> tuple[CognitionOpportunity, ...]:
        """This start, until the Core has been given it once."""
        if not self._enabled or self._ledger.exists(self._opportunity_id):
            return ()
        return (
            CognitionOpportunity(
                opportunity_id=self._opportunity_id,
                origin=CognitionOrigin.EXTERNAL_EVENT,
                arose_at=self._started_at,
                conversation_id=RUNTIME_CONVERSATION_ID,
                references=(f"runtime:started:{self._started_at.isoformat()}",),
            ),
        )

    def recover(self, spend: Any = None) -> tuple[str, ...]:
        """Close starts a stopped process left claimed; they are never offered again.

        Only the current start is ever offered, so an earlier one cannot be
        replayed. One that reached dispatch is kept for inspection; one that
        did not is released, which ends it.
        """
        reclaimed: list[str] = []
        for row in self._ledger.unfinished():
            opportunity_id = row["opportunity_id"]
            if not opportunity_id.startswith(_PREFIX) or opportunity_id == self._opportunity_id:
                continue
            if spend is not None and spend.dispatch_started(opportunity_id):
                self._ledger.mark_unreconciled(opportunity_id)
                continue
            self._ledger.release(opportunity_id)
            reclaimed.append(opportunity_id)
        return tuple(reclaimed)

    def owns(self, opportunity: CognitionOpportunity) -> bool:
        return opportunity.opportunity_id.startswith(_PREFIX)

    def claim(self, opportunity: CognitionOpportunity) -> bool:
        return self._ledger.record_created(opportunity)

    def release(self, opportunity: CognitionOpportunity) -> None:
        self._ledger.release(opportunity.opportunity_id)

    def mark_honoured(self, opportunity: CognitionOpportunity) -> None:
        """Nothing to close: the ledger row is the whole record of a start."""
