"""One bounded, prepaid research-cognition path.

Research tiers are configurations of this class. No generic specialist owns a
tier map, so a configured research model cannot be reached through an
unbudgeted sibling route. The complete provider request is bounded before the
ledger reserves its worst-case price and before the model is called.
"""

from __future__ import annotations

import json
from collections.abc import Callable, Mapping
from dataclasses import dataclass, replace
from typing import Any

from alx.contracts import (
    Cognition,
    ModelMessage,
    ModelRequest,
    ModelRole,
    ReasoningModel,
    ResearchLedger,
    ResearchModelUnpriced,
    ResearchPricing,
    ResearchQuestion,
    SpecialistError,
)
from alx.contracts.models import (
    PROTOCOL_INPUT_TOKEN_ALLOWANCE,
    input_token_upper_bound,
)




class ResearchModelUnbounded(Exception):
    def __init__(self, provider: str, model: str, worst_case: float, ceiling: float) -> None:
        self.provider = provider
        self.model = model
        self.worst_case = worst_case
        self.ceiling = ceiling
        super().__init__(
            f"{provider} model {model} can cost up to {worst_case:.4f} USD per "
            f"bounded request, above the {ceiling:.4f} USD per-request ceiling"
        )


class ResearchInputUnbounded(Exception):
    """The instruction and schema alone do not fit the priced input bound."""


class ResearchCeilingFailed(Exception):
    def __init__(self, overrun_usd: float) -> None:
        self.overrun_usd = overrun_usd
        super().__init__(
            f"research spend exceeded its reservations by {overrun_usd:.6f} USD; "
            "the enforced request bound did not hold"
        )


@dataclass(frozen=True, slots=True)
class ResearchTierModel:
    """One configured transport and the identity whose price is reserved."""

    provider: str
    model: str
    transport: ReasoningModel

    def __post_init__(self) -> None:
        if not self.provider.strip() or not self.model.strip():
            raise ValueError("research provider and model must not be blank")
        if not bool(getattr(self.transport, "supports_bounded_research", False)):
            raise ValueError(
                "research transport has not declared the complete-request bound"
            )


