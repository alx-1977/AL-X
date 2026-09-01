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
    ModelMessage,
    ModelRequest,
    ModelRole,
    ReasoningModel,
    SpecialistError,
    SpecialistQuestion,
)


class ModelSpecialist:
    """Answer one ordinary bounded extraction question through one model."""

    def __init__(
        self,
        model: ReasoningModel,
        cache_key: str = "alx-specialist-v1",
    ) -> None:
        self._model = model
        self._cache_key = cache_key
        # Usage from the most recent answer, so a caller enforcing a spending
        # ceiling can price what the call actually consumed. It is measurement,
        # not state: nothing reads it to decide anything.
        self.last_usage: Mapping[str, Any] | None = None

    def answer(
        self,
        question: SpecialistQuestion,
        max_output_tokens: int | None = None,
    ) -> Mapping[str, Any]:
        if not isinstance(question, SpecialistQuestion):
            raise TypeError("question must be an ordinary SpecialistQuestion")
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
            max_output_tokens,
            kind="specialist",
        )
        self.last_usage = None
        try:
            completion = self._model.complete(request)
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
