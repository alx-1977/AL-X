"""Compose external review requesting, or leave it unavailable entirely.

Returning None leaves the capability unregistered, so AL/X cannot request a
review at all. That is the difference between the capability being withheld and
requesting merely failing.

Qodo is the reviewer this composes because it is the one installed on the
repository. The provider is injected, so another reviewer can replace it
without changing the capability, the gate or the Core; nothing here selects
between reviewers, because there is one.
"""

from __future__ import annotations

import logging
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from alx.contracts import CapabilityDefinition, CapabilityResult, StructuredData
from alx.contracts.review import ReviewRequest
from alx.providers.qodo_review import QodoReviewProvider
from alx.providers.qodo_review_content import QodoReviewContentProvider
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
    provider: Any = None,
    # Reads what a reviewer published. Injected like the requesting provider,
    # so another reviewer can replace it without changing the capability.
    content_provider: Any = None,
    # Called when a review has been requested, so something can watch for the
    # result. Optional: without it the request still works and simply is not
    # watched, which is honest rather than broken.
    started: Callable[[int, str, datetime], None] | None = None,
) -> ReviewRuntime | None:
    """Compose review requesting, or leave it unregistered."""
    if not enabled:
        LOGGER.info("External review requesting is not enabled: no capability")
        return None
    if not repository.strip() or not token.strip():
        LOGGER.info("External review requesting is not configured: no capability")
        return None

    try:
        selected = provider or QodoReviewProvider(repository, token)
        reader = content_provider or QodoReviewContentProvider(repository, token)
    except ValueError:
        LOGGER.warning("External review requesting is misconfigured: no capability")
        return None

    def request_review(review: ReviewRequest) -> Any:
        # The provider reports GitHub's timestamp for the trigger comment. It
        # is the same clock used by result comments, so local clock skew cannot
        # exclude a fast result or admit one that predates this request. An
        # injected provider without that fact falls back to the pre-call time.
        local_requested_at = datetime.now(UTC)
        outcome = selected.request(review)
        if started is not None:
            started(
                outcome.pull_request_number,
                outcome.head_sha,
                outcome.requested_at or local_requested_at,
            )
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
            # Approval required, and deliberately not a standing scope. The
            # approval must be grounded in Friedl's latest turn, which is
            # exactly "he asked for this review". It is single-use, so one
            # instruction buys one request: a review that found issues, a fix,
            # a moved head or a failed request cannot produce another without
            # him asking again. That is the whole reason this is not plain
            # permission like merging is.
            REQUEST_EXTERNAL_REVIEW: AuthorityPolicy(
                frozenset({REVIEW_REQUEST_PERMISSION}),
                approval_required=True,
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