def _plain(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {str(key): _plain(item) for key, item in value.items()}
    if isinstance(value, tuple):
        return [_plain(item) for item in value]
    return value


class ResearchSpecialist:
    """Dispatch exactly one prepaid bounded question and return its result."""

    def __init__(
        self,
        tiers: Mapping[Cognition, ResearchTierModel],
        ledger: ResearchLedger,
        pricing: ResearchPricing,
        max_input_tokens: int,
        max_output_tokens: int,
        per_request_max_usd: float,
        telemetry_sink: Callable[[str, Mapping[str, Any]], None] | None = None,
        cache_key: str = "alx-research-v1",
    ) -> None:
        if max_input_tokens <= PROTOCOL_INPUT_TOKEN_ALLOWANCE:
            raise ValueError("max_input_tokens cannot fit provider framing")
        if max_output_tokens <= 0 or per_request_max_usd <= 0:
            raise ValueError("research output and cost bounds must be positive")
        if not tiers:
            raise ValueError("research requires at least one configured tier")
        if any(
            not isinstance(tier, Cognition)
            or not isinstance(configured, ResearchTierModel)
            for tier, configured in tiers.items()
        ):
            raise TypeError("research tiers must map Cognition to ResearchTierModel")
        self._tiers = dict(tiers)
        self._ledger = ledger
        self._pricing = pricing
        self._max_input_tokens = max_input_tokens
        self._max_output_tokens = max_output_tokens
        self._per_request_max_usd = per_request_max_usd
        self._telemetry_sink = telemetry_sink
        self._cache_key = cache_key

    def answer(
        self, question: ResearchQuestion, task_id: str = ""
    ) -> Mapping[str, Any]:
        if not isinstance(question, ResearchQuestion):
            raise TypeError("question must be a ResearchQuestion")
        configured = self._tiers.get(question.cognition)
        if configured is None:
            raise SpecialistError(
                f"cognition_tier_unconfigured:{question.cognition.value}"
            )
        provider, model = configured.provider, configured.model
        if not self._pricing.is_priced(provider, model):
            raise ResearchModelUnpriced(provider, model)

        request, _input_bound, omitted = self._bounded_request(question, task_id)
        if omitted:
            # Visible to an operator as well as to AL/X: material that never
            # reached the model is a fact about the evidence, not a detail of
            # transport.
            self._emit(
                task_id or question.question_id,
                {
                    "code": "research.material_omitted",
                    "kind": "research",
                    "tier": question.cognition.value,
                    "question_id": question.question_id,
                    "omitted_characters": omitted,
                },
            )
        worst_case = self._pricing.worst_case_usd(
            provider, model, self._max_input_tokens, self._max_output_tokens
        )
        if worst_case is None:
            raise ResearchModelUnpriced(provider, model)
        if worst_case > self._per_request_max_usd:
            raise ResearchModelUnbounded(
                provider, model, worst_case, self._per_request_max_usd
            )
        overrun = self._ledger.overrun_usd()
        if overrun > 0:
            raise ResearchCeilingFailed(overrun)
        reservation = self._ledger.reserve(
            question.cognition.value,
            provider,
            model,
            kind="research",
            worst_case_usd=worst_case,
        )
        request = replace(
            request,
            reservation_id=reservation.reservation_id,
            reserved_usd=reservation.reserved_usd,
        )
        telemetry_task = task_id or question.question_id
        self._emit(
            telemetry_task,
            {
                "code": "research.reserved",
                "kind": "research",
                "tier": question.cognition.value,
                "provider": provider,
                "model": model,
                "reservation_id": reservation.reservation_id,
                "reserved_usd": reservation.reserved_usd,
                "cost_usd": 0.0,
                "outcome": "reserved",
            },
        )
        try:
            completion = configured.transport.complete(request)
        except Exception as error:
            failure_code = type(error).__name__
            settled = self._ledger.abandon(
                reservation, failure_code=failure_code
            )
            self._emit(
                telemetry_task,
                self._event(
                    question, configured, reservation, settled,
                    "failed", failure_code, {},
                ),
            )
            raise SpecialistError(failure_code) from None

        values = completion.output
        if not isinstance(values, Mapping):
            settled = self._ledger.abandon(
                reservation, failure_code="answer_not_structured"
            )
            self._emit(
                telemetry_task,
                self._event(
                    question, configured, reservation, settled,
                    "failed", "answer_not_structured", {},
                ),
            )
            raise SpecialistError("answer_not_structured")

        usage = completion.usage if isinstance(completion.usage, Mapping) else {}
        actual_provider, actual_model = completion.provider, completion.model
        if not self._pricing.is_priced(actual_provider, actual_model):
            settled = self._ledger.abandon(
                reservation,
                failure_code="actual_model_unpriced",
                usage=usage,
                provider=actual_provider,
                model=actual_model,
            )
            self._emit(
                telemetry_task,
                self._event(
                    question,
                    ResearchTierModel(actual_provider, actual_model, configured.transport),
                    reservation,
                    settled,
                    "failed",
                    "actual_model_unpriced",
                    usage,
                ),
            )
            raise ResearchModelUnpriced(actual_provider, actual_model)
        actual = self._pricing.cost_usd(actual_provider, actual_model, usage)
        settled = self._ledger.settle(
            reservation,
            reservation.reserved_usd if actual is None else actual,
            usage=usage,
            provider=actual_provider,
            model=actual_model,
        )
        overrun = self._ledger.overrun_usd()
        self._emit(
            telemetry_task,
            self._event(
                question,
                ResearchTierModel(actual_provider, actual_model, configured.transport),
                reservation, settled,
                "failed" if overrun > 0 else "succeeded",
                "cost_overrun" if overrun > 0 else "",
                usage,
            ),
        )
        if overrun > 0:
            raise ResearchCeilingFailed(overrun)
        # An answer read from part of the material is not the same evidence as
        # an answer read from all of it. The Core is told how much was left
        # out so it can weigh the finding, ask a narrower question, or split
        # the material. Nothing here decides that the answer is good enough.
        # A complete read adds no field, so the ordinary answer is unchanged
        # and the presence of the field is itself the signal.
        if omitted:
            return {**values, "material_omitted_characters": omitted}
        return values

    def _bounded_request(
        self, question: ResearchQuestion, task_id: str
    ) -> tuple[ModelRequest, int, int]:
        """Build the largest request that fits, and say what did not fit.

        The material may be longer than the priced input bound allows. Cutting
        it is a mechanical consequence of that bound, but deciding whether an
        answer read from part of the material is still worth having is a
        judgement, so the omission is reported rather than resolved here.
        """
        def build(material: str) -> ModelRequest:
            return ModelRequest(
                (
                    ModelMessage(ModelRole.SYSTEM, question.instruction),
                    ModelMessage(ModelRole.USER, material),
                ),
                question.question_id,
                question.answer_schema,
                task_id or question.question_id,
                self._cache_key,
                self._max_output_tokens,
                kind="research",
                tier=question.cognition.value,
            )

        # Two separate cuts can remove material: the question's own
        # material_limit, applied before this specialist ever sees the text,
        # and the priced input bound applied below. Measuring only the second
        # would report a complete read of an already-shortened document, so
        # the shortfall is counted against the original source.
        already_omitted = question.material_omitted_characters
        material = question.bounded_material
        minimum = build(material[:1])
        if input_token_upper_bound(minimum) > self._max_input_tokens:
            raise ResearchInputUnbounded()
        low, high = 1, len(material)
        while low < high:
            middle = (low + high + 1) // 2
            if input_token_upper_bound(build(material[:middle])) <= self._max_input_tokens:
                low = middle
            else:
                high = middle - 1
        request = build(material[:low])
        return (
            request,
            input_token_upper_bound(request),
            already_omitted + (len(material) - low),
        )

    def _event(
        self,
        question: ResearchQuestion,
        configured: ResearchTierModel,
        reservation: Any,
        cost: float,
        outcome: str,
        failure_code: str,
        usage: Mapping[str, Any],
    ) -> Mapping[str, Any]:
        return {
            "code": "research.completed",
            "kind": "research",
            "tier": question.cognition.value,
            "provider": configured.provider,
            "model": configured.model,
            "reservation_id": reservation.reservation_id,
            "reserved_usd": reservation.reserved_usd,
            "cost_usd": cost,
            "outcome": outcome,
            "failure_code": failure_code,
            "input_tokens": usage.get("input_tokens", 0),
            "cached_tokens": usage.get("cached_tokens", 0),
            "output_tokens": usage.get("output_tokens", 0),
            "reasoning_tokens": usage.get("reasoning_tokens", 0),
            "remaining_usd": round(self._ledger.remaining_usd(), 6),
        }

    def _emit(self, task_id: str, values: Mapping[str, Any]) -> None:
        if self._telemetry_sink is None:
            return
        try:
            self._telemetry_sink(task_id, values)
        except Exception:
            pass


__all__ = [
    "ResearchCeilingFailed",
    "ResearchInputUnbounded",
    "ResearchModelUnbounded",
    "ResearchSpecialist",
    "ResearchTierModel",
    "input_token_upper_bound",
]
