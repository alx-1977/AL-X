"""Put one bounded question to a model and return structured data.

This is a narrow invocation, not an agent. It sends the instruction, the
bounded material and the answer schema, and nothing else. There is no
conversation, no goal, no capability catalogue, no memory and no loop: the
model answers once and the caller decides what the answer means.
"""

from __future__ import annotations

import json
from typing import Any, Mapping

from alx.contracts import (
    Cognition,
    ModelMessage,
    ModelRequest,
    ModelRole,
    ReasoningModel,
    SpecialistError,
    SpecialistQuestion,
)


class ModelSpecialist:
    """Answer bounded questions through the shared replaceable model port.

    One specialist serves every cognition tier. A tier chooses which configured
    model answers; it does not change what the call is, what it may do, or what
    happens to the answer. Three tiers are three configurations of this one
    path, not three paths.
    """

    def __init__(
        self,
        model: ReasoningModel,
        cache_key: str = "alx-specialist-v1",
        tiers: Mapping[Cognition, ReasoningModel] | None = None,
    ) -> None:
        self._model = model
        self._cache_key = cache_key
        self._tiers = dict(tiers or {})
        # Usage from the most recent answer, so a caller enforcing a spending
        # ceiling can price what the call actually consumed. It is measurement,
        # not state: nothing reads it to decide anything.
        self.last_usage: Mapping[str, Any] | None = None

    def model_for(self, cognition: Cognition) -> ReasoningModel:
        """The model configured for one tier.

        An unconfigured tier falls back to the default specialist model rather
        than to a more expensive one: a missing configuration must never buy
        more capability than Friedl configured.
        """
        return self._tiers.get(cognition, self._model)

    def answer(self, question: SpecialistQuestion) -> Mapping[str, Any]:
        request = ModelRequest(
            (
                # The instruction is the whole system context. AL/X's laws,
                # identity and catalogue are deliberately absent: this call
                # decides nothing, so it needs none of her authority.
                ModelMessage(ModelRole.SYSTEM, question.instruction),
                ModelMessage(ModelRole.USER, question.bounded_material),
            ),
            question.question_id,
            question.answer_schema,
            question.question_id,
            self._cache_key,
        )
        self.last_usage = None
        try:
            completion = self.model_for(question.cognition).complete(request)
        except Exception as error:
            raise SpecialistError(_failure_code(error)) from None
        usage = completion.usage
        self.last_usage = usage if isinstance(usage, Mapping) else None
        values = completion.output
        if not isinstance(values, Mapping):
            raise SpecialistError("answer_not_structured")
        return values


def _failure_code(error: Exception) -> str:
    """Report the failure without carrying the document into the exception."""
    code = getattr(error, "code", None)
    if isinstance(code, str) and code.strip():
        return code
    return type(error).__name__


def json_schema(
    properties: Mapping[str, str], required: tuple[str, ...]
) -> dict[str, Any]:
    """Build a strict flat answer schema from field names and types."""
    return {
        "type": "object",
        "properties": {
            name: {"type": kind} for name, kind in properties.items()
        },
        "required": list(required),
        "additionalProperties": False,
    }
