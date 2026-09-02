"""Compose AL/X's one durable future-cognition path."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path

from alx.contracts import CapabilityDefinition, CapabilityResult, StructuredData
from alx.continuity import SQLiteContinuityStore
from alx.safety import AuthorityPolicy
from alx.tools import CONTINUITY_DEFINITIONS, build_continuity_executors

CONTINUITY_PERMISSION = "continuity.future_cognition"


@dataclass(frozen=True, slots=True)
class ContinuityRuntime:
    store: SQLiteContinuityStore
    definitions: tuple[CapabilityDefinition, ...]
    policies: Mapping[str, AuthorityPolicy]
    executors: Mapping[str, Callable[[StructuredData], CapabilityResult]]
    permissions: frozenset[str]


def build_continuity_runtime(
    storage_root: Path,
    retention_days: int,
    call_id_source: Callable[[], str],
) -> ContinuityRuntime:
    """Build storage and bind both future-cognition primitives.

    Asking for a later occasion changes nothing outside AL/X, so neither
    capability requires approval. They grant no external authority: the most a
    request can do is cause her to be invoked again, which is what D-024
    authorises.
    """
    store = SQLiteContinuityStore(storage_root / "continuity.sqlite3")
    policies = {
        definition.capability_id: AuthorityPolicy(
            frozenset({CONTINUITY_PERMISSION}), approval_required=False
        )
        for definition in CONTINUITY_DEFINITIONS
    }
    return ContinuityRuntime(
        store,
        CONTINUITY_DEFINITIONS,
        policies,
        build_continuity_executors(store, retention_days, call_id_source),
        frozenset({CONTINUITY_PERMISSION}),
    )
