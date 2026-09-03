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
        gateway: Any,
        conversation_id: str,
        step_budget: int,
        retention_days: int,
        usage_of: Callable[[], Any] | None = None,
        clock: Callable[[], datetime] | None = None,
        spend_observer: Any = None,
    ) -> None:
        # Deliberately no budget, provider, model or token bounds. Those belong
        # to the reasoning boundary, which is the only place the exact request
        # exists; holding them here as well would suggest this class still
        # authorises spend, and the next person to read it would reasonably
        # assume the reservation happens before the request is built.
        self._source = source
        self._ledger = ledger
        self._gateway = gateway
        self._conversation_id = conversation_id
        self._step_budget = step_budget
        self._retention_days = retention_days
        # Returns the measured usage of the turn just run, for settlement.
        self._usage_of = usage_of or (lambda: None)
        self._spend_observer = spend_observer
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
        # Bind cost reporting to this occasion for the duration of the turn, so
        # what the reasoning boundary spends lands on the right ledger row.
        spend = _OccasionSpend()
        if self._spend_observer is not None:
            self._spend_observer.watch(spend)
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
            if self._spend_observer is not None:
                self._spend_observer.release()
            if spend.provider:
                self._ledger.record_reserved(
                    opportunity.opportunity_id,
                    spend.provider,
                    spend.model,
                    spend.reserved_usd,
                )
            self._ledger.record_outcome(
                opportunity.opportunity_id,
                outcome_state,
                reasoning_calls=1,
                settled_usd=spend.settled_usd,
                usage=self._usage_of(),
            )
        # The request is closed only once the turn actually happened, so a
        # refused or crashed occasion is not silently marked as taken.
        if outcome_state == "error":
            # The turn did not happen, so the occasion is given back rather
            # than kept. Its request stays pending and will mature again;
            # holding the claim would leave her waiting on a cognition that
            # could never arrive.
            self._source.release(opportunity)
        else:
            self._source.mark_honoured(opportunity)
        return True


class LedgerSpendAuthority:
    """Binds the durable autonomous ledger to the reasoning boundary.

    The reasoner knows it must reserve before dispatching and settle after; it
    does not know about SQLite, days or reconciliation. This adapter is the one
    place those meet, so the money rules stay in observability and the ordering
    stays where the exact request is.
    """

    def __init__(
        self,
        ledger: Any,
        provider: str,
        model: str,
        observer: Any = None,
        opportunity_id: Any = None,
    ) -> None:
        self._ledger = ledger
        # Which occasion is being served, asked at reserve time. The reasoner
        # does not know; the relay does.
        self._opportunity_id = opportunity_id or (lambda: "")
        self._provider = provider
        self._model = model
        # Reports what was withdrawn and settled so the opportunity ledger can
        # record the cost of a turn. D-024 requires every dollar to be
        # inspectable, and the spend ledger alone answers "what did the day
        # cost" without answering "what did this occasion cost".
        self._observer = observer

    def reserve(self, max_input_tokens: int, max_output_tokens: int) -> Any:
        """Withdraw the worst case, or raise. Never a smaller allowance."""
        reservation = self._ledger.reserve(
            self._provider,
            self._model,
            max_input_tokens,
            max_output_tokens,
            # Links the withdrawal to the occasion, so recovery can ask this
            # ledger whether that occasion ever reached the provider.
            self._opportunity_id(),
        )
        if self._observer is not None:
            self._observer.reserved(
                self._provider, self._model, reservation.reserved_usd
            )
        return reservation

    def mark_dispatched(self, reservation: Any) -> None:
        """Durably record that the provider is about to be reached."""
        self._ledger.mark_dispatched(reservation)

    def settle(self, reservation: Any, usage: Any) -> float:
        """Reconcile. Usage that cannot be priced keeps the full reservation."""
        settled = self._ledger.settle(
            reservation, self._provider, self._model, usage
        )
        if self._observer is not None:
            self._observer.settled(settled)
        return settled


class _OccasionSpend:
    """What one occasion withdrew and settled. Written by the relay below."""

    def __init__(self) -> None:
        self.provider = ""
        self.model = ""
        self.reserved_usd = 0.0
        self.settled_usd: float | None = None


class OccasionSpendRelay:
    """Routes spend reported by the reasoning boundary to the current occasion.

    The reasoner authorises spend and does not know which occasion it is
    serving; the runner knows the occasion and no longer authorises spend.
    This is the seam between them, deliberately a dumb relay rather than a
    second accounting path: it copies three numbers and decides nothing.
    """

    def __init__(self) -> None:
        self._current: Any = None

    def watch(self, spend: Any) -> None:
        self._current = spend

    def release(self) -> None:
        self._current = None

    def reserved(self, provider: str, model: str, reserved_usd: float) -> None:
        if self._current is not None:
            self._current.provider = provider
            self._current.model = model
            self._current.reserved_usd = reserved_usd

    def settled(self, settled_usd: float) -> None:
        if self._current is not None:
            self._current.settled_usd = settled_usd
