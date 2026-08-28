"""Compose the Core reasoner from approved repository identity documents."""

from __future__ import annotations

from pathlib import Path

from alx.contracts import ReasoningModel
from alx.core import ModelReasoner


def build_model_reasoner(model: ReasoningModel, repository_root: Path) -> ModelReasoner:
    laws = (repository_root / "LAWS_OF_ALX.md").read_text(encoding="utf-8")
    identity = (repository_root / "IDENTITY_AND_MEMORY.md").read_text(encoding="utf-8")
    return ModelReasoner(model, laws, identity)
