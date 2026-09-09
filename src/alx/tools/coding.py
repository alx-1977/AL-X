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
from pathlib import Path
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
    MAX_BLOCKED_PATHS,
    MAX_BLOCKED_PATH_CHARACTERS,
    MAX_CONTEXT_CHARACTERS,
    MAX_CRITERIA,
    MAX_CRITERION_CHARACTERS,
    MAX_STEP_BUDGET,
    MAX_TASK_CHARACTERS,
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
            "blocked_paths": _STRING_ARRAY,
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
            "preexisting_dirty": _STRING_ARRAY,
            "plan_summary": _STRING,
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
            "plan_summary",
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
    durable_input_fields=("task", "worktree", "blocked_paths"),
)


def build_coding_executors(
    run_job: Callable[[CodingRequest], Any],
    call_id_source: Callable[[], str],
) -> Mapping[str, Callable[[Mapping[str, Any]], CapabilityResult]]:
    """Wire the one coding outcome to its structured capability result."""

    def run(arguments: Mapping[str, Any]) -> CapabilityResult:
        call_id = call_id_source()
        request, argument_failure = parse_coding_arguments(arguments)
        if argument_failure is not None:
            return _failed(call_id, "arguments_unusable", **argument_failure)

        try:
            outcome = run_job(request)
        except CodingError as error:
            return _failed(call_id, error.code, **error.details)
        except Exception:  # noqa: BLE001 - unclassified is still a fact
            LOGGER.warning("Coding job failed")
            return _failed(call_id, "coding_unavailable")

        values = outcome.as_values()
        if outcome.status != "succeeded":
            issues = outcome.unresolved_issues
            if "step_budget_exhausted" in issues:
                code = "step_budget_exhausted"
            elif "command_budget_exhausted" in issues:
                code = "command_budget_exhausted"
            elif "provider_failed" in issues:
                code = "provider_failed"
            elif "plan_unusable" in issues:
                code = "plan_unusable"
            else:
                code = "task_failed"
            return CapabilityResult(
                call_id,
                RUN_CODING_TASK,
                CapabilityResultState.FAILED,
                values,
                failure={
                    "code": code,
                    "status": outcome.status,
                    **{
                        key: value
                        for key, value in (outcome.diagnostics or {}).items()
                        if key not in {"code", "status"}
                    },
                },
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


def parse_coding_arguments(
    arguments: Mapping[str, Any],
) -> tuple[CodingRequest | None, dict[str, object] | None]:
    """Validate run_coding_task arguments field by field.

    The schema already rejects the wrong JSON kinds. These checks name the
    field and bound that CodingRequest would otherwise swallow as a bare
    arguments_unusable, so Core can correct the call.
    """
    if not isinstance(arguments, Mapping):
        return None, _argument_failure(
            None, "not_object", "arguments must be an object"
        )
    task, error = _required_string(arguments, "task", MAX_TASK_CHARACTERS)
    if error is not None:
        return None, error
    worktree, error = _required_string(arguments, "worktree", None)
    if error is not None:
        return None, error
    context, error = _optional_string(
        arguments, "context", MAX_CONTEXT_CHARACTERS
    )
    if error is not None:
        return None, error
    guidance, error = _optional_string(
        arguments, "test_guidance", MAX_CONTEXT_CHARACTERS
    )
    if error is not None:
        return None, error
    criteria, error = _optional_criteria(arguments)
    if error is not None:
        return None, error
    budget, error = _optional_step_budget(arguments)
    if error is not None:
        return None, error
    blocked, error = _optional_blocked_paths(arguments)
    if error is not None:
        return None, error
    return (
        CodingRequest(
            task,
            worktree,
            criteria,
            context,
            guidance,
            budget,
            blocked,
        ),
        None,
    )


def _required_string(
    arguments: Mapping[str, Any], field: str, maximum: int | None
) -> tuple[str | None, dict[str, object] | None]:
    if field not in arguments:
        return None, _argument_failure(field, "missing", f"{field} is required")
    value = arguments[field]
    if not isinstance(value, str) or not value.strip():
        return None, _argument_failure(
            field, "blank", f"{field} must be a non-blank string"
        )
    if maximum is not None and len(value) > maximum:
        return None, _argument_failure(
            field,
            "too_long",
            f"{field} must be at most {maximum} characters",
            received_length=len(value),
        )
    return value, None


def _optional_string(
    arguments: Mapping[str, Any], field: str, maximum: int
) -> tuple[str, dict[str, object] | None]:
    if field not in arguments or arguments[field] is None:
        return "", None
    value = arguments[field]
    if not isinstance(value, str):
        return "", _argument_failure(
            field, "not_string", f"{field} must be a string"
        )
    if len(value) > maximum:
        return "", _argument_failure(
            field,
            "too_long",
            f"{field} must be at most {maximum} characters",
            received_length=len(value),
        )
    return value, None


def _optional_criteria(
    arguments: Mapping[str, Any],
) -> tuple[tuple[str, ...], dict[str, object] | None]:
    if (
        "acceptance_criteria" not in arguments
        or arguments["acceptance_criteria"] is None
    ):
        return (), None
    raw = arguments["acceptance_criteria"]
    if not isinstance(raw, (list, tuple)):
        return (), _argument_failure(
            "acceptance_criteria",
            "not_string_array",
            "acceptance_criteria must be an array of strings",
        )
    if len(raw) > MAX_CRITERIA:
        return (), _argument_failure(
            "acceptance_criteria",
            "too_many",
            f"acceptance_criteria must have at most {MAX_CRITERIA} items",
            received_count=len(raw),
        )
    criteria: list[str] = []
    for item in raw:
        if not isinstance(item, str) or not item.strip():
            return (), _argument_failure(
                "acceptance_criteria",
                "blank_item",
                "acceptance_criteria items must be non-blank strings",
            )
        if len(item) > MAX_CRITERION_CHARACTERS:
            return (), _argument_failure(
                "acceptance_criteria",
                "item_too_long",
                "acceptance_criteria items must be at most "
                f"{MAX_CRITERION_CHARACTERS} characters",
                received_length=len(item),
            )
        criteria.append(item)
    return tuple(criteria), None


def _optional_step_budget(
    arguments: Mapping[str, Any],
) -> tuple[int, dict[str, object] | None]:
    if "step_budget" not in arguments or arguments["step_budget"] is None:
        return DEFAULT_STEP_BUDGET, None
    value = arguments["step_budget"]
    if not isinstance(value, int) or isinstance(value, bool):
        return 0, _argument_failure(
            "step_budget", "not_integer", "step_budget must be an integer"
        )
    if not 1 <= value <= MAX_STEP_BUDGET:
        return 0, _argument_failure(
            "step_budget",
            "out_of_range",
            f"step_budget must be an integer from 1 to {MAX_STEP_BUDGET}",
            received=value,
        )
    return value, None


def _optional_blocked_paths(
    arguments: Mapping[str, Any],
) -> tuple[tuple[str, ...], dict[str, object] | None]:
    if "blocked_paths" not in arguments or arguments["blocked_paths"] is None:
        return (), None
    raw = arguments["blocked_paths"]
    if not isinstance(raw, (list, tuple)):
        return (), _argument_failure(
            "blocked_paths",
            "not_string_array",
            "blocked_paths must be an array of strings",
        )
    if len(raw) > MAX_BLOCKED_PATHS:
        return (), _argument_failure(
            "blocked_paths",
            "too_many",
            f"blocked_paths must have at most {MAX_BLOCKED_PATHS} items",
            received_count=len(raw),
        )
    blocked: list[str] = []
    for item in raw:
        if not isinstance(item, str) or not item.strip():
            return (), _argument_failure(
                "blocked_paths",
                "blank_item",
                "blocked_paths items must be non-blank strings",
            )
        if len(item) > MAX_BLOCKED_PATH_CHARACTERS:
            return (), _argument_failure(
                "blocked_paths",
                "item_too_long",
                "blocked_paths items must be at most "
                f"{MAX_BLOCKED_PATH_CHARACTERS} characters",
                received_length=len(item),
            )
        if Path(item).is_absolute():
            return (), _argument_failure(
                "blocked_paths",
                "absolute",
                "blocked_paths must be worktree-relative",
            )
        blocked.append(item.strip())
    return tuple(blocked), None


def _argument_failure(
    field: str | None,
    reason_code: str,
    detail: str,
    **safe: object,
) -> dict[str, object]:
    failure: dict[str, object] = {
        "reason_code": reason_code,
        "detail": detail,
    }
    if field is not None:
        failure["invalid_field"] = field
    for key, value in safe.items():
        if isinstance(value, (int, str)) and not isinstance(value, bool):
            if isinstance(value, str) and len(value) > 80:
                continue
            failure[key] = value
    return failure


def _failed(call_id: str, code: str, **fields: object) -> CapabilityResult:
    failure = {"code": code}
    failure.update(fields)
    return CapabilityResult(
        call_id,
        RUN_CODING_TASK,
        CapabilityResultState.FAILED,
        failure=failure,
    )
