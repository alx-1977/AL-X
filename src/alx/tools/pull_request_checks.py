"""One language-blind primitive for reading GitHub check results.

Reached the way every capability is: AL/X proposes a structured call, the
broker validates it, the safety gate authorises it, and the executor performs
it.

Reading spends nothing and changes nothing on GitHub. It does not rerun a
job, cancel one, request a review, or merge. What comes back is GitHub's own
check runs, commit statuses, and, for a failed Actions job, that job's steps
and a bounded tail of its log. None of it is scored here.

The log and the check text stay out of durable goal state. A durable copy of
CI output would make the goal store a second evidence store. The pull request
and the revision stay citable across a restart.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any, Mapping

from alx.contracts import (
    CapabilityDefinition,
    CapabilityResult,
    CapabilityResultState,
    ContentOrigin,
    ExecutionOutcome,
    RetentionPolicy,
    SideEffect,
    StructuredSchema,
    ValueKind,
)
from alx.contracts.pull_request_checks import (
    CHECK_READ_FAILURES,
    CheckReadError,
    PullRequestChecks,
    PullRequestChecksRequest,
)


LOGGER = logging.getLogger(__name__)

READ_PULL_REQUEST_CHECKS = "read_pull_request_checks"

_STRING = StructuredSchema(ValueKind.STRING)
_INTEGER = StructuredSchema(ValueKind.INTEGER)
_ANY = StructuredSchema(ValueKind.ANY)

_APP = StructuredSchema(
    ValueKind.OBJECT,
    {"slug": _ANY, "name": _ANY},
    ("slug", "name"),
    extra_properties=False,
)
_OUTPUT = StructuredSchema(
    ValueKind.OBJECT,
    {"title": _ANY, "summary": _ANY},
    ("title", "summary"),
    extra_properties=False,
)
_STEP = StructuredSchema(
    ValueKind.OBJECT,
    {"name": _ANY, "conclusion": _ANY},
    ("name", "conclusion"),
    extra_properties=False,
)
_CHECK_RUN = StructuredSchema(
    ValueKind.OBJECT,
    {
        "name": _ANY,
        "status": _ANY,
        "conclusion": _ANY,
        "started_at": _ANY,
        "completed_at": _ANY,
        "details_url": _ANY,
        "app": _APP,
        "output": _OUTPUT,
        "steps": StructuredSchema(ValueKind.ARRAY, items=_STEP),
        "log_tail": _STRING,
        "characters_omitted": _INTEGER,
        "log_failure": _STRING,
    },
    (
        "name",
        "status",
        "conclusion",
        "started_at",
        "completed_at",
        "details_url",
        "app",
        "output",
    ),
    extra_properties=False,
)
_STATUS = StructuredSchema(
    ValueKind.OBJECT,
    {
        "context": _ANY,
        "state": _ANY,
        "description": _ANY,
        "target_url": _ANY,
    },
    ("context", "state", "description", "target_url"),
    extra_properties=False,
)


DEFINITION = CapabilityDefinition(
    READ_PULL_REQUEST_CHECKS,
    "Read GitHub's own check runs, commit statuses, and Actions log tails "
    "for one pull request at one exact head revision. Returns them as GitHub "
    "reported them. It does not rerun, cancel, merge, or score anything.",
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
            "check_runs": StructuredSchema(ValueKind.ARRAY, items=_CHECK_RUN),
            "commit_statuses": StructuredSchema(ValueKind.ARRAY, items=_STATUS),
        },
        ("pull_request_number", "head_sha", "check_runs", "commit_statuses"),
        extra_properties=False,
    ),
    SideEffect.NONE,
    CHECK_READ_FAILURES,
    durable_input_fields=("pull_request_number", "head_sha"),
    transmits_authored_text=False,
    # Reading again changes nothing, so a plan may wait on it.
    plan_observation=True,
)

# GitHub's own vocabulary, read the way its required-check rule reads it:
# success, neutral and skipped pass; these have failed. Anything else that
# has settled, such as action_required or stale, is for AL/X to read.
_PASSING_CONCLUSIONS = frozenset({"success", "neutral", "skipped"})
_FAILED_CONCLUSIONS = frozenset({"failure", "timed_out", "cancelled", "startup_failure"})
_FAILED_STATUSES = frozenset({"failure", "error"})
# A read that may well succeed if simply made again.
_TRANSIENT_READ_FAILURES = frozenset({"rate_limited", "provider_failed"})


def _outcome(values: Mapping[str, Any]) -> ExecutionOutcome:
    """Whether the checks have settled, and how, for work already decided.

    Settlement, not permission: SUCCESS says every check passed by GitHub's
    own rule, not that a merge should follow; merge authority stays with the
    merge capability and its gates. Precedence is fixed: a failure anywhere,
    including a failed step of a job still running, then any settled result
    needing judgment, then pending, then success.
    """
    runs = values["check_runs"]
    statuses = values["commit_statuses"]
    settled = [run["conclusion"] for run in runs if run["status"] == "completed"]
    # A step with a conclusion has settled, even inside a run still going;
    # one without is still running.
    settled += [step["conclusion"] for run in runs for step in run.get("steps", ())
                if step["conclusion"] is not None]
    # 1. A failure anywhere is a failure now.
    if (any(item in _FAILED_CONCLUSIONS for item in settled)
            or any(item["state"] in _FAILED_STATUSES for item in statuses)):
        return ExecutionOutcome.FAILURE
    # 2. A settled run, step or status outside the passing vocabulary is hers
    #    to read now, whatever else is still running: waiting cannot change it.
    if (any(item not in _PASSING_CONCLUSIONS for item in settled)
            or any(item["state"] not in ("success", "pending") for item in statuses)):
        return ExecutionOutcome.AMBIGUOUS
    # 3. Only then is anything unresolved pending. No checks yet is pending:
    #    they register after a push.
    if (not runs and not statuses
            or any(run["status"] != "completed" for run in runs)
            or any(item["state"] == "pending" for item in statuses)):
        return ExecutionOutcome.PENDING
    # 4. Everything settled and passing.
    return ExecutionOutcome.SUCCESS


def build_pull_request_checks_executors(
    read_checks: Callable[[PullRequestChecksRequest], Any],
    call_id_source: Callable[[], str],
) -> Mapping[str, Callable[[Mapping[str, Any]], CapabilityResult]]:
    """Bind the one check-read primitive to its provider."""

    def read_pull_request_checks(arguments: Mapping[str, Any]) -> CapabilityResult:
        call_id = call_id_source()
        try:
            number = arguments["pull_request_number"]
            if isinstance(number, bool):
                raise TypeError("pull_request_number must be an integer")
            request = PullRequestChecksRequest(
                pull_request_number=int(number),
                head_sha=str(arguments["head_sha"]),
            )
        except (KeyError, TypeError, ValueError):
            return _failed(call_id, "arguments_unusable")

        try:
            content = read_checks(request)
            if not isinstance(content, PullRequestChecks):
                raise TypeError("provider returned malformed check results")
            values = content.as_values()
            provenance = RetentionPolicy().non_mail(
                ContentOrigin.EXTERNAL, datetime.now(UTC)
            )
        except CheckReadError as error:
            if error.code == "head_changed":
                return _failed(call_id, error.code, actual_head=error.actual_head)
            return _failed(call_id, error.code)
        except Exception as error:  # noqa: BLE001 - unclassified is still a fact
            # The type, never the message. A provider exception's wording can
            # carry the request that produced it, and this one carries a token.
            LOGGER.warning(
                "Reading pull request checks failed: %s", type(error).__name__
            )
            return _failed(call_id, "provider_failed")

        return CapabilityResult(
            call_id,
            READ_PULL_REQUEST_CHECKS,
            CapabilityResultState.SUCCEEDED,
            values,
            # The revision stays citable. The check text and the log do not.
            durable_values={
                "pull_request_number": values["pull_request_number"],
                "head_sha": values["head_sha"],
            },
            provenance=provenance,
            outcome=_outcome(values),
        )

    return {READ_PULL_REQUEST_CHECKS: read_pull_request_checks}


def _failed(call_id: str, code: str, **details: object) -> CapabilityResult:
    return CapabilityResult(
        call_id,
        READ_PULL_REQUEST_CHECKS,
        CapabilityResultState.FAILED,
        failure={
            "code": code,
            **details,
            "requires_judgement": code != "arguments_unusable",
        },
        outcome=(ExecutionOutcome.TEMPORARILY_UNAVAILABLE
                 if code in _TRANSIENT_READ_FAILURES else None),
    )


__all__ = [
    "DEFINITION",
    "READ_PULL_REQUEST_CHECKS",
    "build_pull_request_checks_executors",
]
