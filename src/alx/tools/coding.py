"""One language-blind primitive for a Core-delegated coding job, under D-028.

Reached the way every capability is: AL/X proposes a structured call, the
broker validates it, the safety gate authorises it under `coding.execute`, and
the executor runs the bounded job. The default path edits only the prepared
feature branch in the configured canonical checkout, runs only permitted
development commands, and returns evidence.
It does not merge, push, deploy, or request a review. D-062 firmware jobs use
the same agent in isolated allowlisted clones, compile locally, commit and push
only their new feature branch; they never change firmware main.
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
    MAX_BLOCKED_PATHS,
    MAX_BLOCKED_PATH_CHARACTERS,
    MAX_CONTEXT_CHARACTERS,
    MAX_CRITERIA,
    MAX_REQUESTED_CHECKS,
    MAX_REQUESTED_CHECK_ARGUMENT_CHARACTERS,
    MAX_REQUESTED_CHECK_ARGUMENTS,
    MAX_CRITERION_CHARACTERS,
    MAX_BRANCH_NAME_CHARACTERS,
    MAX_COMMIT_MESSAGE_CHARACTERS,
    MAX_REPAIR_BRANCH_ATTEMPTS,
    MAX_TASK_CHARACTERS,
    BranchContinuation,
    CodingError,
    CodingRequest,
    job_id_permitted,
)


LOGGER = logging.getLogger(__name__)

RUN_CODING_TASK = "run_coding_task"
STOP_CODING_JOB = "stop_coding_job"

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

_NO_CHANGE_EVIDENCE = StructuredSchema(
    ValueKind.OBJECT,
    {
        "branch": _STRING,
        "head_sha": _STRING,
        "checkout_clean": _BOOLEAN,
        "session_completed": _BOOLEAN,
    },
    ("branch", "head_sha", "checkout_clean", "session_completed"),
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
        "baseline": _STRING,
    },
    ("name", "argv", "reason", "ran", "passed", "kind", "findings", "baseline"),
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
    # Bounds the runtime enforces are stated here because the structured
    # schema cannot carry them: StructuredSchema describes kinds, not numeric
    # ranges or path semantics. Constants are interpolated rather than written
    # out, so a stated bound cannot drift from the one the executor applies.
    "Execute one bounded software-engineering job in the configured canonical "
    "checkout. "
    "With firmware_repository, select a configured allowlisted firmware GitHub repository "
    "(initially alx-1977/RoomReader_V2). D-062: use an isolated job clone outside AL/X, "
    "fresh origin main, new feature branch, local Particle compilation as required "
    "verification, then commit and push only that feature branch. Never firmware main. "
    "A failed firmware job may resume its durable clone with resume_job_id; "
    "continue_goal_branch is unavailable for firmware. The following canonical "
    "checkout behaviour applies when firmware_repository is absent. "
    "For a tiny improvement you selected yourself, check current main with the "
    "available repository evidence before dispatch when that cheaply establishes "
    "whether the change is still needed. A completed session that changes no "
    "files can return no_change_required with its report and clean-checkout "
    "evidence for your judgement. It leaves a clean feature branch; position "
    "the checkout on main through repository_operation before a new job. "
    "By default the job requires clean main, then "
    "creates and switches to the requested new feature branch before the coding "
    "session may edit. With continue_goal_branch=true, it instead continues on "
    "repair_branch without switching: repair_branch must be a branch an earlier "
    "coding job of this active goal created or worked on, whether that job "
    "succeeded or failed, it may not be main, and the checkout must already be "
    "on it and clean. Its HEAD may be any commit, including a hand commit or a "
    "rebased, force-pushed pull request branch. A branch no coding job of this "
    "goal used is refused. "
    "AL/X must position the checkout first. Only one implementation job may hold "
    "the checkout at a "
    "time. No repository path is accepted. repair_branch and commit_message are "
    "required; the job's own changed files are committed on that branch once every "
    "required check passes. Required verification may or "
    "may not include tests, and includes the law gates when the change touches "
    "the paths they govern. "
    f"verification_commands takes up to {MAX_REQUESTED_CHECKS} exact argv arrays "
    "you require to run after the session, beside those derived checks; each "
    "must pass the command allowlist, and one that is refused, fails or never "
    "runs fails verification. The whole suite in parallel is "
    "[\"python\", \"-m\", \"pytest\", \"-q\", \"--rootdir=tests\", "
    "\"-p\", \"no:cacheprovider\", \"-n\", \"auto\"] (about 1.5 minutes; "
    "without -n auto, about 6.5). "
    "Derived checks already include every test file that imports a changed "
    "module. test_guidance is prose for the session and runs "
    "nothing. summary is the coding session's own report, written before any "
    "check ran: whatever the session ran for itself, only AL/X's checks count. "
    "verification, tests_run and commit record what actually ran and passed. "
    "A passed job is committed, "
    "returning branch and commit_sha. Only files this job changed are "
    "committed: the commit is refused rather than widened if the index or the "
    "resulting tree holds any path outside that authorised set. It still does "
    "not push, merge, deploy, or request an external review. "
    "No second model reviews the diff: the checks, and the external review "
    "you request on the pull request, are the review. To address that review, "
    "give review_pull_request together with continue_goal_branch=true or a "
    "resume, since a review is of an existing branch; a new job with it is "
    "refused as arguments_unusable (review_pull_request_requires_continuation). "
    "The job reads the reviewer's summary and "
    "comments for the exact revision it starts from and hands them to the "
    "session word for word, with the instruction to reproduce each finding in "
    "a failing test before fixing it. Do not retell the findings in task; name "
    "the pull request. If no finished review covers that revision the job "
    "refuses with review_unavailable before changing anything. "
    "AL/X may supply only resume_job_id for a failed or cancelled call "
    "from this active goal to retry only its recorded stage after the checkout "
    "matches the durable branch, HEAD, and full state digest. With a resume of "
    "a failed job whose recorded stage is execution or test, "
    "AL/X may add corrective_action: her diagnosis of that recorded failure and "
    "the specific repair she chose. The job then reruns its required checks on "
    "the exact checkout, hands the coding session the recorded failure, the "
    "reproduced check output, and her corrective_action, and continues through "
    "verification and commit unchanged. Repeating an attempt without "
    "a new diagnosis is bounded per failure episode, and each recorded failure "
    "admits a bounded number of distinct corrections. stop_coding_job stops "
    "the running job; cancellation preserves its branch and diff and returns "
    "a checkpoint.",
    StructuredSchema(
        ValueKind.OBJECT,
        {
            "task": _STRING,
            "acceptance_criteria": _STRING_ARRAY,
            "context": _STRING,
            "test_guidance": _STRING,
            "verification_commands": StructuredSchema(
                ValueKind.ARRAY, items=_STRING_ARRAY
            ),
            "blocked_paths": _STRING_ARRAY,
            "repair_branch": _STRING,
            "commit_message": _STRING,
            "continue_goal_branch": _BOOLEAN,
            "resume_job_id": _STRING,
            "corrective_action": _STRING,
            "review_pull_request": _INTEGER,
            "firmware_repository": _STRING,
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
            "verification": _VERIFICATION,
            "all_required_verification_passed": _BOOLEAN,
            "git_status": _STRING,
            "git_diff": _STRING,
            "unresolved_issues": _STRING_ARRAY,
            "unresolved_count": _INTEGER,
            "diff_digest": _STRING,
            "finished_at": _STRING,
            "tool": _STRING,
            "model": _STRING,
            "started_at": _STRING,
            "duration_seconds": StructuredSchema(ValueKind.NUMBER),
            "session_report": _STRING,
            "baseline": _BASELINE,
            "no_change_evidence": _NO_CHANGE_EVIDENCE,
            "commit": _COMMIT_RECORD,
            "branch": _STRING,
            "commit_sha": _STRING,
            "diff_preserved": _BOOLEAN,
            "uncommitted": _BOOLEAN,
            "checkpoint": _STRING,
            "firmware_repository": _STRING,
            "interruption_reason": _STRING,
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
        ),
        extra_properties=False,
    ),
    SideEffect.EFFECTFUL,
    CODING_FAILURES,
    durable_input_fields=(
        "task", "context", "acceptance_criteria", "test_guidance",
        "verification_commands",
        "blocked_paths", "repair_branch", "commit_message",
        "continue_goal_branch",
        "resume_job_id", "corrective_action", "review_pull_request", "firmware_repository",
    ),
)

# Stopping is its own capability, not a control beside the conversation: when
# Friedl wants a job stopped he says so, and AL/X decides and calls this. It
# replaced the console's "Stop coding job" button, which was the only way to
# stop a job and bypassed her entirely.
STOP_DEFINITION = CapabilityDefinition(
    STOP_CODING_JOB,
    "Stop the coding job that is running now. It may be called from the goal "
    "whose plan is running the job, which admits no other call while the job "
    "runs, or with no goal selected. job_id, when given, must name that job: the call_id of "
    "its run_coding_task step. The stopped job keeps "
    "its branch and diff and its own result returns a checkpoint. stopped is "
    "false when no job is running, the named job is not the running one, or "
    "the job is already committing.",
    StructuredSchema(
        ValueKind.OBJECT,
        {"job_id": _STRING},
        (),
        extra_properties=False,
    ),
    StructuredSchema(
        ValueKind.OBJECT,
        {"stopped": _BOOLEAN},
        ("stopped",),
        extra_properties=False,
    ),
    SideEffect.EFFECTFUL,
    ("arguments_unusable",),
)

DEFINITIONS = (DEFINITION, STOP_DEFINITION)

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
    "unrelated_changes_staged",
    "required_verification_failed",
    "git_refused",
    "git_unavailable",
)


def goal_coding_branch(
    state: GoalState | None, repair_branch: str
) -> BranchContinuation | None:
    """repair_branch, if this goal's own coding work created or used it.

    Ownership is the branch, not a commit (2026-10-24): a hand commit, or a
    merge that rebased and force-pushed the pull request, moves HEAD without
    making the branch any less this goal's. Every run_coding_task attempt of
    the goal that reached implementation counts, succeeded or failed: the
    branch it recorded, or the repair_branch it was asked for when it recorded
    none. A recorded branch wins because a taken name gets a numbered branch
    (feat/x-2), and the taken feat/x was never this goal's. One malformed
    record proves nothing about its branch and is skipped; it never disables
    continuation for the whole goal.
    """
    for attempt in (() if state is None else state.attempts):
        if (attempt.result is not None and
                attempt.result.recovery_values().get('firmware_repository') is not None):
            # A firmware branch never proves ownership of a branch in AL/X.
            continue
        if repair_branch in _attempt_branches(attempt):
            # Only permitted names reach the set, so this cannot raise.
            return BranchContinuation(repair_branch)
    return None


def _attempt_branches(attempt: Any) -> frozenset[str]:
    """Branches one recorded coding attempt worked on, or none."""
    result = attempt.result
    if (
        attempt.disposition is not CapabilityAttemptDisposition.EXECUTED
        or not attempt.implementation_invoked
        or attempt.call is None
        or attempt.call.capability_id != RUN_CODING_TASK
        or result is None
    ):
        return frozenset()
    failure = result.recovery_failure()
    if isinstance(failure, Mapping) and failure.get("implementation_reached") is False:
        # Refused before the feature branch existed: nothing used it.
        return frozenset()
    values = result.recovery_values()
    commit = values.get("commit")
    no_change = values.get("no_change_evidence")
    try:
        checkpoint = json.loads(values.get("checkpoint"))
    except (TypeError, ValueError):
        checkpoint = None
    recorded = _permitted_branches((
        values.get("branch"),
        commit.get("branch") if isinstance(commit, Mapping) else None,
        checkpoint.get("branch") if isinstance(checkpoint, dict) else None,
        # A job that found nothing to change records its branch only here.
        no_change.get("branch") if isinstance(no_change, Mapping) else None,
    ))
    return recorded or _permitted_branches(
        (attempt.call.arguments.get("repair_branch"),)
    )


def _permitted_branches(candidates: tuple[object, ...]) -> frozenset[str]:
    branches = set()
    for candidate in candidates:
        try:
            branches.add(BranchContinuation(candidate).branch)
        except (TypeError, ValueError):
            continue
    return frozenset(branches)


def build_coding_executors(
    run_job: Callable[[CodingRequest], Any],
    call_id_source: Callable[[], str],
    goal_state_source: Callable[[], GoalState | None] = lambda: None,
    stop_job: Callable[[str | None], bool] | None = None,
) -> Mapping[str, Callable[[Mapping[str, Any]], CapabilityResult]]:
    """Wire the coding outcomes to their structured capability results."""

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
                and (
                    item.result.state is CapabilityResultState.FAILED
                    or (item.result.state is CapabilityResultState.PARTIAL
                        and item.result.recovery_values().get("status") == "interrupted")
                )
            }
            previous = recorded.get(resume_id)
            if previous is None:
                return _failed(call_id, "git_refused", reason_code="resume_ownership_unproven",
                               implementation_reached=False)
            raw_checkpoint = previous.result.recovery_values().get("checkpoint")
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
                          "blocked_paths", "commit_message"):
                if field in arguments and getattr(request, field) != getattr(original_request, field):
                    return _failed(call_id, "arguments_unusable", reason_code="resume_request_changed",
                                   implementation_reached=False)
            # Required checks are part of what the job is verified by, so a
            # resume may not change them either.
            if ("verification_commands" in arguments
                    and request.requested_checks != original_request.requested_checks):
                return _failed(call_id, "arguments_unusable", reason_code="resume_request_changed",
                               implementation_reached=False)
            if request.repair_branch.strip() not in {requested_branch, checkpoint["branch"]}:
                return _failed(call_id, "arguments_unusable", reason_code="resume_request_changed",
                               implementation_reached=False)
            corrected_failure = None
            if request.corrective_action:
                # A correction answers recorded failure evidence. An interruption
                # or a cancellation recorded none, and a commit-stage candidate
                # has already passed the session it would reopen.
                failure = previous.result.recovery_failure() or {}
                if (previous.result.state is not CapabilityResultState.FAILED
                        or failure.get("code") == "coding_cancelled"):
                    return _failed(call_id, "arguments_unusable",
                                   reason_code="corrective_action_without_failure",
                                   implementation_reached=False)
                if (checkpoint["stage"] not in {"execution", "review", "test"}
                        or checkpoint.get("commit_candidate_sha")):
                    return _failed(call_id, "arguments_unusable",
                                   reason_code="corrective_action_stage_unsupported",
                                   implementation_reached=False)
                corrected_failure = {
                    key: value for key, value in failure.items()
                    if isinstance(value, (str, int, bool)) or value is None
                }
            request = replace(original_request, repair_branch=checkpoint["branch"],
                              resume_checkpoint=checkpoint,
                              corrective_action=request.corrective_action,
                              corrected_failure=corrected_failure)
        elif arguments.get("continue_goal_branch", False):
            continuation = goal_coding_branch(
                goal_state_source(), request.repair_branch
            )
            if continuation is None:
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
        if outcome.status == "interrupted":
            return CapabilityResult(
                call_id, RUN_CODING_TASK, CapabilityResultState.PARTIAL,
                values, durable_values=outcome.durable_values(),
                provenance=RetentionPolicy().non_mail(
                    ContentOrigin.EXTERNAL, outcome.finished_at
                ),
            )
        if outcome.status not in {"succeeded", "no_change_required"}:
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

    def stop(arguments: Mapping[str, Any]) -> CapabilityResult:
        call_id = call_id_source()
        job_id = arguments.get("job_id") if isinstance(arguments, Mapping) else None
        if not isinstance(arguments, Mapping) or (
            job_id is not None
            and (not isinstance(job_id, str) or not job_id_permitted(job_id))
        ):
            return _failed(
                call_id, "arguments_unusable", capability=STOP_CODING_JOB,
                **_argument_failure("job_id", "unsafe", "job_id is invalid"),
            )
        stopped = bool(stop_job(job_id)) if stop_job is not None else False
        return CapabilityResult(
            call_id, STOP_CODING_JOB, CapabilityResultState.SUCCEEDED,
            {"stopped": stopped},
        )

    executors: dict[str, Callable[[Mapping[str, Any]], CapabilityResult]] = {
        RUN_CODING_TASK: run,
    }
    if stop_job is not None:
        executors[STOP_CODING_JOB] = stop
    return executors


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
    requested_checks, error = _optional_verification_commands(arguments)
    if error is not None:
        return None, error
    blocked, error = _optional_blocked_paths(arguments)
    if error is not None:
        return None, error
    corrective_action, error = _optional_string(
        arguments, "corrective_action", MAX_CONTEXT_CHARACTERS
    )
    if error is not None:
        return None, error
    firmware_repository = arguments.get("firmware_repository")
    if firmware_repository is not None and (
        not isinstance(firmware_repository, str) or not firmware_repository.strip()
    ):
        return None, _argument_failure("firmware_repository", "invalid", "invalid repository selector")
    review_pull_request = arguments.get("review_pull_request")
    if review_pull_request is not None and (
        not isinstance(review_pull_request, int)
        or isinstance(review_pull_request, bool)
        or review_pull_request <= 0
    ):
        return None, _argument_failure(
            "review_pull_request", "not_positive_integer",
            "review_pull_request must be a positive pull request number",
        )
    if "corrective_action" in arguments and not corrective_action.strip():
        return None, _argument_failure(
            "corrective_action", "blank", "corrective_action must be a non-blank string"
        )
    if corrective_action and "resume_job_id" not in arguments:
        return None, _argument_failure(
            "corrective_action",
            "requires_resume",
            "corrective_action answers a recorded failure and requires resume_job_id",
        )
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
            requested_checks=requested_checks,
            blocked_paths=blocked,
            repair_branch=branch,
            commit_message=message,
            corrective_action=corrective_action,
            review_pull_request=review_pull_request,
            firmware_repository=firmware_repository,
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


def _optional_verification_commands(
    arguments: Mapping[str, Any],
) -> tuple[tuple[tuple[str, ...], ...], dict[str, object] | None]:
    """AL/X's required verification commands, as bounded argv lists."""
    raw = arguments.get("verification_commands")
    if raw is None:
        return (), None
    if not isinstance(raw, (list, tuple)):
        return (), _argument_failure(
            "verification_commands", "not_command_array",
            "verification_commands must be an array of argv arrays",
        )
    if len(raw) > MAX_REQUESTED_CHECKS:
        return (), _argument_failure(
            "verification_commands", "too_many",
            f"verification_commands must have at most {MAX_REQUESTED_CHECKS} commands",
            received_count=len(raw),
        )
    commands: list[tuple[str, ...]] = []
    for argv in raw:
        if (
            not isinstance(argv, (list, tuple)) or not argv
            or len(argv) > MAX_REQUESTED_CHECK_ARGUMENTS
            or any(
                not isinstance(item, str) or not item.strip()
                or len(item) > MAX_REQUESTED_CHECK_ARGUMENT_CHARACTERS
                for item in argv
            )
        ):
            return (), _argument_failure(
                "verification_commands", "argv_invalid",
                "each verification command must be a non-empty argv array of "
                "bounded non-blank strings",
            )
        commands.append(tuple(argv))
    return tuple(commands), None


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
