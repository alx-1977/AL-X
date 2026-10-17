"""Compose external review requesting, or leave it unavailable entirely.

Returning None leaves the capability unregistered, so AL/X cannot request a
review at all. That is the difference between the capability being withheld and
requesting merely failing.

The reviewer is Greptile, alone since 2026-10-14. It watches this repository
through GitHub, and its profile supplies what is particular to it: the name,
the trigger comment, the bot account and its check run.

Each review is paid for, so the ones AL/X asks for are counted against
Friedl's allowance (three a pull request). Past it, an
ordinary request is refused before anything is posted, and the separate
beyond-allowance capability asks for one more under his approval of that exact
call.
"""

from __future__ import annotations

import logging
from collections.abc import Callable, Mapping
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from typing import Any

from alx.contracts import CapabilityDefinition, CapabilityResult, StructuredData
from alx.contracts.review import ReviewError, ReviewRequest
from alx.contracts.review_provider import ReviewProvider, profile_for
from alx.observability.review_allowance import ReviewAllowanceReached
from alx.providers.github_review import GitHubReviewProvider
from alx.safety import AuthorityPolicy
from alx.tools.review import (
    BEYOND_ALLOWANCE_DEFINITION,
    DEFINITION as REVIEW_DEFINITION,
    REQUEST_EXTERNAL_REVIEW,
    REQUEST_EXTERNAL_REVIEW_BEYOND_ALLOWANCE,
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


class _AllowanceLedger:
    """Counts reviews against Friedl's allowance for the provider."""

    def __init__(self, allowance: Any, enforce: bool) -> None:
        self._allowance = allowance
        self._enforce = enforce

    def reserve(self, number: int, head_sha: str) -> int:
        try:
            return self._allowance.reserve(
                number, head_sha, datetime.now(UTC), enforce=self._enforce
            )
        except ReviewAllowanceReached as reached:
            raise ReviewError(
                "review_allowance_reached", reached.state.as_values()
            ) from None
        except Exception as error:  # noqa: BLE001 - fail closed, say why
            LOGGER.warning("Review allowance unreadable: %s", type(error).__name__)
            raise ReviewError("review_unavailable") from None

    def release(self, record: int) -> None:
        try:
            self._allowance.release(record)
        except Exception as error:  # noqa: BLE001 - stays counted
            LOGGER.warning("Review count not released: %s", type(error).__name__)


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
    reviewer: ReviewProvider | str = ReviewProvider.GREPTILE,
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
    # Friedl's review allowance (`SQLiteReviewAllowance`). Without it nothing
    # limits requests and no beyond-allowance capability is offered.
    allowance: Any = None,
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
                    ReviewProvider.GREPTILE.value,
                )
                reviewer = ReviewProvider.GREPTILE

        # One provider requests and reads: both are GitHub calls about the
        # same pull request by the same reviewer. Each review is counted just
        # before its trigger is posted; the ordinary request is stopped there
        # at the allowance, the beyond-allowance one, approved by Friedl, is
        # not. A count that cannot be made stops the request: the allowance
        # fails closed.
        composed = GitHubReviewProvider(
            repository, token, profile_for(reviewer),
            trigger_ledger=None if allowance is None else _AllowanceLedger(allowance, True),
        )
        unlimited = GitHubReviewProvider(
            repository, token, profile_for(reviewer),
            trigger_ledger=None if allowance is None else _AllowanceLedger(allowance, False),
        )
        selected = provider or composed
        beyond = provider or unlimited
        reader = content_provider or composed
    except (KeyError, TypeError, ValueError):
        LOGGER.warning("External review requesting is misconfigured: no capability")
        return None

    def request_review(review: ReviewRequest, through: Any = None) -> Any:
        # The provider reports GitHub's timestamp for the trigger comment. It
        # is the same clock used by result comments, so local clock skew cannot
        # exclude a fast result or admit one that predates this request. An
        # injected provider without that fact falls back to the pre-call time.
        local_requested_at = datetime.now(UTC)
        outcome = (through or selected).request(review)
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
    beyond_allowance = allowance is not None
    return ReviewRuntime(
        provider=selected,
        definitions=(
            (REVIEW_DEFINITION, REVIEW_CONTENT_DEFINITION)
            + ((BEYOND_ALLOWANCE_DEFINITION,) if beyond_allowance else ())
        ),
        policies={
            # Plain permission since 2026-10-06 (D-026, "Requesting the
            # review"): Friedl delegated when to ask for a review to AL/X, as
            # he had delegated merging, within his allowance since
            # 2026-10-14. A request on a head whose review is already running
            # or done attaches to that round rather than asking again, which
            # the provider enforces.
            REQUEST_EXTERNAL_REVIEW: AuthorityPolicy(
                frozenset({REVIEW_REQUEST_PERMISSION}),
            ),
            # Past the allowance, a review is Friedl's to allow: "after that
            # she needs to ask" (2026-10-14). His approval binds the exact call.
            **({
                REQUEST_EXTERNAL_REVIEW_BEYOND_ALLOWANCE: AuthorityPolicy(
                    frozenset({REVIEW_REQUEST_PERMISSION}),
                    approval_required=True,
                ),
            } if beyond_allowance else {}),
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
            **(build_review_executors(
                lambda review: request_review(review, beyond),
                call_id_source,
                REQUEST_EXTERNAL_REVIEW_BEYOND_ALLOWANCE,
            ) if beyond_allowance else {}),
            **build_review_content_executors(reader.read, call_id_source),
        },
        permissions=frozenset(
            {REVIEW_REQUEST_PERMISSION, REVIEW_READ_PERMISSION}
        ),
    )
