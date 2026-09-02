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

        # Step 3 and 4. Price, bounds, and the reservation, in one call that
        # refuses rather than returning something cheaper. An unpriced model, a
        # missing bound or an exhausted day all raise here, before dispatch.
        try:
            reservation = self._budget.reserve(
                self._provider,
                self._model,
                self._max_input_tokens,
                self._max_output_tokens,
                opportunity.opportunity_id,
            )
        except Exception as error:
            LOGGER.info(
                "Autonomous cognition refused before dispatch: %s: %s",
                type(error).__name__,
                error,
            )
            self._ledger.record_outcome(
                opportunity.opportunity_id, f"refused_{type(error).__name__}"
            )
            # The request stays pending. A refusal is not a thought she had.
            return False
        self._ledger.record_reserved(
            opportunity.opportunity_id,
            self._provider,
            self._model,
            reservation.reserved_usd,
        )

        # Step 5. Invoke the one Core, through the one gateway. Phase 3's
        # origin selection routes this to the autonomous reasoner; nothing here
        # chooses a model.
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
            # Step 6. Settle whatever happened. Usage that cannot be measured
            # keeps the full reservation: a provider that reported nothing has
            # not told us the turn was free.
            settled = self._budget.settle(
                reservation, self._provider, self._model, self._usage_of()
            )
            # Step 7. Persist the outcome. Identities, counts and money only.
            self._ledger.record_outcome(
                opportunity.opportunity_id,
                outcome_state,
                reasoning_calls=1,
                settled_usd=settled,
                usage=self._usage_of(),
            )
        # The request is closed only once the turn actually happened, so a
        # refused or crashed occasion is not silently marked as taken.
        if outcome_state != "error":
            self._source.mark_honoured(opportunity)
        return True
