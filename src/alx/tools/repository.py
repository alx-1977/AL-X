"""One language-blind primitive for merging a reviewed pull request.

Reached the way every capability is: AL/X proposes a structured call, the
broker validates it, the safety gate authorises it under `repository.merge`,
and the executor performs it.

The judgement happens before this. An external reviewer examines the current
head; AL/X reads what it found and decides whether anything needs correcting.
If it does, she does not call this. If it does not, she calls it with the exact
revision that was reviewed. Nothing here reads a review, scores a finding, or
decides whether a merge is warranted, because that is exactly the judgement
Law 1 keeps in the Core.

There is no per-merge approval. Friedl delegated this authority once, in
governance, and grants or revokes it by granting or revoking the permission.
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
from alx.contracts.repository import (
    MERGE_FAILURES,
    MergeError,
    MergeRequest,
)


LOGGER = logging.getLogger(__name__)

MERGE_PULL_REQUEST = "merge_pull_request"

_STRING = StructuredSchema(ValueKind.STRING)
_INTEGER = StructuredSchema(ValueKind.INTEGER)
_BOOLEAN = StructuredSchema(ValueKind.BOOLEAN)


DEFINITION = CapabilityDefinition(
    MERGE_PULL_REQUEST,
    "Merge one pull request at one exact reviewed revision. Requires the head "
    "commit that was reviewed, and the merge does not happen if the branch has "
    "moved since. Decides nothing about whether the change is ready: that "
    "judgement is made before this is called.",
    StructuredSchema(
        ValueKind.OBJECT,
        {
            "pull_request_number": _INTEGER,
            "head_sha": _STRING,
            "title": _STRING,
            "message": _STRING,
        },
        ("pull_request_number", "head_sha"),
        extra_properties=False,
    ),
    StructuredSchema(
        ValueKind.OBJECT,
        {
            "pull_request_number": _INTEGER,
            "head_sha": _STRING,
            "merged": _BOOLEAN,
            "merge_commit_sha": _STRING,
        },
        ("pull_request_number", "head_sha", "merged"),
        extra_properties=False,
    ),
    SideEffect.EFFECTFUL,
    MERGE_FAILURES,
    # The optional title and message are published as commit metadata, so they
    # are wording AL/X composed for somewhere other than this conversation.
    # Declared rather than left at the default, so the Core's check on text
    # Friedl has not heard applies here as it does to mail.
    transmits_authored_text=True,
)


def build_repository_executors(
    merge: Callable[[MergeRequest], Any],
    call_id_source: Callable[[], str],
) -> Mapping[str, Callable[[Mapping[str, Any]], CapabilityResult]]:
    """Wire the one merge outcome to its structured capability result."""

    def merge_pull_request(arguments: Mapping[str, Any]) -> CapabilityResult:
        call_id = call_id_source()
        try:
            request = MergeRequest(
                pull_request_number=int(arguments["pull_request_number"]),
                head_sha=str(arguments["head_sha"]),
                title=str(arguments.get("title") or ""),
                message=str(arguments.get("message") or ""),
            )
        except (KeyError, TypeError, ValueError):
            return _failed(call_id, "arguments_unusable")

        try:
            outcome = merge(request)
        except MergeError as error:
            return _failed(call_id, error.code)
        except Exception:  # noqa: BLE001 - an unclassified failure is still a fact
            LOGGER.warning("Merge failed for one pull request")
            return _failed(call_id, "merge_unavailable")

        return CapabilityResult(
            call_id,
            MERGE_PULL_REQUEST,
            CapabilityResultState.SUCCEEDED,
            outcome.as_values(),
        )

    return {MERGE_PULL_REQUEST: merge_pull_request}


def _failed(call_id: str, code: str) -> CapabilityResult:
    return CapabilityResult(
        call_id,
        MERGE_PULL_REQUEST,
        CapabilityResultState.FAILED,
        failure={"code": code},
    )
