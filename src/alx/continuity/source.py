"""Turn matured future-cognition requests into cognition opportunities.

This module notices that a time has passed. That is the whole of its
intelligence, and it must stay that way.

It does not read goals, notebook entries, memories or research. It does not
read the note it carries. It does not rank, filter, defer or skip an occasion
because of what the occasion might be about. Anything of that kind would be a
rule about what deserves thought, which is Law 1 phrase routing with a clock
instead of a keyword, and the second mind would have arrived without anyone
deciding to build one.

The architecture gate enforces the import half of that structurally, because a
reviewer cannot be relied on to catch a filter added months from now.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any

from alx.contracts.cognition import CognitionOrigin
from alx.contracts.continuity import CognitionOpportunity


class FutureCognitionSource:
    """The one place a matured request becomes an occasion.

    Idempotent by construction: an opportunity's identity is derived from the
    request that matured, and the ledger refuses a repeat. A restart therefore
    replays nothing, which matters because a replayed occasion is a second paid
    Core call for a thought AL/X only asked for once.
    """

    def __init__(
        self,
        store: Any,
        ledger: Any,
        enabled: bool = False,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self._store = store
        self._ledger = ledger
        # Off by default. A runtime never told it may think unprompted does
        # not, and pending requests simply wait.
        self._enabled = enabled
        self._clock = clock or (lambda: datetime.now(UTC))

    @property
    def enabled(self) -> bool:
        return self._enabled

    @staticmethod
    def opportunity_id_for(request_id: str) -> str:
        """One request matures into one opportunity, permanently."""
        return f"self:{request_id}"

    def due_opportunities(self) -> tuple[CognitionOpportunity, ...]:
        """Every occasion whose time has come and which has not been created.

        Ordered by time alone, because time is the only ordering there is.
        Returns nothing at all when the master switch is off, and in that case
        touches no request: nothing is marked honoured, nothing is deleted.
        """
        if not self._enabled:
            return ()
        now = self._clock()
        opportunities = []
        for request in self._store.due(now):
            opportunity_id = self.opportunity_id_for(request.request_id)
            if self._ledger.exists(opportunity_id):
                continue
            opportunities.append(
                CognitionOpportunity(
                    opportunity_id=opportunity_id,
                    origin=CognitionOrigin.SELF_REQUESTED,
                    arose_at=request.not_before,
                    references=(f"future_cognition:{request.request_id}",),
                    # Carried, not read. This module never looks inside it.
                    note=request.note,
                    provenance=request.provenance,
                )
            )
        return tuple(opportunities)

    def claim(self, opportunity: CognitionOpportunity) -> bool:
        """Take an occasion exactly once, before anything is spent on it.

        Recording creation first is what makes the sequence safe across a
        crash: a process that dies after claiming and before invoking loses the
        turn, which is the right way to fail. Claiming afterwards could pay for
        the same thought twice.
        """
        return self._ledger.record_created(opportunity)

    def release(self, opportunity: CognitionOpportunity) -> None:
        """Return an occasion that produced nothing, so it can arise again.

        The claim exists to stop one request becoming two paid turns. When the
        turn did not happen there is nothing to protect against, and holding
        the claim would silently discard a cognition she asked for.
        """
        self._ledger.release(opportunity.opportunity_id)

    def mark_honoured(self, opportunity: CognitionOpportunity) -> None:
        """Close the request behind an occasion that has been acted on."""
        for reference in opportunity.references:
            if reference.startswith("future_cognition:"):
                self._store.mark_honoured(
                    reference[len("future_cognition:") :]
                )
