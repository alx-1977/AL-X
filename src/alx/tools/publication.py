"""Two language-blind primitives for putting a repair where it can be reviewed.

Reached the way every capability is: AL/X proposes a structured call, the broker
validates it, the safety gate authorises it, and the executor performs one
bounded operation.

Publishing a branch and opening a pull request are separate capabilities because
they are separate decisions and have separate failure modes. A branch that
published but has no pull request is a recoverable state AL/X can see and act
on; one capability doing both would hide which half failed.

Neither merges. The reviewed revision a merge may act on is still established by
`merge_pull_request`, which refuses when the head has moved.
"""

from __future__ import annotations

import logging
from collections.abc import Callable, Mapping
from typing import Any

from alx.contracts import (
    CapabilityDefinition,
    CapabilityResult,
    CapabilityResultState,
    SideEffect,
    StructuredSchema,
    ValueKind,
)
from alx.contracts.publication import (
    PUBLICATION_FAILURES,
    PULL_REQUEST_FAILURES,
    PublicationError,
    PublicationRequest,
    PullRequestError,
    PullRequestRequest,
)

LOGGER = logging.getLogger(__name__)

PUBLISH_REPAIR_BRANCH = "publish_repair_branch"
OPEN_PULL_REQUEST = "open_pull_request"

_STRING = StructuredSchema(ValueKind.STRING)
_INTEGER = StructuredSchema(ValueKind.INTEGER)
_BOOLEAN = StructuredSchema(ValueKind.BOOLEAN)


PUBLISH_DEFINITION = CapabilityDefinition(
    PUBLISH_REPAIR_BRANCH,
    "Publish one local repair branch to the configured origin at one exact "
    "commit. The default branch cannot be published, the commit must be the "
    "one the branch currently points at, and a remote that has moved ahead is "
    "reported rather than overwritten: there is no force. Decides nothing "
    "about whether the work is ready to publish.",
    StructuredSchema(
        ValueKind.OBJECT,
        {"branch": _STRING, "head_sha": _STRING},
        ("branch", "head_sha"),
        extra_properties=False,
    ),
    StructuredSchema(
        ValueKind.OBJECT,
        {
            "branch": _STRING,
            "head_sha": _STRING,
            "published": _BOOLEAN,
            "already_current": _BOOLEAN,
        },
        ("branch", "head_sha", "published"),
        extra_properties=False,
    ),
    SideEffect.EFFECTFUL,
    PUBLICATION_FAILURES,
)


OPEN_DEFINITION = CapabilityDefinition(
    OPEN_PULL_REQUEST,
    "Open one pull request from a published repair branch into the default "
    "branch, or return the open one if it already exists. The base is fixed "
    "and cannot be chosen. Merges nothing and requests no review.",
    StructuredSchema(
        ValueKind.OBJECT,
        {"branch": _STRING, "title": _STRING, "body": _STRING},
        ("branch", "title"),
        extra_properties=False,
    ),
    StructuredSchema(
        ValueKind.OBJECT,
        {
            "pull_request_number": _INTEGER,
            "branch": _STRING,
            "head_sha": _STRING,
            "base": _STRING,
            "state": _STRING,
            "created": _BOOLEAN,
        },
        ("pull_request_number", "branch", "head_sha", "base", "state", "created"),
        extra_properties=False,
    ),
    SideEffect.EFFECTFUL,
    PULL_REQUEST_FAILURES,
    # The title and body are published on GitHub, so they are wording AL/X
    # composed for somewhere other than this conversation.
    transmits_authored_text=True,
)


DEFINITIONS = (PUBLISH_DEFINITION, OPEN_DEFINITION)


def _text(values: Mapping[str, Any], name: str, required: bool = True) -> str:
    value = values.get(name)
    if not isinstance(value, str) or (required and not value.strip()):
        raise ValueError(f"{name} must be a non-blank string")
    return value


def build_publication_executors(
    publication: Any,
    pull_requests: Any,
    call_id_source: Callable[[], str],
) -> Mapping[str, Callable[[Mapping[str, Any]], CapabilityResult]]:
    """Wire the two publication capabilities to their providers."""

    def publish(arguments: Mapping[str, Any]) -> CapabilityResult:
        call_id = call_id_source()
        try:
            request = PublicationRequest(
                branch=_text(arguments, "branch"),
                head_sha=_text(arguments, "head_sha"),
            )
        except (TypeError, ValueError):
            return CapabilityResult(
                call_id, PUBLISH_REPAIR_BRANCH, CapabilityResultState.FAILED,
                failure={"code": "arguments_unusable"},
            )
        try:
            outcome = publication.publish(request)
        except PublicationError as error:
            return CapabilityResult(
                call_id, PUBLISH_REPAIR_BRANCH, CapabilityResultState.FAILED,
                failure={"code": error.code},
            )
        except Exception as error:  # pragma: no cover - provider contract
            LOGGER.warning("Publication failed: %s", type(error).__name__)
            return CapabilityResult(
                call_id, PUBLISH_REPAIR_BRANCH, CapabilityResultState.FAILED,
                failure={"code": "publication_unavailable"},
            )
        return CapabilityResult(
            call_id, PUBLISH_REPAIR_BRANCH, CapabilityResultState.SUCCEEDED,
            outcome.as_values(),
        )

    def open_pull_request(arguments: Mapping[str, Any]) -> CapabilityResult:
        call_id = call_id_source()
        try:
            request = PullRequestRequest(
                branch=_text(arguments, "branch"),
                title=_text(arguments, "title"),
                body=_text(arguments, "body", required=False)
                if "body" in arguments
                else "",
            )
        except (TypeError, ValueError):
            return CapabilityResult(
                call_id, OPEN_PULL_REQUEST, CapabilityResultState.FAILED,
                failure={"code": "arguments_unusable"},
            )
        try:
            outcome = pull_requests.open(request)
        except PullRequestError as error:
            return CapabilityResult(
                call_id, OPEN_PULL_REQUEST, CapabilityResultState.FAILED,
                failure={"code": error.code},
            )
        except Exception as error:  # pragma: no cover - provider contract
            LOGGER.warning("Opening a pull request failed: %s", type(error).__name__)
            return CapabilityResult(
                call_id, OPEN_PULL_REQUEST, CapabilityResultState.FAILED,
                failure={"code": "pull_request_unavailable"},
            )
        return CapabilityResult(
            call_id, OPEN_PULL_REQUEST, CapabilityResultState.SUCCEEDED,
            outcome.as_values(),
        )

    return {
        PUBLISH_REPAIR_BRANCH: publish,
        OPEN_PULL_REQUEST: open_pull_request,
    }


__all__ = [
    "DEFINITIONS",
    "OPEN_DEFINITION",
    "OPEN_PULL_REQUEST",
    "PUBLISH_DEFINITION",
    "PUBLISH_REPAIR_BRANCH",
    "build_publication_executors",
]
