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
# Deleting is irreversible, so it stays a separate authority from writing.
# AL/X holds it because deciding a line of enquiry is no longer worth keeping is
# part of thinking, not an administrative act. What she cannot do is carry it
# out alone: the gate stops every deletion for Friedl's explicit approval.
RESEARCH_DELETE_PERMISSION = "research.delete"

# Notebook approvals expire on their own clock. Inheriting a window from mail or
# Xero meant a runtime configured for neither had no expiry at all, so an
# approval could stay valid indefinitely. 600 seconds is the value both existing
# gated capabilities use, so an approval-gated notebook action expires exactly as
# a bill write or a mail send does.
NOTEBOOK_APPROVAL_TTL_SECONDS = 600


@dataclass(frozen=True, slots=True)
class NotebookRuntime:
    store: SQLiteResearchStore
    approval_ttl_seconds: int
    definitions: tuple[CapabilityDefinition, ...]
    policies: Mapping[str, AuthorityPolicy]
    executors: Mapping[str, Callable[[StructuredData], CapabilityResult]]
    permissions: frozenset[str]


def build_notebook_runtime(
    storage_root: Path,
    retention_days: int,
    call_id_source: Callable[[], str],
    clock: Any = None,
    provenance_of: Any = None,
    approval_ttl_seconds: int = NOTEBOOK_APPROVAL_TTL_SECONDS,
) -> NotebookRuntime:
    if approval_ttl_seconds <= 0:
        raise ValueError(
            "notebook approvals require a finite positive TTL; a permanent "
            "approval would let one consent authorise a later deletion"
        )
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
        # Deletion destroys content permanently. AL/X may propose it, but
        # every deletion needs Friedl's approval for that exact record.
        # standing_scope_allowed stays false deliberately: a standing grant
        # would let one approval authorise later deletions she proposes
        # herself, which is the whole thing this gate exists to prevent.
        DELETE_RESEARCH: AuthorityPolicy(
            frozenset({RESEARCH_DELETE_PERMISSION}), approval_required=True
        ),
    }
    return NotebookRuntime(
        store=store,
        approval_ttl_seconds=approval_ttl_seconds,
        definitions=NOTEBOOK_DEFINITIONS,
        policies=policies,
        executors=build_notebook_executors(
            store, retention_days, call_id_source, clock, provenance_of
        ),
        # Deletion is granted so AL/X can decide research is no longer worth
        # keeping. The approval requirement above, not a withheld permission,
        # is what stops her acting on that decision unilaterally.
        permissions=frozenset(
            {
                RESEARCH_READ_PERMISSION,
                RESEARCH_WRITE_PERMISSION,
                RESEARCH_DELETE_PERMISSION,
            }
        ),
    )
