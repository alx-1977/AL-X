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
    spend_authority=None,
) -> ModelReasoner:
    """Build a reasoner over the approved Laws and identity.

    Every reasoner reads the same two approved documents, so a second Core
    built here is the same AL/X over a different model, never a different mind.
    The two bounds and the spend authority are one thing, not three options:
    conversation passes none of them, and a path spending against a dollar
    ceiling passes all of them. ModelReasoner refuses a partial combination,
    so a bounded reasoner that could dispatch without withdrawing anything
    cannot be built at all.
    """
    laws = (repository_root / "LAWS_OF_ALX.md").read_text(encoding="utf-8")
    identity = (repository_root / "IDENTITY_AND_MEMORY.md").read_text(encoding="utf-8")
    return ModelReasoner(
        model, laws, identity, max_output_tokens, max_input_tokens,
        spend_authority,
    )
