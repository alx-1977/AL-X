"""A bounded question put to a model, deliberately without AL/X's world.

AL/X's reasoning call carries her laws, identity, capability catalogue, goal
state and conversation, because she is deciding what to do. Reading fields off
an invoice needs none of that, and routing it through the Core cost roughly ten
times what the question is worth.

A specialist answers one narrowly defined question against one document and
stops. It holds no goal, memory, personality, catalogue or authority; it cannot
choose an action, call a capability, or continue anything. It returns structured
data to AL/X or to deterministic code, which decide what the answer means.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping, Protocol

from alx.contracts.records import StructuredData


class SpecialistError(Exception):
    """A sanitised specialist failure carrying no document content."""

    def __init__(self, code: str) -> None:
        if not isinstance(code, str) or not code.strip():
            raise ValueError("code must not be blank")
        self.code = code
        super().__init__(code)


@dataclass(frozen=True, slots=True)
class SpecialistQuestion:
    """One bounded question, its material, and the shape of the answer."""

    question_id: str
    instruction: str
    material: str
    answer_schema: StructuredData
    # A specialist reads a bounded amount of one document. An unbounded prompt
    # would reintroduce the cost this exists to avoid.
    material_limit: int = 6000

    def __post_init__(self) -> None:
        for name in ("question_id", "instruction", "material"):
            value = getattr(self, name)
            if not isinstance(value, str) or not value.strip():
                raise ValueError(f"{name} must not be blank")
        if not isinstance(self.answer_schema, Mapping) or not self.answer_schema:
            raise ValueError("answer_schema must be a non-empty schema")
        if self.material_limit <= 0:
            raise ValueError("material_limit must be positive")

    @property
    def bounded_material(self) -> str:
        return self.material[: self.material_limit]

    @property
    def material_omitted_characters(self) -> int:
        """How much of the source `bounded_material` leaves out.

        A specialist reads a bounded amount of one document, so the limit is
        deliberate. What is not deliberate is a caller being unable to tell
        that it applied: an answer read from the first part of a document is
        weaker evidence than an answer read from all of it, and only the
        reader can judge whether that matters.
        """
        return max(0, len(self.material) - self.material_limit)


class SpecialistModel(Protocol):
    """Answers one bounded question and returns structured data."""

    def answer(self, question: SpecialistQuestion) -> Mapping[str, Any]: ...
