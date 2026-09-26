"""One language-blind primitive for a Core-delegated coding job, under D-028.

Reached the way every capability is: AL/X proposes a structured call, the
broker validates it, the safety gate authorises it under `coding.execute`, and
the executor runs the bounded job. The capability edits only the prepared
feature branch in the configured canonical checkout, runs only permitted
development commands, and returns evidence.
It does not merge, push, deploy, or request a review.
"""

from __future__ import annotations

import logging
import json
from collections.abc import Callable
from dataclasses import replace
from pathlib import Path
from typing import Any, Mapping

from alx.contracts import (
    CapabilityAttemptDisposition,
    GoalState,
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
    MAX_BRANCH_NAME_CHARACTERS,
    MAX_COMMIT_MESSAGE_CHARACTERS,
    MAX_STEP_BUDGET,
    MAX_REPAIR_BRANCH_ATTEMPTS,
    MAX_TASK_CHARACTERS,
    BranchContinuation,
    CodingError,
    CodingRequest,
    job_id_permitted,
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


_BASELINE = StructuredSchema(
    ValueKind.OBJECT,
    {
        "branch": _STRING,
        "head_sha": _STRING,
        "inherited_dirty": _STRING_ARRAY,
        "clean": _BOOLEAN,
        "detached": _BOOLEAN,
    },
    ("branch", "head_sha", "inherited_dirty", "clean", "detached"),
    extra_properties=False,
)

_COMMIT_RECORD = StructuredSchema(
    ValueKind.OBJECT,
    {
        "branch": _STRING,
        "commit_sha": _STRING,
        "committed_files": _STRING_ARRAY,
        "worktree_clean": _BOOLEAN,
    },
    ("branch", "commit_sha", "committed_files", "worktree_clean"),
    extra_properties=False,
)

# What the local reviewer said about this job's candidate. Advisory: a finding
# is the reviewer's opinion for Core to weigh, not a verdict on the work.
_REVIEW_ATTEMPT = StructuredSchema(
    ValueKind.OBJECT,
    {
        "attempt": _INTEGER,
        "reason": _STRING,
        "error_message": _STRING,
        "raw_excerpt": _STRING,
    },
    ("attempt", "reason", "error_message", "raw_excerpt"),
    extra_properties=False,
)

_REVIEW_FINDING = StructuredSchema(
    ValueKind.OBJECT,
    {
        "severity": _STRING,
        "title": _STRING,
        "evidence": _STRING,
        "correction": _STRING,
    },
    ("severity", "title", "evidence", "correction"),
    extra_properties=False,
)

_VERIFICATION_CHECK = StructuredSchema(
    ValueKind.OBJECT,
    {
        "name": _STRING,
        "argv": _STRING_ARRAY,
        "reason": _STRING,
        "ran": _BOOLEAN,
        "passed": _BOOLEAN,
        # "command" ran through the allowlisted executor; "content" was
        # performed in process over the job's own files, which is the half
        # `git diff --check` cannot see because a new file is still untracked.
        "kind": _STRING,
        # What a failed content check found, so Core is told why rather than
        # having to infer it from a bare false.
        "findings": _STRING_ARRAY,
    },
    ("name", "argv", "reason", "ran", "passed", "kind", "findings"),
    extra_properties=False,
)

# What the job was required to verify and how each check ended. Core reads
# this to know why a change was or was not committed; `tests_run` and
# `tests_passed` remain beside it as the test-specific facts.
_VERIFICATION = StructuredSchema(
    ValueKind.OBJECT,
    {
        "required": _STRING_ARRAY,
        "ran": _STRING_ARRAY,
        "failed": _STRING_ARRAY,
        "all_required_passed": _BOOLEAN,
        "checks": StructuredSchema(ValueKind.ARRAY, items=_VERIFICATION_CHECK),
    },
    ("required", "ran", "failed", "all_required_passed", "checks"),
    extra_properties=False,
)


DEFINITION = CapabilityDefinition(
    RUN_CODING_TASK,
    # The two bounds the runtime enforces are stated here because the
    # structured schema cannot carry them: StructuredSchema describes kinds,
    # not numeric ranges or path semantics. MAX_STEP_BUDGET is interpolated
    # rather than written out, so the stated ceiling cannot drift from the one
    # the executor applies.
    "Execute one bounded software-engineering job in the configured canonical "
    "checkout. By default it requires clean main, then "
    "creates and switches to the requested new feature branch before the coding "
    "session may edit. With continue_goal_branch=true, it instead verifies the "
    "checked-out branch and HEAD belong to this active goal's successful coding "
    "commits and the checkout is clean; repair_branch must match that branch. "
    "AL/X must position the checkout first. Only one implementation job may hold "
    "the checkout at a "
    "time. No repository path is accepted. repair_branch and commit_message are "
    "required; the job's own changed files are committed on that branch once every "
    f"required check passes. step_budget is optional and must be from 1 to {MAX_STEP_BUDGET}. "
    "Required verification may or "
    "may not include tests, and includes the law gates when the change touches "
    "the paths they govern, "
    "returning branch and commit_sha. Only files this job changed are "
    "committed: the commit is refused rather than widened if the index or the "
    "resulting tree holds any path outside that authorised set. It still does "
    "not push, merge, deploy, or request an external review. "
    "A local reviewer advises on the candidate and the job corrects what it "
    "raises, but its findings never block the commit: findings it did not "
    "resolve come back in review_findings with external_review_recommended "
    "true and local_review_material_findings in unresolved_issues, so you "
    "judge them against the committed work rather than being handed a refusal "
    "in place of it. A reviewer schema, timeout, or provider failure is not a "
    "finding: review_classification is infrastructure, that same diff is "
    "reviewed again up to two more times. review_attempts carries each failed "
    "attempt's reason, error_message, and bounded raw_excerpt, including when "
    "a later attempt succeeds and the job commits. If those attempts are "
    "exhausted the job stays uncommitted with diff_preserved and the branch "
    "name. AL/X may supply only resume_job_id for a failed or cancelled call "
    "from this active goal to retry only its recorded stage after the checkout "
    "matches the durable branch, HEAD, and full state digest. The user can "
    "stop an active job through the structured coding cancellation control; "
    "cancellation preserves its branch and diff and returns a checkpoint.",
    StructuredSchema(
        ValueKind.OBJECT,
        {
            "task": _STRING,
            "acceptance_criteria": _STRING_ARRAY,
            "context": _STRING,
            "test_guidance": _STRING,
            "step_budget": _INTEGER,
            "blocked_paths": _STRING_ARRAY,
            "repair_branch": _STRING,
            "commit_message": _STRING,
            "continue_goal_branch": _BOOLEAN,
            "resume_job_id": _STRING,
        },
        (),
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
            "review_findings": StructuredSchema(
                ValueKind.ARRAY, items=_REVIEW_FINDING
            ),
            "material_review_findings": _INTEGER,
            "verification": _VERIFICATION,
            "all_required_verification_passed": _BOOLEAN,
            "git_status": _STRING,
            "git_diff": _STRING,
            "unresolved_issues": _STRING_ARRAY,
            "external_review_recommended": _BOOLEAN,
            "unresolved_count": _INTEGER,
            "diff_digest": _STRING,
            "finished_at": _STRING,
            "baseline": _BASELINE,
            "commit": _COMMIT_RECORD,
            "branch": _STRING,
            "commit_sha": _STRING,
            # Set when a review attempt failed for schema, timeout, or provider
            # reasons, including when a later attempt succeeded. Absent when
            # the only review evidence is advisory findings.
            "review_classification": _STRING,
            "diff_preserved": _BOOLEAN,
            "uncommitted": _BOOLEAN,
            "review_attempts": StructuredSchema(
                ValueKind.ARRAY, items=_REVIEW_ATTEMPT
            ),
            "checkpoint": _STRING,
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
    durable_input_fields=(
        "task", "context", "acceptance_criteria", "test_guidance",
        "step_budget", "blocked_paths", "repair_branch", "commit_message",
        "continue_goal_branch",
        "resume_job_id",
    ),
)

# First match wins. Not CODING_FAILURES: sandbox_unusable / session_failed
# sit late there and would invert this order. task_failed is the fallback
# for undeclared issues such as no_files_changed, not an entry.
_OUTCOME_ISSUE_CODES = (
    "coding_cancelled",
    "sandbox_unusable",
    "session_failed",
    "coding_unavailable",
    "provider_failed",
    "plan_unusable",
    "planning_failed",
    "review_failed",
    "local_review_material_findings",
    "unrelated_changes_staged",
    "required_verification_failed",
    "git_refused",
    "git_unavailable",
)


def goal_coding_branch(state: GoalState | None) -> BranchContinuation | None:
    """Derive ownership solely from this goal's durable successful CA commits."""
    commits: list[tuple[str, str]] = []
    for attempt in (() if state is None else state.attempts):
        result = attempt.result
        if (
            attempt.disposition is not CapabilityAttemptDisposition.EXECUTED
            or not attempt.implementation_invoked
            or attempt.call is None
            or attempt.call.capability_id != RUN_CODING_TASK
            or result is None
            or result.state is not CapabilityResultState.SUCCEEDED
        ):
            continue
        commit = result.durable_values.get("commit")
        if not isinstance(commit, Mapping):
            continue
        branch, sha = commit.get("branch"), commit.get("commit_sha")
        try:
            validated = BranchContinuation(branch, frozenset({sha}))
        except (TypeError, ValueError):
            return None
        commits.append((validated.branch, sha))
    if not commits:
        return None
    branch = commits[-1][0]
    return BranchContinuation(
        branch, frozenset(sha for name, sha in commits if name == branch)
    )


def build_coding_executors(
    run_job: Callable[[CodingRequest], Any],
    call_id_source: Callable[[], str],
    goal_state_source: Callable[[], GoalState | None] = lambda: None,
) -> Mapping[str, Callable[[Mapping[str, Any]], CapabilityResult]]:
    """Wire the one coding outcome to its structured capability result."""

    def run(arguments: Mapping[str, Any]) -> CapabilityResult:
        call_id = call_id_source()
        if not isinstance(arguments, Mapping):
            return _failed(call_id, "arguments_unusable", **_argument_failure(
                None, "not_object", "arguments must be an object"
            ))
        resume_id = arguments.get("resume_job_id")
        if "resume_job_id" in arguments and (
            not isinstance(resume_id, str) or not job_id_permitted(resume_id)
        ):
            return _failed(call_id, "arguments_unusable", **_argument_failure(
                "resume_job_id", "unsafe", "resume_job_id is invalid"
            ))
        if not resume_id:
            # New jobs retain their original required fields. A resume gets
            # these fields from its durable same-goal ancestor instead.
            request, argument_failure = parse_coding_arguments(arguments, call_id)
            if argument_failure is not None:
                return _failed(call_id, "arguments_unusable", **argument_failure)
        if resume_id:
            state = goal_state_source()
            recorded = {
                item.call.call_id: item
                for item in (() if state is None else state.attempts)
                if item.call is not None and item.call.capability_id == RUN_CODING_TASK
                and item.result is not None
                and item.result.state is CapabilityResultState.FAILED
            }
            previous = recorded.get(resume_id)
            if previous is None:
                return _failed(call_id, "git_refused", reason_code="resume_ownership_unproven",
                               implementation_reached=False)
            raw_checkpoint = previous.result.durable_values.get("checkpoint")
            try:
                checkpoint = json.loads(raw_checkpoint)
            except (TypeError, ValueError):
                checkpoint = None
            # A resumed attempt records only what Core supplied on that call.
            # Walk its durable same-goal ancestry to recover the complete
            # original request, while using the immediate attempt's checkpoint
            # for the current stage and checkout proof.
            origin = previous
            seen: set[str] = set()
            while True:
                origin_id = origin.call.call_id
                if origin_id in seen:
                    return _failed(call_id, "git_refused", reason_code="resume_ownership_unproven",
                                   implementation_reached=False)
                seen.add(origin_id)
                parent_id = origin.call.arguments.get("resume_job_id")
                if not parent_id:
                    break
                if not isinstance(parent_id, str) or not job_id_permitted(parent_id):
                    return _failed(call_id, "git_refused", reason_code="resume_ownership_unproven",
                                   implementation_reached=False)
                origin = recorded.get(parent_id)
                if origin is None:
                    return _failed(call_id, "git_refused", reason_code="resume_ownership_unproven",
                                   implementation_reached=False)
            original = origin.call.arguments
            original_request, original_failure = parse_coding_arguments(original, call_id)
            if original_failure is not None or original_request is None:
                return _failed(call_id, "git_refused", reason_code="resume_original_request_invalid",
                               implementation_reached=False)
            request, argument_failure = parse_coding_arguments(
                {**original, **arguments}, call_id
            )
            if argument_failure is not None:
                return _failed(call_id, "arguments_unusable", **argument_failure)
            requested_branch = original_request.repair_branch.strip()
            prepared_branches = {requested_branch} | {
                f"{requested_branch}-{number}"
                for number in range(2, MAX_REPAIR_BRANCH_ATTEMPTS + 1)
            }
            if (not isinstance(checkpoint, dict) or checkpoint.get("job_id") != resume_id
                    or checkpoint.get("branch") not in prepared_branches
                    or checkpoint.get("stage") not in {"planning", "execution", "review", "test", "commit"}
                    or arguments.get("continue_goal_branch", False)):
                return _failed(call_id, "git_refused", reason_code="resume_checkpoint_invalid",
                               implementation_reached=False)
            for field in ("task", "context", "acceptance_criteria", "test_guidance",
                          "step_budget", "blocked_paths", "commit_message"):
                if field in arguments and getattr(request, field) != getattr(original_request, field):
                    return _failed(call_id, "arguments_unusable", reason_code="resume_request_changed",
                                   implementation_reached=False)
            if request.repair_branch.strip() not in {requested_branch, checkpoint["branch"]}:
                return _failed(call_id, "arguments_unusable", reason_code="resume_request_changed",
                               implementation_reached=False)
            request = replace(original_request, repair_branch=checkpoint["branch"],
                              resume_checkpoint=checkpoint)
        elif arguments.get("continue_goal_branch", False):
            continuation = goal_coding_branch(goal_state_source())
            if continuation is None or request.repair_branch != continuation.branch:
                return _failed(
                    call_id, "git_refused",
                    reason_code="continuation_ownership_unproven",
                    implementation_reached=False,
                )
            request = replace(request, continuation=continuation)

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
            code = next(
                (item for item in _OUTCOME_ISSUE_CODES if item in issues),
                "task_failed",
            )
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
    job_id: str,
) -> tuple[CodingRequest | None, dict[str, object] | None]:
    """Validate run_coding_task arguments field by field.

    The schema already rejects the wrong JSON kinds. These checks name the
    field and bound that CodingRequest would otherwise swallow as a bare
    arguments_unusable, so Core can correct the call.

    `job_id` is supplied by the executor, never by the caller. An argument
    spelled `job_id` or `worktree` is refused rather than ignored: silently
    dropping it would let a model believe it had chosen where the job runs.
    """
    if not isinstance(arguments, Mapping):
        return None, _argument_failure(
            None, "not_object", "arguments must be an object"
        )
    for reserved in ("worktree", "job_id", "continuation"):
        if reserved in arguments:
            return None, _argument_failure(
                reserved,
                "not_accepted",
                f"{reserved} is assigned by AL/X and cannot be supplied",
            )
    if not isinstance(arguments.get("continue_goal_branch", False), bool):
        return None, _argument_failure(
            "continue_goal_branch", "not_boolean", "continue_goal_branch must be a boolean"
        )
    if "resume_job_id" in arguments and (
        not isinstance(arguments["resume_job_id"], str)
        or not job_id_permitted(arguments["resume_job_id"])
    ):
        return None, _argument_failure("resume_job_id", "unsafe", "resume_job_id is invalid")
    if not isinstance(job_id, str) or not job_id.strip():
        return None, _argument_failure(
            "job_id", "missing", "job_id was not assigned"
        )
    # Broker call ids are UUID-shaped today, but the durable identity remains
    # validated rather than assumed.
    if not job_id_permitted(job_id):
        return None, _argument_failure(
            "job_id",
            "unsafe",
            "the assigned job_id cannot be used as a workspace identity",
        )
    task, error = _required_string(arguments, "task", MAX_TASK_CHARACTERS)
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
    branch, error = _optional_string(
        arguments, "repair_branch", MAX_BRANCH_NAME_CHARACTERS
    )
    if error is not None:
        return None, error
    message, error = _optional_string(
        arguments, "commit_message", MAX_COMMIT_MESSAGE_CHARACTERS
    )
    if error is not None:
        return None, error
    if not branch.strip():
        return None, _argument_failure(
            "repair_branch",
            "missing",
            "repair_branch is required",
        )
    if not message.strip():
        return None, _argument_failure(
            "commit_message",
            "missing",
            "commit_message is required",
        )
    return (
        CodingRequest(
            task=task,
            job_id=job_id,
            acceptance_criteria=criteria,
            context=context,
            test_guidance=guidance,
            step_budget=budget,
            blocked_paths=blocked,
            repair_branch=branch,
            commit_message=message,
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
    capability = str(fields.pop("capability", RUN_CODING_TASK))
    failure = {"code": code}
    failure.update(fields)
    return CapabilityResult(
        call_id,
        capability,
        CapabilityResultState.FAILED,
        failure=failure,
    )
