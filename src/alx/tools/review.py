"""One language-blind primitive for requesting an external review.

Reached the way every capability is: AL/X proposes a structured call, the
broker validates it, the safety gate authorises it under `review.request`, and
the executor performs it.

Requesting a review is effectful and may consume review credits, so the gate
requires an approval grounded in Friedl's latest turn. That is the existing
mechanism for "the person asked for this", and it gives the property this
capability needs without inventing anything: an approval is single-use and tied
to one turn, so one instruction produces one request. A review that found
issues, a fix, a changed head or a failed request cannot cause another; each
needs Friedl to ask again.

The approval authorises one paid review of one pull request. It does not name a
revision, because Friedl asks for a pull request to be reviewed rather than for
a particular commit; which commit that is now is read when the request is made
and reported back with the outcome.

Nothing here reads a review, waits for one, judges findings, or decides whether
anything may merge.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from typing import Any, Mapping

from alx.contracts import (
    CapabilityDefinition,
    CapabilityResult,
    CapabilityResultState,
    SideEffect,
    StructuredSchema,
    ValueKind,
)
from alx.contracts.review import REVIEW_FAILURES, ReviewError, ReviewRequest


LOGGER = logging.getLogger(__name__)

REQUEST_EXTERNAL_REVIEW = "request_external_review"

_STRING = StructuredSchema(ValueKind.STRING)
_INTEGER = StructuredSchema(ValueKind.INTEGER)
_BOOLEAN = StructuredSchema(ValueKind.BOOLEAN)


DEFINITION = CapabilityDefinition(
    REQUEST_EXTERNAL_REVIEW,
    "Ask the configured external reviewer to review one pull request. "
    "Requests a review of whatever revision the pull request currently points "
    "at, and reports that revision; it does not wait for the review, read it, "
    "or judge what it finds.",
    StructuredSchema(
        ValueKind.OBJECT,
        {"pull_request_number": _INTEGER},
        ("pull_request_number",),
        extra_properties=False,
    ),
    StructuredSchema(
        ValueKind.OBJECT,
        {
            "pull_request_number": _INTEGER,
            "head_sha": _STRING,
            "requested": _BOOLEAN,
            "reviewer": _STRING,
        },
        ("pull_request_number", "head_sha", "requested", "reviewer"),
        extra_properties=False,
    ),
    SideEffect.EFFECTFUL,
    REVIEW_FAILURES,
)


def build_review_executors(
    request_review: Callable[[ReviewRequest], Any],
    call_id_source: Callable[[], str],
) -> Mapping[str, Callable[[Mapping[str, Any]], CapabilityResult]]:
    """Wire the one review request to its structured capability result."""

    def request_external_review(arguments: Mapping[str, Any]) -> CapabilityResult:
        call_id = call_id_source()
        try:
            review = ReviewRequest(
                pull_request_number=int(arguments["pull_request_number"]),
            )
        except (KeyError, TypeError, ValueError):
            return _failed(call_id, "arguments_unusable")

        try:
            outcome = request_review(review)
        except ReviewError as error:
            return _failed(call_id, error.code)
        except Exception as error:  # noqa: BLE001 - unclassified is still a fact
            # The type, never the message: a provider exception's wording can
            # carry the request that produced it, and this one carries a token.
            LOGGER.warning(
                "Review request failed for one pull request: %s",
                type(error).__name__,
            )
            return _failed(call_id, "review_unavailable")

        return CapabilityResult(
            call_id,
            REQUEST_EXTERNAL_REVIEW,
            CapabilityResultState.SUCCEEDED,
            outcome.as_values(),
        )

    return {REQUEST_EXTERNAL_REVIEW: request_external_review}


def _failed(call_id: str, code: str) -> CapabilityResult:
    return CapabilityResult(
        call_id,
        REQUEST_EXTERNAL_REVIEW,
        CapabilityResultState.FAILED,
        failure={"code": code},
    )
