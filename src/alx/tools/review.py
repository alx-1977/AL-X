"""One language-blind primitive for requesting an external review.

Reached the way every capability is: AL/X proposes a structured call, the
broker validates it, the safety gate authorises it under `review.request`, and
the executor performs it.

Requesting a review is effectful. Since 2026-10-06 it is AL/X's to decide
under plain permission (D-026, "Requesting the review"): Friedl delegated when
to ask, as he delegated merging. Each review is paid for, so Friedl set an
allowance on 2026-10-14: three a pull request and twelve a month. Within it she
asks as she judges; past it the request is refused, and
`request_external_review_beyond_allowance` asks for one more with his approval
of that exact call. A request on a head whose review is already running or done
attaches to that round instead of asking again, and costs nothing.

A request names one pull request, not a revision; which commit that is now is
read when the request is made and reported back with the outcome.

The existing task runtime waits boundedly for publication. Nothing here reads
a review, judges findings, or decides whether anything may merge.
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
REQUEST_EXTERNAL_REVIEW_BEYOND_ALLOWANCE = "request_external_review_beyond_allowance"

_STRING = StructuredSchema(ValueKind.STRING)
_INTEGER = StructuredSchema(ValueKind.INTEGER)
_BOOLEAN = StructuredSchema(ValueKind.BOOLEAN)


_ARGUMENTS = StructuredSchema(
    ValueKind.OBJECT,
    {"pull_request_number": _INTEGER},
    ("pull_request_number",),
    extra_properties=False,
)

_RESULT = StructuredSchema(
    ValueKind.OBJECT,
    {
        "pull_request_number": _INTEGER,
        "head_sha": _STRING,
        "requested": _BOOLEAN,
        "reviewer": _STRING,
        "requested_at": _STRING,
        "wait_state": _STRING,
    },
    ("pull_request_number", "head_sha", "requested", "reviewer"),
    extra_properties=False,
)


DEFINITION = CapabilityDefinition(
    REQUEST_EXTERNAL_REVIEW,
    "Ask the configured external reviewer to review one pull request. "
    "Requests a review of whatever revision the pull request currently points "
    "at, waits boundedly for publication, and reports that revision and wait_state. "
    "If the reviewer is already reviewing, or has reviewed, that exact revision, "
    "nothing new is requested (requested is false) and the wait attaches to that "
    "review. "
    "Each review is paid for: Friedl allows three a pull request. Past that, "
    "nothing is requested and review_allowance_reached returns the count. "
    "Read the completed review to judge it; never request another review to poll.",
    _ARGUMENTS,
    _RESULT,
    SideEffect.EFFECTFUL,
    REVIEW_FAILURES,
    trace_fields=("pull_request_number",),
)

BEYOND_ALLOWANCE_DEFINITION = CapabilityDefinition(
    REQUEST_EXTERNAL_REVIEW_BEYOND_ALLOWANCE,
    "Ask the configured external reviewer for one more review of one pull "
    "request after request_external_review returned review_allowance_reached. "
    "Needs Friedl's approval of this exact call; otherwise the same as "
    "request_external_review.",
    _ARGUMENTS,
    _RESULT,
    SideEffect.EFFECTFUL,
    REVIEW_FAILURES,
    trace_fields=("pull_request_number",),
)


def build_review_executors(
    request_review: Callable[[ReviewRequest], Any],
    call_id_source: Callable[[], str],
    name: str = REQUEST_EXTERNAL_REVIEW,
) -> Mapping[str, Callable[[Mapping[str, Any]], CapabilityResult]]:
    """Wire one review request to its structured capability result."""

    def request_external_review(arguments: Mapping[str, Any]) -> CapabilityResult:
        call_id = call_id_source()
        try:
            review = ReviewRequest(
                pull_request_number=int(arguments["pull_request_number"]),
            )
        except (KeyError, TypeError, ValueError):
            return _failed(call_id, "arguments_unusable", name=name)

        try:
            outcome = request_review(review)
        except ReviewError as error:
            return _failed(call_id, error.code, error.details, name)
        except Exception as error:  # noqa: BLE001 - unclassified is still a fact
            # The type, never the message: a provider exception's wording can
            # carry the request that produced it, and this one carries a token.
            LOGGER.warning(
                "Review request failed for one pull request: %s",
                type(error).__name__,
            )
            return _failed(call_id, "review_unavailable", name=name)

        if outcome.wait_state and outcome.wait_state != "completed":
            return CapabilityResult(
                call_id, name, CapabilityResultState.FAILED,
                outcome.as_values(),
                failure={"code": "review_unavailable", "reason": outcome.wait_state,
                         "requires_judgement": True, "requested": outcome.requested,
                         "head_sha": outcome.head_sha},
            )
        return CapabilityResult(
            call_id,
            name,
            CapabilityResultState.SUCCEEDED,
            outcome.as_values(),
        )

    return {name: request_external_review}


def _failed(
    call_id: str,
    code: str,
    details: Mapping[str, Any] | None = None,
    name: str = REQUEST_EXTERNAL_REVIEW,
) -> CapabilityResult:
    failure: dict[str, Any] = {"code": code, **(details or {})}
    if code == "review_allowance_reached":
        # Not a fault: Friedl's allowance is used, and the next review is his
        # to allow. Whether to ask him, and when, is hers.
        failure["requires_judgement"] = True
    return CapabilityResult(
        call_id,
        name,
        CapabilityResultState.FAILED,
        failure=failure,
    )
