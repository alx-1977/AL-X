"""Compose branch publication and pull-request opening, or leave them absent.

Returning None leaves both capabilities unregistered, so AL/X cannot publish at
all. That is the difference between the authority being withheld and publishing
merely failing.

These are AL/X's, never the Coding Agent's. A job commits inside the worktree it
was given and cannot push by construction; whether that work is published is a
separate decision, made after she has seen what the job produced. Composing them
here rather than inside the coding runtime is what keeps those two authorities
apart.
"""

from __future__ import annotations

import logging
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from alx.contracts import CapabilityDefinition, CapabilityResult, StructuredData
from alx.contracts.publication import PublicationError
from alx.providers.github_pull_request import GitHubPullRequests
from alx.providers.repository_publication import RepositoryPublication
from alx.safety import AuthorityPolicy
from alx.tools.publication import (
    DEFINITIONS,
    OPEN_PULL_REQUEST,
    PUBLISH_REPAIR_BRANCH,
    build_publication_executors,
)

LOGGER = logging.getLogger(__name__)

PUBLISH_PERMISSION = "repository.publish"


@dataclass(frozen=True, slots=True)
class PublicationRuntime:
    """The two publication capabilities, or nothing at all."""

    definitions: tuple[CapabilityDefinition, ...]
    policies: Mapping[str, AuthorityPolicy]
    executors: Mapping[str, Callable[[StructuredData], CapabilityResult]]
    permissions: frozenset[str]


def build_publication_runtime(
    enabled: bool,
    repository: str,
    token: str,
    checkout: Path | None,
    call_id_source: Callable[[], str],
    publication: Any = None,
    pull_requests: Any = None,
) -> PublicationRuntime | None:
    """Compose publishing, or leave it unregistered."""
    if not enabled:
        LOGGER.info("Publication is not enabled: no publish capability")
        return None
    if not repository.strip() or not token.strip() or checkout is None:
        LOGGER.info("Publication is not configured: no publish capability")
        return None

    try:
        branches = publication or RepositoryPublication(checkout)
        pulls = pull_requests or GitHubPullRequests(repository, token)
    except (TypeError, ValueError):
        LOGGER.warning("Publication is misconfigured: no publish capability")
        return None

    # The branch is pushed to the checkout's own `origin`, and the pull request
    # is opened against a separately configured repository. Nothing but this
    # comparison keeps those the same place: with two settings that can drift,
    # a repair could be pushed to one repository and proposed in another, where
    # the branch does not exist. The push would succeed and the work would sit
    # somewhere nobody reviews.
    #
    # Checked here, once, rather than at each publication: a mismatch is a
    # misconfiguration, and the honest response is to withhold the capability
    # rather than to offer one that cannot complete. An origin that cannot be
    # identified is refused for the same reason — it is not proof of a match.
    # Reading the origin runs git, which can fail for reasons that have
    # nothing to do with this decision: a missing binary, an unreadable
    # checkout, a timeout. Those are answered the way every other failure here
    # is — the capability is withheld and AL/X starts. Letting it escape made
    # an unreadable origin stop the whole composition root, so a publication
    # problem became no AL/X at all.
    try:
        identity = branches.origin_identity()
    except PublicationError as error:
        LOGGER.warning(
            "Publication origin could not be read (%s): no publish capability",
            error.code,
        )
        return None
    expected = repository.strip().lower()
    if not identity:
        LOGGER.warning(
            "Publication checkout has no identifiable GitHub origin: "
            "no publish capability"
        )
        return None
    if identity != expected:
        LOGGER.warning(
            "Publication checkout publishes to %s but pull requests target %s: "
            "no publish capability",
            identity,
            expected,
        )
        return None

    LOGGER.info("Publication enabled: %s, %s", PUBLISH_REPAIR_BRANCH, OPEN_PULL_REQUEST)
    # No per-publication approval. Publishing proposes work for review rather
    # than changing anything Friedl relies on: the branch is not main, the
    # remote is never overwritten, and nothing merges. The permission is the
    # authority, and what is worth publishing is her judgement — the same shape
    # as the merge delegation under D-026.
    policy = AuthorityPolicy(
        permission_references=frozenset({PUBLISH_PERMISSION}),
        approval_required=False,
    )
    return PublicationRuntime(
        definitions=DEFINITIONS,
        policies={
            PUBLISH_REPAIR_BRANCH: policy,
            OPEN_PULL_REQUEST: policy,
        },
        executors=build_publication_executors(branches, pulls, call_id_source),
        permissions=frozenset({PUBLISH_PERMISSION}),
    )


__all__ = [
    "PUBLISH_PERMISSION",
    "PublicationRuntime",
    "build_publication_runtime",
]
