"""Compose merge authority, or leave it unavailable entirely.

Returning None leaves the capability unregistered, so AL/X cannot propose a
merge at all. That is the difference between the authority being withheld and
merging merely failing: an unregistered capability is honestly absent.

Friedl grants this authority by configuring it and revokes it by removing the
configuration or the permission. There is no per-merge approval: the delegation
is the decision, recorded in governance, and each merge is AL/X exercising it.
"""

from __future__ import annotations

import logging
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Any

from alx.contracts import CapabilityDefinition, CapabilityResult, StructuredData
from alx.contracts.repository import MergeRequest
from alx.providers.github_merge import GitHubMergeProvider
from alx.safety import AuthorityPolicy
from alx.tools.repository import (
    DEFINITION as MERGE_DEFINITION,
    MERGE_PULL_REQUEST,
    build_repository_executors,
)


LOGGER = logging.getLogger(__name__)

# Merging is its own authority. Holding it follows from no other permission,
# and no other permission follows from it.
REPOSITORY_MERGE_PERMISSION = "repository.merge"


@dataclass(frozen=True, slots=True)
class RepositoryRuntime:
    """The one merge capability, or nothing at all."""

    provider: Any
    definitions: tuple[CapabilityDefinition, ...]
    policies: Mapping[str, AuthorityPolicy]
    executors: Mapping[str, Callable[[StructuredData], CapabilityResult]]
    permissions: frozenset[str]


def build_repository_runtime(
    enabled: bool,
    repository: str,
    token: str,
    call_id_source: Callable[[], str],
    provider: Any = None,
) -> RepositoryRuntime | None:
    """Compose merge authority, or leave it unregistered."""
    if not enabled:
        LOGGER.info("Merge authority is not enabled: no merge capability")
        return None
    if not repository.strip() or not token.strip():
        LOGGER.info("Merge authority is not configured: no merge capability")
        return None

    try:
        selected = provider or GitHubMergeProvider(repository, token)
    except ValueError:
        LOGGER.warning("Merge authority is misconfigured: no merge capability")
        return None

    def merge(request: MergeRequest) -> Any:
        return selected.merge(request)

    LOGGER.info("Merge authority enabled: %s", MERGE_PULL_REQUEST)
    return RepositoryRuntime(
        provider=selected,
        definitions=(MERGE_DEFINITION,),
        policies={
            # No per-merge approval. Friedl delegated routine merge
            # authorisation in governance rather than participating in each
            # decision, so the permission is the authority and the judgement
            # about a specific reviewed head is AL/X's. Revoking the permission
            # revokes the delegation.
            MERGE_PULL_REQUEST: AuthorityPolicy(
                frozenset({REPOSITORY_MERGE_PERMISSION}),
                approval_required=False,
            ),
        },
        executors=build_repository_executors(merge, call_id_source),
        permissions=frozenset({REPOSITORY_MERGE_PERMISSION}),
    )
