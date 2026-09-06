"""One language-blind primitive for reading an external review.

Reached the way every capability is: AL/X proposes a structured call, the
broker validates it, the safety gate authorises it, and the executor performs
it.

Reading is not requesting. This spends nothing, changes nothing outside, and
needs no approval grounded in a person turn: it is a read of something that
already exists, so plain permission is the right authority and asking Friedl
each time would put a question to him that has only one answer.

The review's words arrive as external content, marked as such. They are not a
verdict this tool reached and must never read as one: a reviewer is advising,
and under D-026 whether the advice permits a merge is AL/X's judgement. So
there is no clean field, no severity ranking and no finding count computed
here. She reads what the reviewer wrote and decides.

The body stays out of durable goal state, exactly as a retrieved web page
does. The retrieval remains citable across a restart through attempt:<call_id>
without the review text becoming a second evidence store.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from typing import Any, Mapping

from alx.contracts import (
    CapabilityDefinition,
    CapabilityResult,
    CapabilityResultState,
    ContentOrigin,
    RetentionPolicy,
    SideEffect,
    StructuredSchema,
    ValueKind,
)
from alx.contracts.review_content import (
    REVIEW_READ_FAILURES,
    ReviewContentRequest,
    ReviewReadError,
)


LOGGER = logging.getLogger(__name__)

READ_EXTERNAL_REVIEW = "read_external_review"

_STRING = StructuredSchema(ValueKind.STRING)
_INTEGER = StructuredSchema(ValueKind.INTEGER)
_BOOLEAN = StructuredSchema(ValueKind.BOOLEAN)

_COMMENT = StructuredSchema(
    ValueKind.OBJECT,
    {"body": _STRING, "path": _STRING, "line": _INTEGER},
    ("body",),
    extra_properties=False,
)


DEFINITION = CapabilityDefinition(
    READ_EXTERNAL_REVIEW,
    "Read what the external reviewer published about one pull request at one "
    "exact revision. Returns the reviewer's own summary and comments as "
    "written, or reports that no review covers that revision. It does not "
    "request a review, wait for one, judge what it found, or merge anything.",
    StructuredSchema(
        ValueKind.OBJECT,
        {"pull_request_number": _INTEGER, "head_sha": _STRING},
        ("pull_request_number", "head_sha"),
        extra_properties=False,
    ),
    StructuredSchema(
        ValueKind.OBJECT,
        {
            "pull_request_number": _INTEGER,
            "head_sha": _STRING,
            "reviewer": _STRING,
            # Whether a review by this reviewer for this exact commit was
            # found. A fact about retrieval, never about the code.
            "available": _BOOLEAN,
            "summary": _STRING,
            "comments": StructuredSchema(ValueKind.ARRAY, items=_COMMENT),
            "submitted_at": _STRING,
            "retrieved_at": _STRING,
            "unavailable_reason": _STRING,
        },
        ("pull_request_number", "head_sha", "reviewer", "available"),
        extra_properties=False,
    ),
    # Reaches outside the process, so it is effectful in the sense the gate
    # means. It writes nothing anywhere.
    SideEffect.EFFECTFUL,
    REVIEW_READ_FAILURES,
    # The revision and the pull request stay citable across a restart. The
    # review's wording deliberately does not: a durable copy of what a reviewer
    # said would make the goal store a second evidence store.
    durable_input_fields=("pull_request_number", "head_sha"),
)


def build_review_content_executors(
    read_review: Callable[[ReviewContentRequest], Any],
    call_id_source: Callable[[], str],
) -> Mapping[str, Callable[[Mapping[str, Any]], CapabilityResult]]:
    """Bind the one review-read primitive to its provider."""

    def read_external_review(arguments: Mapping[str, Any]) -> CapabilityResult:
        call_id = call_id_source()
        try:
            request = ReviewContentRequest(
                pull_request_number=int(arguments["pull_request_number"]),
                head_sha=str(arguments["head_sha"]),
            )
        except (KeyError, TypeError, ValueError):
            return _failed(call_id, "arguments_unusable")

        try:
            content = read_review(request)
        except ReviewReadError as error:
            return _failed(call_id, error.code)
        except Exception as error:  # noqa: BLE001 - unclassified is still a fact
            # The type, never the message: a provider exception's wording can
            # carry the request that produced it, and this one carries a token.
            LOGGER.warning(
                "Reading one external review failed: %s", type(error).__name__
            )
            return _failed(call_id, "review_unavailable")

        values = content.as_values()
        return CapabilityResult(
            call_id,
            READ_EXTERNAL_REVIEW,
            CapabilityResultState.SUCCEEDED,
            values,
            # Metadata only. What the reviewer said stays in this turn's
            # context and out of durable goal state.
            durable_values={
                key: values[key]
                for key in (
                    "pull_request_number",
                    "head_sha",
                    "reviewer",
                    "available",
                    "submitted_at",
                    "retrieved_at",
                    "unavailable_reason",
                )
                if key in values
            },
            # A reviewer's words are external content, not AL/X's own and not
            # this tool's conclusion. Marked so nothing downstream can mistake
            # a finding for something she reasoned or something code decided.
            # Not mail-derived, so no D-013 expiry.
            provenance=RetentionPolicy().non_mail(
                ContentOrigin.EXTERNAL,
                content.retrieved_at,
            ),
        )

    return {READ_EXTERNAL_REVIEW: read_external_review}


def _failed(call_id: str, code: str) -> CapabilityResult:
    return CapabilityResult(
        call_id,
        READ_EXTERNAL_REVIEW,
        CapabilityResultState.FAILED,
        failure={"code": code},
    )
