"""Compose AL/X's one durable research-notebook capability path."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

from alx.contracts import CapabilityDefinition, CapabilityResult, StructuredData
from alx.research import SQLiteResearchStore
from alx.safety import AuthorityPolicy
from alx.tools import (
    CORRECT_RESEARCH_ENTRY,
    DELETE_RESEARCH,
    NOTEBOOK_DEFINITIONS,
    build_notebook_executors,
)


NOTEBOOK_PERMISSION = "research.notebook"


@dataclass(frozen=True, slots=True)
class NotebookRuntime:
    store: SQLiteResearchStore
    definitions: tuple[CapabilityDefinition, ...]
    policies: Mapping[str, AuthorityPolicy]
    executors: Mapping[str, Callable[[StructuredData], CapabilityResult]]
    permissions: frozenset[str]


def build_notebook_runtime(
    storage_root: Path,
    retention_days: int,
    call_id_source: Callable[[], str],
    provenance_of: Callable[[tuple[str, ...], datetime], Any] | None = None,
) -> NotebookRuntime:
    """Build storage and bind every live notebook operation to its primitives."""
    store = SQLiteResearchStore(storage_root / "research-notebook.sqlite3")
    policies = {
        definition.capability_id: AuthorityPolicy(
            frozenset({NOTEBOOK_PERMISSION}),
            approval_required=definition.capability_id in {
                CORRECT_RESEARCH_ENTRY,
                DELETE_RESEARCH,
            },
        )
        for definition in NOTEBOOK_DEFINITIONS
    }
    return NotebookRuntime(
        store,
        NOTEBOOK_DEFINITIONS,
        policies,
        build_notebook_executors(
            store,
            retention_days,
            call_id_source,
            provenance_of=provenance_of,
        ),
        frozenset({NOTEBOOK_PERMISSION}),
    )
