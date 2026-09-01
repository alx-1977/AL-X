"""Compose the research notebook into the existing AL/X boundaries.

The notebook is storage AL/X reaches through the one capability broker, exactly
like mail or Xero. Nothing here starts work, schedules anything, or spends
money: opening a thread records a question, it does not begin researching it.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping

from alx.contracts import CapabilityDefinition, CapabilityResult, StructuredData
from alx.research import SQLiteResearchStore
from alx.safety import AuthorityPolicy
from alx.tools import (
    CORRECT_RESEARCH_ENTRY,
    DELETE_RESEARCH,
    NOTEBOOK_DEFINITIONS,
    OPEN_RESEARCH_THREAD,
    READ_RESEARCH_THREAD,
    RECORD_RESEARCH_ENTRY,
    REVISE_RESEARCH_ENTRY,
    SEARCH_RESEARCH,
    SET_RESEARCH_STATUS,
    build_notebook_executors,
)


# Reading her own research needs no more authority than thinking does.
RESEARCH_READ_PERMISSION = "research.read"
# Writing research is durable but private and reversible by revision.
RESEARCH_WRITE_PERMISSION = "research.write"
# Deleting is irreversible, so it is a separate authority that AL/X does not
# hold. Only Friedl deletes research, and the gate refuses it otherwise.
RESEARCH_DELETE_PERMISSION = "research.delete"


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
    clock: Any = None,
) -> NotebookRuntime:
    store = SQLiteResearchStore(storage_root / "research.sqlite3")
    policies = {
        OPEN_RESEARCH_THREAD: AuthorityPolicy(
            frozenset({RESEARCH_WRITE_PERMISSION})
        ),
        RECORD_RESEARCH_ENTRY: AuthorityPolicy(
            frozenset({RESEARCH_WRITE_PERMISSION})
        ),
        REVISE_RESEARCH_ENTRY: AuthorityPolicy(
            frozenset({RESEARCH_WRITE_PERMISSION})
        ),
        SET_RESEARCH_STATUS: AuthorityPolicy(
            frozenset({RESEARCH_WRITE_PERMISSION})
        ),
        SEARCH_RESEARCH: AuthorityPolicy(frozenset({RESEARCH_READ_PERMISSION})),
        READ_RESEARCH_THREAD: AuthorityPolicy(
            frozenset({RESEARCH_READ_PERMISSION})
        ),
        # Correcting research is Friedl's, and it preserves what was there
        # before, so it is gated by approval rather than left to the model.
        CORRECT_RESEARCH_ENTRY: AuthorityPolicy(
            frozenset({RESEARCH_WRITE_PERMISSION}), approval_required=True
        ),
        # Deletion destroys content permanently. It requires an authority the
        # runtime does not grant, so AL/X cannot delete her own research and
        # cannot delete Friedl's corrections to it.
        DELETE_RESEARCH: AuthorityPolicy(
            frozenset({RESEARCH_DELETE_PERMISSION}), approval_required=True
        ),
    }
    return NotebookRuntime(
        store=store,
        definitions=NOTEBOOK_DEFINITIONS,
        policies=policies,
        executors=build_notebook_executors(
            store, retention_days, call_id_source, clock
        ),
        # Deletion is deliberately absent: granting it here would hand AL/X the
        # one notebook action that cannot be undone.
        permissions=frozenset(
            {RESEARCH_READ_PERMISSION, RESEARCH_WRITE_PERMISSION}
        ),
    )
