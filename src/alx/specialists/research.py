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

from dataclasses import replace
from typing import Any, Callable, Mapping

from alx.contracts import (
    Cognition,
    ResearchLedger,
    ResearchModelUnpriced,
    ResearchPricing,
    SpecialistError,
    SpecialistQuestion,
)


class ResearchModelUnbounded(Exception):
    """The model's worst-case cost exceeds the configured per-request ceiling.

    A reservation is only honest if the request cannot cost more than it. When
    the provider bound still permits a price above the ceiling, the model is
    refused for autonomous research rather than used with a ceiling that would
    not hold.
    """

    def __init__(self, provider: str, model: str, worst_case: float, ceiling: float) -> None:
        self.provider = provider
        self.model = model
        self.worst_case = worst_case
        self.ceiling = ceiling
        super().__init__(
            f"{provider} model {model} can cost up to {worst_case:.4f} USD per "
            f"bounded request, above the {ceiling:.4f} USD per-request ceiling"
        )


class ResearchCeilingFailed(Exception):
    """A settled cost exceeded its reservation, so the ceiling did not hold.

    Research stops until Friedl has seen it. Continuing would spend against a
    limit already known to be broken, which is worse than not researching.
    """

    def __init__(self, overrun_usd: float) -> None:
        self.overrun_usd = overrun_usd
        super().__init__(
            f"research spend exceeded its reservations by {overrun_usd:.6f} USD; "
            "the enforced request bound did not hold"
        )


class ResearchSpecialist:
    """Put one bounded research question, within the day's budget.

    The bound comes first. Before anything is reserved the request is capped at
    a token ceiling the provider enforces, and the worst-case price of that
    capped request is compared with the configured per-request maximum. Only a
    model that cannot exceed the maximum may run.
    """

    def __init__(
        self,
        specialist: Any,
        ledger: ResearchLedger,
        pricing: ResearchPricing,
        model_identity: Callable[[Cognition], tuple[str, str]],
        max_input_tokens: int,
        max_output_tokens: int,
        per_request_max_usd: float,
        telemetry_sink: Callable[[str, Mapping[str, Any]], None] | None = None,
    ) -> None:
        self._specialist = specialist
        self._ledger = ledger
        self._pricing = pricing
        # Which provider and model a tier resolves to, so the ledger can record
        # what was actually charged rather than what was configured somewhere.
        self._model_identity = model_identity
        self._max_input_tokens = max_input_tokens
        self._max_output_tokens = max_output_tokens
        self._per_request_max_usd = per_request_max_usd
        self._telemetry_sink = telemetry_sink

    def answer(
        self, question: SpecialistQuestion, task_id: str = ""
    ) -> Mapping[str, Any]:
        provider, model = self._model_identity(question.cognition)
        if not self._pricing.is_priced(provider, model):
            # Refused before any spend. An unpriced model cannot be reconciled
            # against the ceiling, and a guessed price would quietly break it.
            raise ResearchModelUnpriced(provider, model)
        worst_case = self._pricing.worst_case_usd(
            provider, model, self._max_input_tokens, self._max_output_tokens
        )
        if worst_case is None:
            raise ResearchModelUnpriced(provider, model)
        if worst_case > self._per_request_max_usd:
            # The ceiling would not hold for this model at this bound, so the
            # model is refused rather than the ceiling quietly weakened.
            raise ResearchModelUnbounded(
                provider, model, worst_case, self._per_request_max_usd
            )

        # Raises ResearchBudgetExceeded when the day cannot cover one more
        # request. It does not select a cheaper tier or another provider.
        # A prior overrun means an enforced bound already failed today, so the
        # ceiling is not trustworthy until Friedl looks at it. Continuing would
        # spend against a limit known to be broken.
        overrun = self._ledger.overrun_usd()
        if overrun > 0:
            raise ResearchCeilingFailed(overrun)
        # The reservation is the worst case of this exact bounded request, so
        # settlement cannot exceed what was withdrawn.
        reservation = self._ledger.reserve(
            question.cognition.value,
            provider,
            model,
            kind="research",
            worst_case_usd=worst_case,
        )
        # The input bound must be enforced on the material, not merely declared
        # to the pricing calculation. A question whose material exceeds it is
        # truncated to the bound the worst case was computed from.
        bounded = self._bounded(question)
        try:
            answer = self._specialist.answer(
                bounded, max_output_tokens=self._max_output_tokens
            )
        except Exception as error:
            # A failed call may still have been billed: a timeout after the
            # model answered, a stream cut mid-response. The reservation is
            # kept in full rather than refunded, because treating failures as
            # free would let unbounded failing calls run inside one day.
            settled = self._ledger.abandon(reservation)
            # A failure that leaves no durable row makes spend unauditable, so
            # the same lifecycle record is written for both outcomes.
            self._emit(
                task_id,
                {
                    "code": "research.completed",
                    "kind": "research",
                    "tier": question.cognition.value,
                    "provider": provider,
                    "model": model,
                    "question_id": question.question_id,
                    "reservation_id": reservation.reservation_id,
                    "reserved_usd": reservation.reserved_usd,
                    "cost_usd": settled,
                    "cost_measured": False,
                    "outcome": "failed",
                    "failure_code": type(error).__name__,
                    "remaining_usd": round(self._ledger.remaining_usd(), 6),
                },
            )
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
                "reservation_id": reservation.reservation_id,
                "reserved_usd": reservation.reserved_usd,
                "cost_usd": settled,
                "cost_measured": actual is not None,
                "outcome": "succeeded",
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

    def _bounded(self, question: SpecialistQuestion) -> SpecialistQuestion:
        """Truncate the material to the input bound the worst case assumed.

        Characters are not tokens, so this uses a deliberately conservative
        ratio: at least one token per character means the character budget can
        never exceed the token bound the price was computed from.
        """
        limit = min(question.material_limit, self._max_input_tokens)
        if question.material_limit <= limit:
            return question
        return replace(question, material_limit=limit)

    def _emit(self, task_id: str, values: Mapping[str, Any]) -> None:
        if not task_id or self._telemetry_sink is None:
            return
        try:
            self._telemetry_sink(task_id, values)
        except Exception:
            # Telemetry must never fail research it is only observing.
            pass


__all__ = [
    "ResearchCeilingFailed",
    "ResearchModelUnbounded",
    "ResearchSpecialist",
    "SpecialistError",
]
