"""Bounded research questions under a hard daily spending ceiling.

This is the specialist path with a budget wrapped around it, not a new kind of
call. AL/X composes the question and its cognition tier exactly as she does for
any bounded question; what this adds is that the money is reserved before the
model runs and reconciled after, so autonomous research cannot cross Friedl's
boundary even on its last and most expensive call.

Nothing here decides what to research or what an answer means. It buys thinking
time and records what it cost.
"""

from __future__ import annotations

from typing import Any, Callable, Mapping

from alx.contracts import (
    Cognition,
    ResearchLedger,
    ResearchModelUnpriced,
    ResearchPricing,
    SpecialistError,
    SpecialistQuestion,
)


class ResearchSpecialist:
    """Put one bounded research question, within the day's budget."""

    def __init__(
        self,
        specialist: Any,
        ledger: ResearchLedger,
        pricing: ResearchPricing,
        model_identity: Callable[[Cognition], tuple[str, str]],
        telemetry_sink: Callable[[str, Mapping[str, Any]], None] | None = None,
    ) -> None:
        self._specialist = specialist
        self._ledger = ledger
        self._pricing = pricing
        # Which provider and model a tier resolves to, so the ledger can record
        # what was actually charged rather than what was configured somewhere.
        self._model_identity = model_identity
        self._telemetry_sink = telemetry_sink

    def answer(
        self, question: SpecialistQuestion, task_id: str = ""
    ) -> Mapping[str, Any]:
        provider, model = self._model_identity(question.cognition)
        if not self._pricing.is_priced(provider, model):
            # Refused before any spend. An unpriced model cannot be reconciled
            # against the ceiling, and a guessed price would quietly break it.
            raise ResearchModelUnpriced(provider, model)

        # Raises ResearchBudgetExceeded when the day cannot cover one more
        # request. It does not select a cheaper tier or another provider.
        reservation = self._ledger.reserve(
            question.cognition.value, provider, model, kind="research"
        )
        try:
            answer = self._specialist.answer(question)
        except Exception:
            # A failed call may still have been billed: a timeout after the
            # model answered, a stream cut mid-response. The reservation is
            # kept in full rather than refunded, because treating failures as
            # free would let unbounded failing calls run inside one day.
            self._ledger.abandon(reservation)
            raise

        usage = getattr(self._specialist, "last_usage", None)
        actual = (
            self._pricing.cost_usd(provider, model, usage)
            if isinstance(usage, Mapping)
            else None
        )
        # A priced model with no reported usage settles at the full reservation
        # rather than at zero. Treating an unmeasured call as free would let an
        # unlimited number of them run inside one day's budget.
        settled = self._ledger.settle(
            reservation, reservation.reserved_usd if actual is None else actual
        )
        measured = usage if isinstance(usage, Mapping) else {}
        self._emit(
            task_id,
            {
                "code": "research.completed",
                "kind": "research",
                "tier": question.cognition.value,
                "provider": provider,
                "model": model,
                "question_id": question.question_id,
                "cost_usd": settled,
                "cost_measured": actual is not None,
                "remaining_usd": round(self._ledger.remaining_usd(), 6),
                # The same token fields every other reasoning call records, so
                # Core, specialist and research spend read from one table.
                "input_tokens": measured.get("input_tokens", 0),
                "cached_tokens": measured.get("cached_tokens", 0),
                "output_tokens": measured.get("output_tokens", 0),
                "reasoning_tokens": measured.get("reasoning_tokens", 0),
            },
        )
        return answer

    def _emit(self, task_id: str, values: Mapping[str, Any]) -> None:
        if not task_id or self._telemetry_sink is None:
            return
        try:
            self._telemetry_sink(task_id, values)
        except Exception:
            # Telemetry must never fail research it is only observing.
            pass


__all__ = ["ResearchSpecialist", "SpecialistError"]
