"""Compose AL/X's repository authority, or leave it absent.

Returning None leaves the capability unregistered, so she cannot act on the
repository at all. That is the difference between the authority being withheld
and every operation failing.

This is AL/X's, never the Coding Agent's. A job commits inside the worktree it
was given and cannot reach a remote by construction; what is published, merged,
reset or deleted is decided after she has seen what the job produced. Composing
this here rather than inside the coding runtime is what keeps those authorities
apart.
"""

from __future__ import annotations

import logging
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from alx.contracts import CapabilityDefinition, CapabilityResult, StructuredData
from alx.contracts.repository_authority import CanonicalSystem
from alx.providers.github_pull_request import GitHubPullRequests
from alx.providers.repository_authority import RepositoryAuthority
from alx.safety import AuthorityPolicy
from alx.tools.repository_authority import (
    DEFINITIONS,
    REPOSITORY_OPERATION,
    build_repository_operation_executors,
)

LOGGER = logging.getLogger(__name__)

REPOSITORY_PERMISSION = "repository.operate"


@dataclass(frozen=True, slots=True)
class RepositoryAuthorityRuntime:
    """The one repository capability, or nothing at all."""

    definitions: tuple[CapabilityDefinition, ...]
    policies: Mapping[str, AuthorityPolicy]
    executors: Mapping[str, Callable[[StructuredData], CapabilityResult]]
    permissions: frozenset[str]


def build_repository_authority_runtime(
    enabled: bool,
    root: Path | None,
    repository_identity: str,
    timeout_seconds: int,
    call_id_source: Callable[[], str],
    github_token: str = "",
    authority: Any = None,
) -> RepositoryAuthorityRuntime | None:
    """Compose repository authority, or leave it unregistered."""
    if not enabled:
        LOGGER.info("Repository authority is not enabled: no repository capability")
        return None
    if root is None or not repository_identity.strip():
        LOGGER.info("Repository authority is not configured: no repository capability")
        return None

    try:
        # The canonical system is configured, never inferred. Working out which
        # repository AL/X is from the current directory would mean a wrong
        # working directory silently disables the one protection that matters.
        system = CanonicalSystem(root, repository_identity.strip())
        # The pull request is where the work is proposed, reviewed and
        # answered, so the GitHub side belongs to the same authority. Without a
        # token it is absent, and those operations report that rather than
        # appearing in the catalogue and failing as unusable arguments.
        pull_requests = None
        if github_token.strip():
            try:
                pull_requests = GitHubPullRequests(
                    system.repository, github_token.strip()
                )
            except (TypeError, ValueError):
                LOGGER.warning(
                    "GitHub is misconfigured: pull-request operations unavailable"
                )
        else:
            LOGGER.info("No GitHub token: pull-request operations unavailable")
        # The origin is read before the authority is built, so the object is
        # constructed once knowing whether the remote is confirmed rather than
        # being adjusted afterwards.
        probe = authority or RepositoryAuthority(system, timeout_seconds)
        verified_remote = ""
        try:
            identity = probe.origin_identity()
            # The URL behind that identity, kept so the push names it rather
            # than the mutable `origin`.
            verified_remote = probe.origin_url()
        except Exception as error:  # noqa: BLE001 - reading git may fail many ways
            LOGGER.warning(
                "Repository origin could not be read (%s): local operations only",
                type(error).__name__,
            )
            identity = ""
        expected = system.repository.strip().lower()
        remote_verified = bool(identity) and identity == expected
        if not remote_verified:
            # Local work is still hers — inspecting history, committing,
            # branching, resetting a feature branch — and withholding all of it
            # would leave her unable to look at a repository merely because its
            # remote is unusual. What is withheld is everything that reaches the
            # remote: work must not travel to a repository nobody has confirmed
            # is this one.
            LOGGER.warning(
                "Repository checkout is %s but AL/X is configured as %s: "
                "local operations only",
                identity or "unidentifiable",
                expected,
            )
        selected = authority or RepositoryAuthority(
            system, timeout_seconds,
            pull_requests=pull_requests,
            remote_verified=remote_verified,
            verified_remote=verified_remote if remote_verified else "",
        )
    except (TypeError, ValueError) as error:
        LOGGER.warning(
            "Repository authority is misconfigured (%s): no repository capability",
            type(error).__name__,
        )
        return None

    LOGGER.info(
        "Repository authority enabled: %s (canonical %s at %s)",
        REPOSITORY_OPERATION,
        system.repository,
        system.root,
    )
    # No per-operation approval. This is the authority decision itself: AL/X
    # manages her repositories the way an engineer does, and asking permission
    # for each operation would be the narrow model this replaces, wearing a
    # different shape. What she may not do is enforced deterministically in the
    # provider rather than by a person answering prompts.
    policy = AuthorityPolicy(
        permission_references=frozenset({REPOSITORY_PERMISSION}),
        approval_required=False,
    )
    return RepositoryAuthorityRuntime(
        definitions=DEFINITIONS,
        policies={REPOSITORY_OPERATION: policy},
        executors=build_repository_operation_executors(selected, call_id_source),
        permissions=frozenset({REPOSITORY_PERMISSION}),
    )


__all__ = [
    "REPOSITORY_PERMISSION",
    "RepositoryAuthorityRuntime",
    "build_repository_authority_runtime",
]
