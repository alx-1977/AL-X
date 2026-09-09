"""One language-blind primitive for a Core-delegated coding job, under D-028.

Reached the way every capability is: AL/X proposes a structured call, the
broker validates it, the safety gate authorises it under `coding.execute`, and
the executor runs the bounded job. The capability edits only the assigned
worktree, runs only permitted development commands, and returns evidence.
It does not merge, push, deploy, or request a review.
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
from alx.contracts.coding import (
    CODING_FAILURES,
    DEFAULT_STEP_BUDGET,
    CodingError,
    CodingRequest,
)


LOGGER = logging.getLogger(__name__)

RUN_CODING_TASK = "run_coding_task"

_STRING = StructuredSchema(ValueKind.STRING)
_INTEGER = StructuredSchema(ValueKind.INTEGER)
_BOOLEAN = StructuredSchema(ValueKind.BOOLEAN)
_STRING_ARRAY = StructuredSchema(ValueKind.ARRAY, items=_STRING)

_COMMAND = StructuredSchema(
    ValueKind.OBJECT,
    {
        "argv": _STRING_ARRAY,
        "exit_status": _INTEGER,
        "stdout": _STRING,
        "stderr": _STRING,
        "timed_out": _BOOLEAN,
        "permitted": _BOOLEAN,
    },
    ("argv", "exit_status", "stdout", "stderr", "timed_out", "permitted"),
    extra_properties=False,
)


DEFINITION = CapabilityDefinition(
    RUN_CODING_TASK,
    "Execute one bounded software-engineering job in an assigned worktree: "
    "inspect and edit files there, run permitted tests and git inspection, "
    "and return structured evidence. Does not merge, push, deploy, or request "
    "an external review.",
    StructuredSchema(
        ValueKind.OBJECT,
        {
            "task": _STRING,
            "worktree": _STRING,
            "acceptance_criteria": _STRING_ARRAY,
            "context": _STRING,
            "test_guidance": _STRING,
            "step_budget": _INTEGER,
        },
        ("task", "worktree"),
        extra_properties=False,
    ),
    StructuredSchema(
        ValueKind.OBJECT,
        {
            "status": _STRING,
            "summary": _STRING,
            "files_changed": _STRING_ARRAY,
            "file_count": _INTEGER,
            "command_count": _INTEGER,
            "commands": StructuredSchema(ValueKind.ARRAY, items=_COMMAND),
            "tests_run": _BOOLEAN,
            "tests_passed": _BOOLEAN,
            "git_status": _STRING,
            "git_diff": _STRING,
            "unresolved_issues": _STRING_ARRAY,
            "external_review_recommended": _BOOLEAN,
            "unresolved_count": _INTEGER,
            "diff_digest": _STRING,
            "finished_at": _STRING,
        },
        (
            "status",
            "summary",
            "files_changed",
            "commands",
            "tests_run",
            "git_status",
            "git_diff",
            "unresolved_issues",
            "external_review_recommended",
        ),
        extra_properties=False,
    ),
    SideEffect.EFFECTFUL,
    CODING_FAILURES,
    durable_input_fields=("task", "worktree"),
)


def build_coding_executors(
    run_job: Callable[[CodingRequest], Any],
    call_id_source: Callable[[], str],
) -> Mapping[str, Callable[[Mapping[str, Any]], CapabilityResult]]:
    """Wire the one coding outcome to its structured capability result."""

    def run(arguments: Mapping[str, Any]) -> CapabilityResult:
        call_id = call_id_source()
        try:
            criteria = arguments.get("acceptance_criteria") or ()
            request = CodingRequest(
                task=str(arguments["task"]),
                worktree=str(arguments["worktree"]),
                acceptance_criteria=tuple(str(item) for item in criteria),
                context=str(arguments.get("context") or ""),
                test_guidance=str(arguments.get("test_guidance") or ""),
                step_budget=int(
                    DEFAULT_STEP_BUDGET
                    if arguments.get("step_budget") is None
                    else arguments["step_budget"]
                ),
            )
        except (KeyError, TypeError, ValueError):
            return _failed(call_id, "arguments_unusable")

        try:
            outcome = run_job(request)
        except CodingError as error:
            return _failed(call_id, error.code)
        except Exception:  # noqa: BLE001 - unclassified is still a fact
            LOGGER.warning("Coding job failed")
            return _failed(call_id, "coding_unavailable")

        values = outcome.as_values()
        if outcome.status != "succeeded":
            code = "step_budget_exhausted" if (
                "step_budget_exhausted" in outcome.unresolved_issues
            ) else "task_failed"
            return CapabilityResult(
                call_id,
                RUN_CODING_TASK,
                CapabilityResultState.FAILED,
                values,
                failure={"code": code, "status": outcome.status},
                durable_values=outcome.durable_values(),
                provenance=RetentionPolicy().non_mail(
                    ContentOrigin.EXTERNAL, outcome.finished_at
                ),
            )
        return CapabilityResult(
            call_id,
            RUN_CODING_TASK,
            CapabilityResultState.SUCCEEDED,
            values,
            durable_values=outcome.durable_values(),
            provenance=RetentionPolicy().non_mail(
                ContentOrigin.EXTERNAL, outcome.finished_at
            ),
        )

    return {RUN_CODING_TASK: run}


def _failed(call_id: str, code: str) -> CapabilityResult:
    return CapabilityResult(
        call_id,
        RUN_CODING_TASK,
        CapabilityResultState.FAILED,
        failure={"code": code},
    )
