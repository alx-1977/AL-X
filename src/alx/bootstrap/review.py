"""Compose external review requesting, or leave it unavailable entirely.

Returning None leaves the capability unregistered, so AL/X cannot request a
review at all. That is the difference between the capability being withheld and
requesting merely failing.

Which reviewer is composed is configuration. Every supported reviewer watches
this repository through GitHub and works the same way, so one provider serves
all of them and the profile supplies the three things that differ: the name,
the trigger comment, and the bot account. Changing the configured reviewer
changes nothing about the capability, the gate, the Core, or what AL/X asks
for.
"""

from __future__ import annotations

import logging
from collections.abc import Callable, Mapping
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from typing import Any

from alx.contracts import CapabilityDefinition, CapabilityResult, StructuredData
from alx.contracts.review import ReviewRequest
from alx.contracts.review_provider import ReviewProvider, profile_for
from alx.providers.github_review import GitHubReviewProvider
from alx.safety import AuthorityPolicy
from alx.tools.review import (
    DEFINITION as REVIEW_DEFINITION,
    REQUEST_EXTERNAL_REVIEW,
    build_review_executors,
)
from alx.tools.review_content import (
    DEFINITION as REVIEW_CONTENT_DEFINITION,
    READ_EXTERNAL_REVIEW,
    build_review_content_executors,
)


LOGGER = logging.getLogger(__name__)

# Requesting a review is its own authority. It grants no merge authority, and
# merge authority grants no ability to request a review.
REVIEW_REQUEST_PERMISSION = "review.request"

# Reading a review is a different authority from asking for one. Holding it
# grants no ability to request a review and none to merge: it permits reading
# something that already exists, and nothing else.
REVIEW_READ_PERMISSION = "review.read"

# The joined round's state when the reviewer had already finished it.
COMPLETED_ROUND = "success"
# Why a request returned without waiting: the head's review was finished and
# already delivered, and no new one will come for an unchanged head.
ALREADY_REVIEWED = "already_reviewed"


@dataclass(frozen=True, slots=True)
class ReviewRuntime:
    """The one review-request capability, or nothing at all."""

    provider: Any
    definitions: tuple[CapabilityDefinition, ...]
    policies: Mapping[str, AuthorityPolicy]
    executors: Mapping[str, Callable[[StructuredData], CapabilityResult]]
    permissions: frozenset[str]


def build_review_runtime(
    enabled: bool,
    repository: str,
    token: str,
    call_id_source: Callable[[], str],
    # Which reviewer to compose. Configuration, never a reasoning input: AL/X
    # asks for a review, not for a particular reviewer.
    reviewer: ReviewProvider | str = ReviewProvider.CODERABBIT,
    provider: Any = None,
    # Reads what a reviewer published. Injected like the requesting provider,
    # so another reviewer can replace it without changing the capability.
    content_provider: Any = None,
    # The sole bounded task waiter. Without it the capability is withheld;
    # an immediate-return request would restore Core-driven polling.
    started: Callable[[int, str, datetime], str] | None = None,
    # Whether an earlier task already consumed the verdict of this pull
    # request at this head. Supplied by the composition that holds the task
    # store; absent, a joined finished round is always waited on.
    delivered: Callable[[int, str, datetime], bool] | None = None,
) -> ReviewRuntime | None:
    """Compose review requesting, or leave it unregistered."""
    if started is None:
        LOGGER.info("No deterministic review waiter: review requesting unavailable")
        return None
    if not enabled:
        LOGGER.info("External review requesting is not enabled: no capability")
        return None
    if not repository.strip() or not token.strip():
        LOGGER.info("External review requesting is not configured: no capability")
        return None

    try:
        # The configured name becomes a provider here, where providers are
        # composed. An unrecognised one falls back rather than leaving review
        # silently unavailable: a typo should cost the default reviewer, not
        # the capability.
        if not isinstance(reviewer, ReviewProvider):
            try:
                reviewer = ReviewProvider(str(reviewer).strip().lower())
            except ValueError:
                LOGGER.warning(
                    "Unknown review provider %r: using %s",
                    reviewer,
                    ReviewProvider.CODERABBIT.value,
                )
                reviewer = ReviewProvider.CODERABBIT
        # One provider object requests and reads: both are GitHub calls about
        # the same pull request by the same reviewer, and splitting them would
        # mean two places that have to agree on which reviewer is configured.
        composed = GitHubReviewProvider(repository, token, profile_for(reviewer))
        selected = provider or composed
        reader = content_provider or composed
    except (KeyError, TypeError, ValueError):
        LOGGER.warning("External review requesting is misconfigured: no capability")
        return None

    def request_review(review: ReviewRequest) -> Any:
        # The provider reports GitHub's timestamp for the trigger comment. It
        # is the same clock used by result comments, so local clock skew cannot
        # exclude a fast result or admit one that predates this request. An
        # injected provider without that fact falls back to the pre-call time.
        local_requested_at = datetime.now(UTC)
        outcome = selected.request(review)
        requested_at = outcome.requested_at or local_requested_at
        # Joined a round that had already finished, and whose verdict an
        # earlier task already took. The reviewer does not review an unchanged
        # head again, so nothing further will arrive for a wait to observe;
        # waiting would only run out the clock. Say so now instead, and leave
        # the existing review to be read.
        if (
            delivered is not None
            and outcome.attached_round == COMPLETED_ROUND
            and outcome.head_sha
        ):
            try:
                consumed = delivered(
                    outcome.pull_request_number, outcome.head_sha, requested_at
                )
            except Exception as error:  # noqa: BLE001 - fall back to waiting
                LOGGER.warning(
                    "Review history unreadable: %s", type(error).__name__
                )
                consumed = False
            if consumed:
                return replace(outcome, wait_state=ALREADY_REVIEWED)
        wait_state = started(
            outcome.pull_request_number,
            outcome.head_sha,
            requested_at,
        )
        outcome = replace(outcome, wait_state=wait_state or "observer_unavailable")
        return outcome

    LOGGER.info(
        "External review requesting enabled: %s (%s)",
        REQUEST_EXTERNAL_REVIEW,
        getattr(selected, "reviewer", "external"),
    )
    return ReviewRuntime(
        provider=selected,
        definitions=(REVIEW_DEFINITION, REVIEW_CONTENT_DEFINITION),
        policies={
            # Plain permission since 2026-10-06 (D-026, "Requesting the
            # review"): Friedl delegated when to ask for a review to AL/X, as
            # he had delegated merging. A request on a head whose review is
            # already running or done attaches to that round rather than
            # asking again, which the provider enforces.
            REQUEST_EXTERNAL_REVIEW: AuthorityPolicy(
                frozenset({REVIEW_REQUEST_PERMISSION}),
            ),
            # Plain permission, deliberately. Reading spends nothing and
            # changes nothing outside, so requiring Friedl's word each time
            # would ask him a question with one answer - and would leave AL/X
            # unable to read a review she was woken to evaluate. It is a
            # separate permission from requesting, so holding one grants
            # nothing of the other.
            READ_EXTERNAL_REVIEW: AuthorityPolicy(
                frozenset({REVIEW_READ_PERMISSION}),
            ),
        },
        executors={
            **build_review_executors(request_review, call_id_source),
            **build_review_content_executors(reader.read, call_id_source),
        },
        permissions=frozenset(
            {REVIEW_REQUEST_PERMISSION, REVIEW_READ_PERMISSION}
        ),
    )
