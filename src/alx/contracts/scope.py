"""Durable contextual coordinates for AL/X's records.

A scope says *where* a record belongs, never *what* it means. It is a
coordinate the Core may attach so that later cognition can narrow or annotate
context, and it carries no reasoning, no behaviour and no content of its own.

## Why a value object rather than a bare column

`ContentProvenance` solved this shape already: one cross-cutting fact, needed
by several unrelated stores, expressed as a value object in `contracts` with
encode/decode helpers, while each store keeps its own small column plumbing.
`ScopeReference` follows that precedent deliberately. A second scope dimension
later extends this one object and its two helpers; it does not require every
store to reinvent scoping for itself.

The object is a single nullable column rather than one column per dimension,
so adding a dimension is a contract change and a decode change, not another
`ALTER TABLE` in every store that ever holds a scope.

## What a Project is, and is not

A Project is identity and lifecycle: an id, a name a person can read, a status
and provenance. That is the whole of it.

It is **not** a container. Memories, goals, decisions, evidence and research
entries continue to live in their own authoritative primitives and are merely
*labelled* with a scope. Nothing is moved into a project, and a project holds
no durable knowledge of its own, because a project that accumulated content
would be a second memory system with none of memory's supersession, provenance
or isolation guarantees.

It is **not** an owner of cognition. Memory remains platform-wide: a scope is
optional everywhere, cross-project and unscoped records stay ordinary, and
nothing here makes a project the container for AL/X's context.

It is **not** inferred. Deterministic code may never decide that a record
belongs to a project by reading its wording; that is a judgement, and under
Law 1 it belongs to the Core. This module therefore offers no classifier, no
matcher and no defaulting rule — a scope is attached explicitly or not at all.

## Scope is a label, never an authority boundary

A scope must never be mistaken for a permission. Person isolation, approval
scope, retention and supersession remain the deterministic boundaries they
already are, and a scope neither widens nor narrows any of them. Relationship
memory is isolated by `person_id` alone, exactly as before.

This matters most for retrieval that does not exist yet: when similarity is
eventually available, exact filters stay authoritative and similarity may only
order what those filters already allowed. Nothing here may become the thing
that lets a match cross a person boundary.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import Enum
import json
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from alx.contracts.provenance import ContentProvenance


def _required(value: str, name: str) -> None:
    if not value.strip():
        raise ValueError(f"{name} must not be blank")


def _aware(value: datetime, name: str) -> None:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{name} must be timezone aware")


class ProjectStatus(str, Enum):
    """A project's lifecycle, which is about the work, not about the records.

    Archiving a project says the work is no longer current. It says nothing
    about the truth or reachability of records scoped to it, which keep their
    own retention and supersession.
    """

    ACTIVE = "active"
    ARCHIVED = "archived"


@dataclass(frozen=True, slots=True)
class Project:
    """One durable contextual coordinate: identity, a readable name, lifecycle.

    Deliberately thin. Anything that looks like project *knowledge* belongs in
    memories, goals, evidence or research entries, which already carry
    provenance and supersession this record would have to reimplement badly.
    """

    project_id: str
    name: str
    created_at: datetime
    status: ProjectStatus = ProjectStatus.ACTIVE
    provenance: ContentProvenance | None = None

    def __post_init__(self) -> None:
        _required(self.project_id, "project_id")
        _required(self.name, "name")
        _aware(self.created_at, "created_at")
        if not isinstance(self.status, ProjectStatus):
            raise TypeError("status must be a ProjectStatus")
        if self.provenance is not None:
            from alx.contracts.provenance import ContentProvenance

            if not isinstance(self.provenance, ContentProvenance):
                raise TypeError("project provenance must be ContentProvenance or None")


@dataclass(frozen=True, slots=True)
class ScopeReference:
    """Where a durable record belongs, as coordinates the Core chose.

    One dimension exists today. The object is the extension point: a later
    dimension is a field here, decoded by the same helper, rather than a new
    column in every store that holds a scope.

    An empty scope is not a scope. A record with nothing to say about where it
    belongs stores `None`, so "unscoped" stays exactly one representation and
    no store has to treat an empty object and a null column as the same thing.
    """

    project_id: str | None = None

    def __post_init__(self) -> None:
        if self.project_id is not None:
            _required(self.project_id, "project_id")
        if not self.dimensions:
            raise ValueError("a scope reference must name at least one dimension")

    @property
    def dimensions(self) -> tuple[str, ...]:
        """Every dimension this scope actually names, for stores and tests."""
        return tuple(
            name
            for name, value in (("project_id", self.project_id),)
            if value is not None
        )


def scope_to_storage(scope: ScopeReference | None) -> str | None:
    """Encode a scope into the one nullable column every store uses."""
    if scope is None:
        return None
    return json.dumps({"project_id": scope.project_id}, separators=(",", ":"))


def scope_from_storage(value: str | None) -> ScopeReference | None:
    """Decode one store row, leaving an unscoped legacy row unscoped.

    A row written before scopes existed decodes to `None` rather than to an
    empty scope, which is what keeps every pre-existing record valid and
    unchanged.
    """
    if value is None:
        return None
    data = json.loads(value)
    if not isinstance(data, dict):
        raise ValueError("stored scope must be an object")
    project_id = data.get("project_id")
    if project_id is not None and not isinstance(project_id, str):
        raise ValueError("stored project_id must be a string")
    # A stored object naming no dimension is corrupt rather than unscoped:
    # unscoped is a null column, and silently accepting it would create a
    # second representation of the same fact.
    return ScopeReference(project_id=project_id)
