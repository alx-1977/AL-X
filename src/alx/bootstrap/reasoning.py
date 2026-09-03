"""Compose the Core reasoner from approved repository identity documents."""

from __future__ import annotations

from pathlib import Path

from alx.contracts import CognitionOrigin, ReasoningModel
from alx.core import ModelReasoner


def build_model_reasoner(
    model: ReasoningModel,
    repository_root: Path,
    max_output_tokens: int | None = None,
    max_input_tokens: int | None = None,
) -> ModelReasoner:
    """Build a reasoner over the approved Laws and identity.

    Every reasoner reads the same two approved documents, so a second Core
    built here is the same AL/X over a different model, never a different mind.
    The two bounds are the ceilings a reservation is computed against, so both
    travel together: conversation passes neither, and a path spending against a
    dollar ceiling passes both. Passing only one would leave a reservation
    resting on a bound nothing enforces.
    """
    laws = (repository_root / "LAWS_OF_ALX.md").read_text(encoding="utf-8")
    identity = (repository_root / "IDENTITY_AND_MEMORY.md").read_text(encoding="utf-8")
    return ModelReasoner(
        model, laws, identity, max_output_tokens, max_input_tokens
    )


class AutonomousReasonerUnavailable(Exception):
    """An autonomous turn arrived with no autonomous Core configured.

    Refused before any provider call. Answering it with the conversational
    model would spend money on the wrong Core and would make the recorded
    experiment a measurement of something nobody chose.
    """

    def __init__(self, origin: str) -> None:
        self.origin = origin
        super().__init__(
            f"no autonomous reasoner is configured for a {origin} turn"
        )


class OriginSelectedReasoner:
    """Two Cores, chosen by where the turn came from. Nothing else.

    Recorded under D-024a as a time-boxed experiment, not architecture. It
    exists to compare one model answering Friedl with a stronger one thinking
    unprompted, and it concludes with Friedl's deliberate decision.

    The selection below is the whole mechanism: one expression over
    `CognitionOrigin.is_autonomous`. There is deliberately no table, registry,
    strategy object or per-turn choice, because each of those is the shape a
    model router takes, and a router keyed on anything semantic would be a
    second authority deciding which AL/X shows up before she has reasoned at
    all.

    Both reasoners are built from identical Laws, identity, catalogue,
    contracts, stores, broker and gate. Only provider, model, effort and output
    bound differ. `CoreAgent`, the broker and the gate are never told which one
    answered, and nothing downstream can find out.
    """

    def __init__(
        self,
        conversational: ModelReasoner,
        autonomous: ModelReasoner | None = None,
    ) -> None:
        self._conversational = conversational
        # None when the experiment is unconfigured. The boundary still exists:
        # an autonomous turn is refused rather than quietly answered by the
        # conversational model, because a silent fallback would run the
        # experiment on the wrong Core and record results as if it had not.
        self._autonomous = autonomous

    def decide(self, context):
        # The one expression. It reads `origin` and nothing else: not the
        # goal, the notebook, the memories, the capabilities, the conversation,
        # nor any content of the turn.
        if context.origin.is_autonomous:
            if self._autonomous is None:
                raise AutonomousReasonerUnavailable(context.origin.value)
            return self._autonomous.decide(context)
        return self._conversational.decide(context)
