"""Trusted scope for a delegated presentation edit, outside model arguments."""

from contextvars import ContextVar
from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class PresentationEditScope:
    work_copy_id: str
    work_copy_revision: str


presentation_edit_scope: ContextVar[PresentationEditScope | None] = ContextVar(
    "presentation_edit_scope", default=None,
)
