"""Compose reading of GitHub check results, or leave it unavailable.

Returning None leaves the capability unregistered, so AL/X cannot propose the
read at all. That is the difference between the read being withheld and the
read merely failing.

The repository and token are the ones merge and review already use. This
does not consult their enable switches: a runtime that can name the
repository and authenticate can read checks without being allowed to merge
or to request a review. Holding this permission grants neither of those.
"""

from __future__ import annotations

import logging
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Any

from alx.contracts import CapabilityDefinition, CapabilityResult, StructuredData
from alx.providers.github_checks import GitHubPullRequestChecks
from alx.safety import AuthorityPolicy
from alx.tools.pull_request_checks import (
    DEFINITION as CHECKS_DEFINITION,
    READ_PULL_REQUEST_CHECKS,
    build_pull_request_checks_executors,
)


LOGGER = logging.getLogger(__name__)

# Reading checks is its own authority. It grants no merge and no review
# request, and neither of those grants this.
CHECKS_READ_PERMISSION = "checks.read"


@dataclass(frozen=True, slots=True)
class PullRequestChecksRuntime:
    """The one check-read capability, or nothing at all."""

    provider: Any
    definitions: tuple[CapabilityDefinition, ...]
    policies: Mapping[str, AuthorityPolicy]
    executors: Mapping[str, Callable[[StructuredData], CapabilityResult]]
    permissions: frozenset[str]


def build_pull_request_checks_runtime(
    repository: str,
    token: str,
    call_id_source: Callable[[], str],
    provider: Any = None,
) -> PullRequestChecksRuntime | None:
    """Compose the check read, or leave it unregistered."""
    if not repository.strip() or not token.strip():
        LOGGER.info("Pull request checks are not configured: no capability")
        return None
    if provider is None:
        try:
            provider = GitHubPullRequestChecks(repository, token)
        except ValueError:
            LOGGER.warning(
                "Pull request checks are misconfigured: no capability"
            )
            return None

    LOGGER.info("Pull request check reading enabled: %s", READ_PULL_REQUEST_CHECKS)
    return PullRequestChecksRuntime(
        provider=provider,
        definitions=(CHECKS_DEFINITION,),
        policies={
            # Plain permission. The read writes nothing, so asking Friedl
            # each time would be a question with one answer. It is not a
            # standing approval, and it is not merge or review authority.
            READ_PULL_REQUEST_CHECKS: AuthorityPolicy(
                frozenset({CHECKS_READ_PERMISSION}),
                approval_required=False,
            ),
        },
        executors=build_pull_request_checks_executors(
            provider.read, call_id_source
        ),
        permissions=frozenset({CHECKS_READ_PERMISSION}),
    )


__all__ = [
    "CHECKS_READ_PERMISSION",
    "PullRequestChecksRuntime",
    "build_pull_request_checks_runtime",
]
