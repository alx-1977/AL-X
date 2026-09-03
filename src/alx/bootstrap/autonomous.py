"""The one production path from a due occasion to a paid autonomous Core turn.

The ordering below is the whole safety property, and it is deliberately rigid:

    master switch
      -> a due opportunity exists
      -> the model is priced and the bounds are known
      -> the worst case is reserved from the daily ledger
      -> the Core is invoked
      -> usage is reconciled against the reservation
      -> the outcome is persisted

Every step is a gate on the next. Nothing is dispatched before its worst case
is withdrawn, so a crash at any point leaves the day over-charged rather than
under-charged, which is the only direction a ceiling may fail in.

There is no fallback, no downgrade and no provider substitution anywhere in
this file. A refusal is a refusal: it becomes evidence AL/X reasons about on a
later turn, not a quieter purchase.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from typing import Any

from alx.contracts import CognitionOpportunity

LOGGER = logging.getLogger(__name__)


class AutonomousCognitionRunner:
    """Run one due occasion, under the approved ordering."""

    def __init__(
        self,
        source: Any,
        ledger: Any,
        budget: Any,
        gateway: Any,
        provider: str,
        model: str,
        max_input_tokens: int,
        max_output_tokens: int,
        conversation_id: str,
        step_budget: int,
        retention_days: int,
        usage_of: Callable[[], Any] | None = None,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self._source = source
        self._ledger = ledger
        self._budget = budget
        self._gateway = gateway
        self._provider = provider
        self._model = model
        self._max_input_tokens = max_input_tokens
        self._max_output_tokens = max_output_tokens
        self._conversation_id = conversation_id
        self._step_budget = step_budget
        self._retention_days = retention_days
        # Returns the measured usage of the turn just run, for settlement.
        self._usage_of = usage_of or (lambda: None)
        # Timezone-aware, like every other clock in the runtime. A naive
        # datetime here would reach retention_until and the durable records,
        # where the contracts reject it, so the failure would surface as a
        # broken turn rather than as the wrong time.
        self._clock = clock or (lambda: datetime.now(UTC))

    def run_due(self) -> tuple[str, ...]:
        """Run every occasion that is due now. Returns what was attempted."""
        # Step 1. The master switch. `due_opportunities` returns nothing when
        # autonomous cognition is disabled, and touches no request.
        attempted: list[str] = []
        for opportunity in self._source.due_opportunities():
            if self._run_one(opportunity):
                attempted.append(opportunity.opportunity_id)
        return tuple(attempted)

    def _run_one(self, opportunity: CognitionOpportunity) -> bool:
        # Step 2. Claim it before spending anything, so a crash loses the turn
        # rather than paying for it twice.
        if not self._source.claim(opportunity):
            return False

        # Step 3. Invoke the one Core, through the one gateway. Phase 3's
        # origin selection routes this to the autonomous reasoner, which owns
        # the money sequence because it is the only place the exact request
        # exists: it constructs the request once, measures that object, refuses
        # or reserves against it, and dispatches the same object. Reserving
        # here would be paying against an estimate of a request not yet built.
        outcome_state = "error"
        try:
            outcome = self._gateway.receive_cognition_opportunity(
                self._conversation_id,
                opportunity,
                self._step_budget,
                self._clock() + timedelta(days=self._retention_days),
            )
            outcome_state = outcome.state.value
        except Exception as error:
            LOGGER.warning("Autonomous cognition turn failed: %s", error)
        finally:
            # Step 4. Persist the outcome. Reservation and settlement already
            # happened inside the reasoning boundary, against the exact request.
            self._ledger.record_outcome(
                opportunity.opportunity_id,
                outcome_state,
                reasoning_calls=1,
                usage=self._usage_of(),
            )
        # The request is closed only once the turn actually happened, so a
        # refused or crashed occasion is not silently marked as taken.
        if outcome_state != "error":
            self._source.mark_honoured(opportunity)
        return True


class LedgerSpendAuthority:
    """Binds the durable autonomous ledger to the reasoning boundary.

    The reasoner knows it must reserve before dispatching and settle after; it
    does not know about SQLite, days or reconciliation. This adapter is the one
    place those meet, so the money rules stay in observability and the ordering
    stays where the exact request is.
    """

    def __init__(self, ledger: Any, provider: str, model: str) -> None:
        self._ledger = ledger
        self._provider = provider
        self._model = model

    def reserve(self, max_input_tokens: int, max_output_tokens: int) -> Any:
        """Withdraw the worst case, or raise. Never a smaller allowance."""
        return self._ledger.reserve(
            self._provider, self._model, max_input_tokens, max_output_tokens
        )

    def settle(self, reservation: Any, usage: Any) -> float:
        """Reconcile. Usage that cannot be priced keeps the full reservation."""
        return self._ledger.settle(
            reservation, self._provider, self._model, usage
        )
