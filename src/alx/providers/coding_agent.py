"""Run one bounded coding job and return structured evidence.

The shape of this is deliberate, and it is the second attempt. The first asked
the coding model to emit one AL/X-specific JSON operation per provider call and
executed each one here. That protocol produced valid plans and then no
actionable operation at all: the model was being asked to hand-serialise a tool
loop it already implements, statelessly, one round trip per file read.

So the model now runs as what it is. It plans, then a native coding-agent
session works inside the assigned worktree with its own tools and its own
multi-turn context. Containment moved from a per-operation Python check to the
kernel, where a generated sandbox profile denies git metadata, credentials and
every blocked path for every process the agent starts.

What did not move is authority. The agent has no terminal in this iteration, so
it cannot commit, push, merge, deploy or request a review, and it cannot run a
test either. AL/X runs verification afterwards through the allowlisted command
executor, which is the only site in this package that starts a development
process. The agent's own report is treated as an account, never as evidence:
the repository diff and the verification results are what Core is given.

What verification *means* is decided by `alx.contracts.coding_verification`
from the job's final changed files, not by whether pytest happened to run.
"""

from __future__ import annotations

import hashlib
import logging
import re
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path
from collections.abc import Callable
from typing import Any, Mapping

from alx.contracts import ModelMessage, ModelRequest, ModelRole, ReasoningModel
from alx.contracts.coding import (
    MAX_PLANNING_ATTEMPTS,
    MAX_REPORTED_COMMANDS,
    MAX_REPORTED_FILES,
    MAX_STAGED_FILES,
    MAX_LOCAL_REVIEW_CONTEXT_CHARACTERS,
    MAX_LOCAL_REVIEW_CYCLES,
    MAX_VERIFICATION_COMMANDS,
    DEFAULT_VERIFICATION_COMMAND_SECONDS,
    FULL_SUITE_COMMAND_SECONDS,
    CodingCommandRecord,
    CodingError,
    CodingOutcome,
    CodingRequest,
    CodingCommit,
    CodingSession,
    CodingSessionResult,
    CodingTelemetry,
    GitWorkspaceState,
    LocalReviewFinding,
)
from alx.contracts.coding_verification import (
    VerificationCheck,
    VerificationEvidence,
    content_violations,
    required_verification,
)
from alx.providers.coding_process import (
    command_permitted,
    files_from_git_status,
    inspect_git,
    is_test_command,
    run_permitted_command,
)
from alx.providers.coding_worktree import (
    CodingWorktree,
    CodingWorktreeAllocator,
)
from alx.providers.coding_git import (
    commit_job_changes,
    deleted_paths,
    read_workspace_state,
)
from alx.providers.coding_workspace import CodingWorkspace
from alx.providers.errors import ProviderError
import json


LOGGER = logging.getLogger(__name__)

PLAN_INSTRUCTION = (
    "You are a bounded coding worker preparing an implementation plan for one "
    "assigned software-engineering job. You are not AL/X and have no product, "
    "merge, deploy, push, review or governance authority. Produce only the "
    "requested structured plan. You will carry the plan out yourself in an "
    "assigned worktree using ordinary file reading, searching and editing, so "
    "plan real code changes. You will not have a terminal: do not plan shell "
    "commands. Verification is not yours to choose: AL/X derives the required "
    "checks from the files the job actually changes and runs them herself. "
    "You are in PLAN mode."
)

PLAN_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "problem_understanding": {"type": "string"},
        "hypotheses": {"type": "array", "items": {"type": "string"}},
        "inspection_targets": {"type": "array", "items": {"type": "string"}},
        "intended_changes": {"type": "array", "items": {"type": "string"}},
        "verification": {"type": "array", "items": {"type": "string"}},
        "risks_constraints": {"type": "array", "items": {"type": "string"}},
        "more_context_required": {"type": "boolean"},
    },
    "required": [
        "problem_understanding", "hypotheses", "inspection_targets",
        "intended_changes", "verification", "risks_constraints",
        "more_context_required",
    ],
    "additionalProperties": False,
}

LOCAL_REVIEW_INSTRUCTION = (
    "You are an advisory local code reviewer for one bounded coding job. "
    "You cannot edit files, run commands, commit, push, merge, deploy, or "
    "request an external review. Inspect only the supplied task, diff, bounded "
    "file context, and test evidence. Return findings only when the candidate "
    "misses the stated cause, leaves an adjacent path violating the same "
    "invariant, or lacks meaningful regression coverage. Do not make style-only "
    "findings."
)

LOCAL_REVIEW_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "findings": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "severity": {"type": "string"},
                    "title": {"type": "string"},
                    "evidence": {"type": "string"},
                    "correction": {"type": "string"},
                },
                "required": ["severity", "title", "evidence", "correction"],
                "additionalProperties": False,
            },
        },
    },
    "required": ["findings"],
    "additionalProperties": False,
}

_LOCAL_REVIEW_DETAIL_KEYS = frozenset({
    "provider", "reason_code", "exit_status",
    "stdout_characters", "stderr_characters",
})
_LOCAL_REVIEW_PARSE_CATEGORIES = {
    "structured_output_missing": "empty_response",
    "structured_output_not_object": "malformed_structured_output",
    "response_event_invalid": "parser_failure",
    "response_invalid": "parser_failure",
    "review_schema_invalid": "schema_invalid",
}
_SAFE_DIAGNOSTIC_CODE = re.compile(r"[a-z0-9_]{1,96}")
_SAFE_DIAGNOSTIC_ID = re.compile(r"[A-Za-z0-9_.:-]{1,128}")


def _digest(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


# pytest's "no tests were collected". What it means depends entirely on what
# was asked for.
#
# For the full suite it is not a failure: a repository with no tests has nothing
# there to fail, and reading it as failure made a Python change in such a
# repository permanently uncommittable — the same shape as the defect this whole
# change removes, one class of verification standing in for verification itself.
#
# For a *targeted* run it is a failure. The whole basis for running targeted
# tests instead of the suite is that these specific files cover the change; if
# they collect nothing, that basis was false and nothing was verified. Accepting
# it there would let a job commit having executed no test at all. Found in
# review on PR #54.
_PYTEST_NOTHING_COLLECTED = 5


def _check_passed(record: CodingCommandRecord, check_name: str = "") -> bool:
    """Whether one verification command's result counts as passing."""
    if record.timed_out:
        return False
    if record.exit_status == 0:
        return True
    return (
        check_name == "pytest_full"
        and is_test_command(record.argv)
        and record.exit_status == _PYTEST_NOTHING_COLLECTED
    )


def _strings(value: object) -> tuple[str, ...]:
    if not isinstance(value, (list, tuple)):
        return ()
    return tuple(str(item) for item in value if str(item).strip())


def build_briefing(request: CodingRequest, plan: Mapping[str, Any]) -> str:
    """The human-language instruction handed to the native coding session.

    Written as prose because the agent is a coding agent, not a protocol
    endpoint. It states the task, the accepted plan and the boundaries it
    cannot cross, including the two that are enforced elsewhere regardless of
    what it reads here: it has no terminal, and denied paths are refused by the
    operating system rather than by its own restraint.
    """
    lines = [
        "You are completing one assigned software-engineering task inside a git",
        "worktree that has already been prepared for you. Work directly: read,",
        "search and edit files with your normal tools until the task is done.",
        "",
        "# Task",
        request.task.strip(),
    ]
    if request.context.strip():
        lines += ["", "# Context", request.context.strip()]
    criteria = [item for item in request.acceptance_criteria if item.strip()]
    if criteria:
        lines += ["", "# Acceptance criteria"]
        lines += [f"- {item.strip()}" for item in criteria]
    lines += ["", "# Your accepted plan", str(plan.get("problem_understanding", "")).strip()]
    for label, key in (
        ("Intended changes", "intended_changes"),
        ("Verification expected", "verification"),
        ("Risks and constraints", "risks_constraints"),
    ):
        items = _strings(plan.get(key))
        if items:
            lines += ["", f"## {label}"]
            lines += [f"- {item}" for item in items]
    if request.test_guidance.strip():
        lines += ["", "# Test guidance", request.test_guidance.strip()]
    if request.repair_branch.strip():
        # Stated so the agent knows its edits are already on the repair branch
        # and has no reason to try to arrange one. It cannot run git either way.
        lines += [
            "",
            "# Branch",
            f"This worktree is already on the branch {request.repair_branch.strip()},",
            "prepared for you. Do not attempt to change it.",
        ]
    if request.blocked_paths:
        lines += ["", "# Paths you must not read or write"]
        lines += [f"- {item}" for item in request.blocked_paths]
    lines += [
        "",
        "# Boundaries",
        "- You have no terminal in this task. You cannot run commands or tests.",
        "  AL/X runs the required checks after you finish and reads the results",
        "  herself. Which checks those are follows from the files you changed.",
        "- Do not commit, push, merge, deploy, or request a code review. AL/X",
        "  manages the branch and the commit herself once every required check",
        "  has passed.",
        "- Stay inside this worktree. Git metadata, environment files and",
        "  credential files are denied by the operating system, not by you.",
        "- Change only what this task requires.",
        "",
        "# Finishing",
        "When the work is done or you are genuinely blocked, stop and report",
        "plainly: what you changed and why, which files, anything you could not",
        "resolve, and what should be tested. Do not claim the task is complete",
        "if it is not.",
    ]
    return "\n".join(lines)


class _JobState:
    """Everything one `run` owns, so two overlapping runs own nothing jointly.

    Created per call and passed explicitly. A mutable attribute on the agent
    would be shared by every job the agent runs, and the agent is long-lived:
    one runtime builds one `CodingAgent` and dispatches every coding job
    through it.
    """

    __slots__ = ("job_id", "allocated", "telemetry", "activity")

    def __init__(self, job_id: str) -> None:
        self.job_id = job_id or "coding-job"
        self.allocated: CodingWorktree | None = None
        self.telemetry: CodingTelemetry | None = None
        self.activity: str | None = None


class CodingAgent:
    """One coding job: AL/X plans it, a native session does it, AL/X verifies it."""

    def __init__(
        self, model: ReasoningModel, session: CodingSession | None,
        reviewer: ReasoningModel, activity_sink: Callable[[str], None] | None = None,
        telemetry_sink: Callable[[CodingTelemetry], None] | None = None,
        clock: Callable[[], datetime] | None = None,
        allocator: CodingWorktreeAllocator | None = None,
    ) -> None:
        self._model = model
        self._session = session
        self._reviewer = reviewer
        self._activity_sink = activity_sink or (lambda _activity: None)
        self._telemetry_sink = telemetry_sink or (lambda _telemetry: None)
        self._clock = clock or (lambda: datetime.now(UTC))
        # D-031. Without an allocator there is no isolated worktree to run in,
        # and running somewhere else is the thing that decision exists to
        # prevent, so a job fails closed rather than falling back to a path.
        self._allocator = allocator
        # Per-run state lives in `_JobState`, created by `run` and threaded
        # explicitly from there. It used to live here, as three instance
        # attributes, which meant two overlapping jobs on one agent shared a
        # worktree pointer, a telemetry anchor and an activity cache: whichever
        # ran second overwrote the first, so the first could resolve the
        # second's worktree, report its elapsed time, and write its outcome.
        # Nothing job-authoritative belongs on the agent itself.

    def _report_telemetry(
        self, state: "_JobState", phase: str, *, in_flight: bool = False,
        waiting: bool = False, terminal: bool = False, outcome: str = "",
        transition: str = "", correction_cycle: int | None = None,
    ) -> None:
        """Publish a lifecycle fact; telemetry failure never changes the job."""
        now = self._clock()
        previous = state.telemetry
        started = previous.started_at if previous is not None else now
        phase_started = (
            previous.phase_started_at
            if previous is not None and previous.phase == phase
            else now
        )
        provider = str(getattr(self._session, "provider_name", "") or "")
        model = str(getattr(self._session, "model_name", "") or "")
        # This job's own identity, carried on its state. Telemetry used to read
        # a shared `job_id_source` callable — the runtime's in-flight call ID —
        # which a concurrent job moves, so two overlapping jobs both reported
        # under whichever had dispatched most recently. The request carries the
        # identity the broker assigned to *this* job, and that is what is used.
        telemetry = CodingTelemetry(
            job_id=state.job_id, phase=phase,
            started_at=started, phase_started_at=phase_started,
            last_activity_at=now, provider=provider, model=model,
            attempt=1,
            correction_cycle=(previous.correction_cycle if previous is not None else 0)
            if correction_cycle is None else correction_cycle,
            in_flight=in_flight, waiting=waiting, terminal=terminal,
            outcome=outcome, transition=transition,
        )
        try:
            self._telemetry_sink(telemetry)
        except Exception as error:  # noqa: BLE001 - diagnostic transport only
            LOGGER.warning("Coding telemetry sink failed (%s); the job is unaffected", type(error).__name__)
            # The transport missed this observation, but the Coding Agent did
            # not. Preserve its local lifecycle anchor so a later successful
            # publication reports the job's real elapsed time.
            state.telemetry = telemetry
            return
        state.telemetry = telemetry

    def _report_activity(self, state: "_JobState", activity: str) -> None:
        """Tell the runtime what this job is doing. Never affect the job.

        The sink is a telemetry transport supplied by the caller, and a
        transport can fail. It used to fail into the job: an exception here
        propagated out of `run`, where the `finally` clause reports the final
        state *after* a valid outcome has already been computed, so a broken
        status line destroyed a completed repair. The tool layer then reported
        `coding_unavailable`, telling Core the job failed while the worktree
        held the finished work.

        Reporting is not part of the outcome, so a failure to report is not a
        failure of the job. It is logged and swallowed.

        The cache is updated only once the sink has accepted the report. Doing
        it first meant a swallowed transient failure still recorded the
        activity as current, so the identical terminal report from `run`'s
        finalizer was suppressed as redundant and the runtime was left showing
        a worker state for a job that had finished.
        """
        if state.activity == activity:
            return
        try:
            self._activity_sink(activity)
        except Exception as error:  # noqa: BLE001 - telemetry must not fail a job
            # The exception type only. D-012 forbids logging a traceback here:
            # a sink is caller-supplied and its failure can carry private
            # runtime state.
            LOGGER.warning(
                "Coding activity sink failed (%s); the job is unaffected",
                type(error).__name__,
            )
            return
        state.activity = activity

    def run(self, request: CodingRequest) -> CodingOutcome:
        """Run one job and never leave runtime telemetry at a worker state."""
        outcome: CodingOutcome | None = None
        # One state object per call, so nothing this run touches is reachable
        # from another run on the same agent.
        state = _JobState(request.job_id)
        self._report_telemetry(
            state, "plan", in_flight=True, transition="CASE started"
        )
        try:
            outcome = self._run(request, state)
            return outcome
        finally:
            # D-031. How the job ended is recorded beside its worktree, from the
            # one place that runs for every ending: success, declared failure,
            # and the exception path a crash takes. An unrecorded outcome leaves
            # a workspace that refuses release, which is the safe direction.
            self._record_worktree_outcome(state, outcome)
            self._report_telemetry(
                state,
                "complete" if outcome is not None and outcome.status == "succeeded" else "failed",
                terminal=True,
                outcome=outcome.status if outcome is not None else "failed",
                transition="COMPLETE" if outcome is not None and outcome.status == "succeeded" else "FAILED",
            )
            self._report_activity(state, "reasoning")

    def _record_worktree_outcome(
        self, state: "_JobState", outcome: CodingOutcome | None
    ) -> None:
        """Note the job's ending on its allocation record. Never fail the job."""
        allocated = state.allocated
        if allocated is None or self._allocator is None:
            return
        status = outcome.status if outcome is not None else "failed"
        try:
            self._allocator.record_outcome(allocated.job_id, status)
        except Exception as error:  # noqa: BLE001 - bookkeeping, not the job
            LOGGER.warning(
                "Coding worktree outcome not recorded (%s); the job is unaffected",
                type(error).__name__,
            )

    def _run(self, request: CodingRequest, state: "_JobState") -> CodingOutcome:
        # D-031: branch and worktree are allocated together, before anything
        # else touches a filesystem, from the job's own identity. This replaces
        # both the Core-supplied path and the separate `create_repair_branch`
        # step: one command creates both, so they cannot disagree about which
        # collision suffix won.
        if self._allocator is None:
            raise CodingError(
                "worktree_unusable", reason_code="allocator_not_configured"
            )
        allocated = self._allocator.allocate(
            request.job_id, request.repair_branch.strip()
        )
        state.allocated = allocated
        # The branch git actually created is authoritative: D-029's scheme may
        # have suffixed the requested base name, and every later step must use
        # the name that exists rather than the one that was asked for. The
        # worktree is written here for the same reason — the session needs a
        # directory, and this is the only place one can come from.
        request = replace(
            request,
            repair_branch=allocated.branch,
            worktree=str(allocated.path),
        )
        workspace = CodingWorkspace(str(allocated.path), request.blocked_paths)
        commands: list[CodingCommandRecord] = []
        preexisting_status, _ = self._git_evidence(workspace)
        preexisting_dirty = files_from_git_status(preexisting_status)
        preexisting_fingerprints = self._file_fingerprints(
            workspace, preexisting_dirty
        )
        # The baseline is read before anything else touches the worktree, so a
        # job can prove which HEAD it started from and what dirt it inherited.
        # Git being unreadable is not fatal on its own: a job that was not
        # asked for a commit still works in a directory that is not a
        # repository, so the baseline is simply absent.
        baseline = self._read_baseline(workspace)
        # `preexisting_dirty` above comes from the bounded evidence path, which
        # clips status at 16,000 characters. It is fine for reporting. It is
        # not fine for deciding what a job may stage: a clipped inherited path
        # that later reappears in status reads as job-owned. The baseline
        # reader has the complete listing, so authorisation uses that and falls
        # back only when git could not answer at all.
        baseline_dirty = (
            baseline.inherited_dirty if baseline is not None else preexisting_dirty
        )
        # Which paths were already deleted before this job touched anything.
        # Read once, here, so a file missing at the baseline can never become
        # this job's deletion merely by still being missing afterwards.
        inherited_deleted = self._deletions(workspace)
        # No separate branch-creation step remains. `git worktree add -b`
        # above created the branch and checked it out in the same command, so
        # the session's edits already land on the job's own branch and there is
        # no window in which a branch exists without its worktree.

        plan, planning_failure = self._planning_phase(request, workspace)
        if plan is None:
            git_status, git_diff = self._git_evidence(workspace)
            issue = (
                "provider_failed"
                if planning_failure.get("failure_code") == "provider_failed"
                else "planning_failed"
            )
            return self._outcome(
                status="failed",
                summary="the coding model did not produce a usable plan",
                files=(), preexisting_dirty=preexisting_dirty, commands=commands,
                tests_run=False, tests_passed=None, git_status=git_status,
                git_diff=git_diff, issues=(issue,), review=False,
                failure_status=True, diagnostics=planning_failure,
                baseline=baseline,
                allocated=state.allocated,
            )
        plan_summary = str(plan["problem_understanding"])
        self._report_telemetry(state, "execution", transition="PLAN completed")

        if self._session is None:
            git_status, git_diff = self._git_evidence(workspace)
            return self._outcome(
                status="failed",
                summary="no coding session is configured to carry out the plan",
                files=(), preexisting_dirty=preexisting_dirty, commands=commands,
                tests_run=False, tests_passed=None, git_status=git_status,
                git_diff=git_diff, issues=("coding_unavailable",), review=False,
                failure_status=True, plan_summary=plan_summary,
                diagnostics={"phase": "execution", "reason_code": "no_session"},
                baseline=baseline,
                allocated=state.allocated,
            )

        self._report_activity(state, "coding")
        self._report_telemetry(state, "execution", in_flight=True, transition="EXECUTION started")
        try:
            session = self._session.run_session(
                request, build_briefing(request, plan)
            )
        except CodingError as error:
            git_status, git_diff = self._git_evidence(workspace)
            return self._outcome(
                status="failed",
                summary="the coding session could not be started or completed",
                files=self._files_changed(
                    (), git_status, preexisting_dirty,
                    self._modified_preexisting(
                        workspace, preexisting_fingerprints
                    ),
                ),
                preexisting_dirty=preexisting_dirty, commands=commands,
                tests_run=False, tests_passed=None, git_status=git_status,
                git_diff=git_diff, issues=(error.code,), review=False,
                failure_status=True, plan_summary=plan_summary,
                diagnostics={"phase": "execution", **error.details},
                baseline=baseline,
                allocated=state.allocated,
            )

        self._report_telemetry(state, "execution", transition="EXECUTION completed")

        post_session_status, _ = self._git_evidence(workspace)
        session_files = self._files_changed(
            (), post_session_status, preexisting_dirty,
            self._modified_preexisting(workspace, preexisting_fingerprints),
        )
        review_failure: str | None = None
        review_issues: tuple[str, ...] = ()
        reviewed_files = session_files
        review_diagnostics: dict[str, object] = {}
        review_findings: tuple[LocalReviewFinding, ...] = ()
        # There is no candidate to review when the native session reports a
        # failed execution. Preserve that failure for AL/X's normal outcome.
        if session.completed and session_files:
            (
                review_failure, review_issues, reviewed_files,
                review_diagnostics, review_findings,
            ) = self._local_review_loop(
                request, workspace, plan, session_files, preexisting_dirty,
                preexisting_fingerprints, state,
            )
        if review_failure is not None:
            git_status, git_diff = self._git_evidence(workspace, reviewed_files)
            return self._outcome(
                status="failed", summary=review_failure, files=self._files_changed(
                    (), git_status, preexisting_dirty,
                    self._modified_preexisting(
                        workspace, preexisting_fingerprints
                    ),
                ), preexisting_dirty=preexisting_dirty, commands=commands,
                tests_run=False, tests_passed=None, git_status=git_status,
                git_diff=git_diff, issues=tuple(review_issues),
                review=bool([f for f in review_findings if f.material]),
                review_findings=review_findings,
                failure_status=True, plan_summary=plan_summary,
                diagnostics=review_diagnostics or {"phase": "local_review"},
                baseline=baseline,
                allocated=state.allocated,
            )

        # Verification is AL/X's, not the session's. The agent has no terminal,
        # so every command below is chosen here and refused unless the
        # allowlist already permits it. The scope is `reviewed_files`, the job's
        # final file set: a reviewer correction can touch a file the initial
        # session never did, and that file must select tests like any other.
        self._report_activity(state, "reasoning")
        self._report_telemetry(state, "test", transition="TEST started")
        verification, tests_run, tests_passed = self._verify(
            request, workspace, reviewed_files, commands
        )

        self._report_telemetry(state, "verify", transition="TEST completed")

        git_status, git_diff = self._git_evidence(workspace, reviewed_files)
        files = self._files_changed(
            (), git_status, preexisting_dirty,
            self._modified_preexisting(workspace, preexisting_fingerprints),
        )
        issues = list(_strings(session.diagnostics.get("unresolved_issues")))
        issues.extend(review_issues)
        status = "succeeded"
        if not session.completed:
            status = "failed"
            if session.failure_code:
                issues.append(session.failure_code)
            else:
                issues.append("session_failed")
        elif not files:
            # A session that reports success while changing nothing has not
            # done the job. V1 treated an unchanged worktree the same way.
            status = "failed"
            issues.append("no_files_changed")
        elif not verification.all_required_passed:
            # A required check that failed, or that never ran because its
            # command was refused, is a failed job. This used to read
            # `tests_run and tests_passed is False`, which let a job whose only
            # candidate command was refused reach "succeeded" having verified
            # nothing at all.
            status = "failed"
            issues.append("required_verification_failed")

        # Only a job that actually succeeded is committed. A failed one leaves
        # its work in the worktree for AL/X to read as a diff: committing it
        # would turn evidence Core still has to judge into a branch and a SHA
        # that read as a finished repair.
        #
        # D-029 authorises a commit "after the job has passed its required
        # verification". `all_required_passed` is that sentence: every check the
        # repository's rules attach to a path this job actually changed ran and
        # exited zero. A check that was refused or never ran is not a check that
        # passed, so an unverified job still gets no commit — the difference
        # matters precisely because an unverified commit reads downstream
        # exactly like a verified one.
        #
        # What changed is the definition, not the strictness. It used to be
        # `tests_run and tests_passed`, which made pytest the meaning of
        # verification rather than one class of it: a documentation-only job
        # had no test to run, so it could never satisfy the predicate however
        # correct it was. Now a job is required to pass exactly the checks its
        # own changed files call for, and must pass all of them.
        commit: CodingCommit | None = None
        wanted_commit = status == "succeeded" and request.commit_message.strip()
        # `files` is clipped to MAX_REPORTED_FILES for reporting. Committing
        # from a clipped set would stage the first fifty and present the
        # result as a complete repair, so the untruncated count decides
        # whether a commit is possible at all. Found in review on 2026-09-12:
        # the bound inside `commit_job_changes` compared an already-truncated
        # tuple against the same number and so could never fire.
        complete_files = self._files_changed(
            (), git_status, preexisting_dirty,
            self._modified_preexisting(workspace, preexisting_fingerprints),
            limit=None,
        )
        # D-029 takes two input kinds and they are separated here, not there:
        # a path in the job's changed set that git now reports deleted is the
        # job's deletion, and everything else is a surviving file. Inherited
        # deletions are excluded, so a file somebody else removed before the
        # job started is neither kind.
        currently_deleted = self._deletions(workspace)
        job_deleted = tuple(
            name for name in complete_files
            if name in currently_deleted and name not in inherited_deleted
        )
        complete_files = tuple(
            name for name in complete_files if name not in job_deleted
        )
        if wanted_commit and (
            len(complete_files) + len(job_deleted)
        ) > MAX_STAGED_FILES:
            issues.append("too_many_files_to_commit")
            wanted_commit = False
        # The status derivation above has already failed a job whose required
        # verification did not pass, so `wanted_commit` is false by then and
        # this guard does not fire in that case. It is kept because it is the
        # commit site's own precondition rather than a restatement of the
        # status: D-029 authorises the commit, and the condition D-029 names is
        # checked where the commit is made. If a later change ever lets an
        # unverified job reach "succeeded", the commit still does not happen.
        if wanted_commit and not verification.all_required_passed:
            issues.append("unverified_not_committed")
        elif wanted_commit:
            try:
                commit = commit_job_changes(
                    workspace.root,
                    request.repair_branch.strip(),
                    request.commit_message.strip(),
                    complete_files,
                    baseline_dirty,
                    workspace.blocked_paths,
                    job_deleted,
                    inherited_deleted,
                )
            except CodingError as error:
                # A refused commit is a failed job, not a succeeded one with a
                # footnote. `unrelated_changes_staged` is the case this exists
                # for: rather than commit somebody else's work alongside the
                # repair, nothing is committed and Core is told why.
                status = "failed"
                issues.append(error.code)
                git_status, git_diff = self._git_evidence(workspace, reviewed_files)
            else:
                # Re-read after the commit: the files are now in history, so
                # the diff and status Core sees must describe what is left.
                git_status, git_diff = self._git_evidence(workspace, reviewed_files)

        summary = session.report.strip() or "the coding session returned no report"
        return self._outcome(
            status=status,
            summary=summary[:8_000],
            files=files,
            preexisting_dirty=preexisting_dirty,
            commands=commands,
            tests_run=tests_run,
            tests_passed=tests_passed,
            verification=verification,
            git_status=git_status,
            git_diff=git_diff,
            issues=tuple(issues),
            # D-028 declares `external_review_recommended` as returned
            # evidence; it was hardcoded False at every site and never once
            # populated. A material finding the correction cycle did not
            # answer is exactly the case it exists for.
            review=bool([f for f in review_findings if f.material]),
            review_findings=review_findings,
            plan_summary=plan_summary,
            baseline=baseline,
            commit=commit,
            diagnostics={
                "phase": "execution",
                "session_turns": session.turns,
                "session_completed": session.completed,
                **{
                    key: value
                    for key, value in session.diagnostics.items()
                    if key != "unresolved_issues"
                },
            },
            allocated=state.allocated,
        )

    def _local_review_loop(
        self, request: CodingRequest, workspace: CodingWorkspace,
        plan: Mapping[str, Any], initial_files: tuple[str, ...],
        preexisting_dirty: tuple[str, ...],
        preexisting_fingerprints: Mapping[str, str | None],
        state: "_JobState",
    ) -> tuple[
        str | None,
        tuple[str, ...],
        tuple[str, ...],
        dict[str, object],
        tuple[LocalReviewFinding, ...],
    ]:
        """Review a candidate once, then re-review one bounded correction.

        Returns a hard failure only when the reviewer itself could not produce
        a result, or when the correction session broke. Findings the reviewer
        raised and the correction did not resolve are *not* a failure: they are
        returned as advisory evidence and travel with the job's commit.

        That distinction is the point of this stage. The reviewer is advisory —
        it cannot edit, run a command or commit — so a finding it raises is an
        opinion for AL/X to weigh, not a verdict on the work. Blocking the
        commit on one destroyed the artifact she needed in order to weigh it:
        the candidate was left as an uncommitted diff in a retained worktree,
        which is the evidence-reconstruction problem D-029 exists to remove.
        Infrastructure failure stays hard, because then there is no opinion at
        all and nothing was actually reviewed.
        """
        reviewed_files = initial_files
        for cycle in range(MAX_LOCAL_REVIEW_CYCLES):
            # The reviewer judges this job's diff, not the worktree's. Same
            # scoping as the outcome evidence, so both see the same thing.
            git_status, git_diff = self._git_evidence(workspace, reviewed_files)
            changed_files = self._files_changed(
                (), git_status, preexisting_dirty,
                self._modified_preexisting(workspace, preexisting_fingerprints),
            )
            reviewed_files = tuple(dict.fromkeys((*reviewed_files, *changed_files)))
            inspection_targets = tuple(
                name
                for name in _strings(plan.get("inspection_targets"))
                if name not in preexisting_dirty or name in changed_files
            )
            files = tuple(dict.fromkeys((
                *reviewed_files, *inspection_targets,
            )))
            self._report_activity(state, "reviewing")
            self._report_telemetry(state, "review", in_flight=True, transition="REVIEW started", correction_cycle=cycle)
            try:
                findings = self._review(request, workspace, plan, files, git_diff)
            except CodingError as error:
                # Infrastructure failure: the reviewer produced no opinion at
                # all, so nothing was reviewed. This stays a hard failure.
                return (
                    "the local reviewer could not produce a usable result",
                    ("review_failed",), reviewed_files,
                    self._local_review_diagnostics(error), (),
                )
            material = [item for item in findings if item.material]
            self._report_telemetry(state, "review", transition="REVIEW completed", correction_cycle=cycle)
            if not material:
                # Nothing material. Any low-severity findings still travel with
                # the job: "nothing worth blocking on" and "nothing said" are
                # different facts, and only AL/X should collapse them.
                return None, (), reviewed_files, {}, findings
            if cycle + 1 == MAX_LOCAL_REVIEW_CYCLES:
                # Findings survived the bounded cycle. Advisory, not fatal:
                # the job continues to verification and its commit, and these
                # go to AL/X with the SHA so she can judge them against the
                # actual artifact.
                return (
                    None, ("local_review_material_findings",), reviewed_files,
                    {}, findings,
                )
            before = (git_status, git_diff)
            briefing = build_briefing(request, plan) + "\n\n# Local reviewer findings\n" + "\n".join(
                f"- [{item.severity}] {item.title}: {item.evidence} Correction: {item.correction}"
                for item in material
            )
            self._report_activity(state, "coding")
            self._report_telemetry(state, "correction", in_flight=True, transition="CORRECTION cycle", correction_cycle=cycle + 1)
            try:
                correction = self._session.run_session(request, briefing)
            except CodingError:
                # The session broke. That is infrastructure, not an opinion.
                return (
                    "the coding session could not correct local review findings",
                    ("session_failed",), reviewed_files, {}, findings,
                )
            if not correction.completed:
                return (
                    "the coding session could not correct local review findings",
                    (correction.failure_code or "session_failed",), reviewed_files,
                    {}, findings,
                )
            self._report_telemetry(state, "correction", transition="CORRECTION completed", correction_cycle=cycle + 1)
            # Scoped exactly as `before` was. Comparing a narrowed diff with a
            # whole-worktree one would never match, so a correction that
            # changed nothing would read as progress.
            post_correction_status, _ = self._git_evidence(workspace)
            corrected_files = self._files_changed(
                (), post_correction_status, preexisting_dirty,
                self._modified_preexisting(workspace, preexisting_fingerprints),
            )
            next_files = tuple(dict.fromkeys((*reviewed_files, *corrected_files)))
            after = self._git_evidence(workspace, next_files)
            if after == before:
                # The correction changed nothing, so the findings stand
                # unanswered. Same disposition as surviving them by exhausting
                # the cycle: advisory evidence, and the job carries on.
                return (
                    None, ("local_review_material_findings",), reviewed_files,
                    {}, findings,
                )
            reviewed_files = next_files
        raise AssertionError("local review loop must return within its bound")

    def _local_review_diagnostics(self, error: CodingError) -> dict[str, object]:
        """Project a reviewer failure into durable, non-sensitive evidence."""
        details = dict(error.details)
        values: dict[str, object] = {"phase": "local_review"}
        for key in _LOCAL_REVIEW_DETAIL_KEYS:
            value = details.get(key)
            if key in ("exit_status", "stdout_characters", "stderr_characters"):
                if isinstance(value, int) and value >= 0:
                    values[key] = value
            elif isinstance(value, str) and value.strip():
                candidate = value.strip()
                pattern = _SAFE_DIAGNOSTIC_CODE if key == "reason_code" else _SAFE_DIAGNOSTIC_ID
                if pattern.fullmatch(candidate):
                    values[key] = candidate
        provider = values.get("provider")
        model = getattr(self._reviewer, "_model", "")
        if (provider and isinstance(model, str) and _SAFE_DIAGNOSTIC_ID.fullmatch(model.strip())):
            values["model"] = model.strip()
        reason = values.get("reason_code")
        if not isinstance(reason, str) or not reason:
            reason = error.code
            values["reason_code"] = reason
        if reason == "reasoning_timeout":
            values["timed_out"] = True
        category = _LOCAL_REVIEW_PARSE_CATEGORIES.get(reason)
        if category is not None:
            values["parse_category"] = category
        return values

    def _review(
        self, request: CodingRequest, workspace: CodingWorkspace,
        plan: Mapping[str, Any], files: tuple[str, ...], git_diff: str,
    ) -> tuple[LocalReviewFinding, ...]:
        """Ask the configured coding model for bounded advisory findings only."""
        remaining = MAX_LOCAL_REVIEW_CONTEXT_CHARACTERS
        context: dict[str, str] = {}
        for name in files:
            if remaining <= 0:
                break
            try:
                workspace.validate_inspection_target(name)
                text = workspace.resolve(name).read_text(encoding="utf-8")
            except (CodingError, OSError, UnicodeDecodeError):
                continue
            context[name] = text[:remaining]
            remaining -= len(context[name])
        values = self._complete(LOCAL_REVIEW_INSTRUCTION, {
            "task": request.task, "root_cause_context": request.context,
            "acceptance_criteria": list(request.acceptance_criteria),
            "plan": dict(plan), "git_diff": git_diff,
            "changed_files": list(files), "changed_file_context": context,
            "test_guidance": request.test_guidance,
        }, "alx_coding_local_review", LOCAL_REVIEW_SCHEMA, model=self._reviewer)
        raw = values.get("findings")
        if not isinstance(raw, (list, tuple)):
            raise CodingError("review_failed", reason_code="review_schema_invalid")
        findings: list[LocalReviewFinding] = []
        for item in raw:
            if not isinstance(item, Mapping):
                raise CodingError("review_failed", reason_code="review_schema_invalid")
            finding = {key: str(item.get(key, "")).strip() for key in ("severity", "title", "evidence", "correction")}
            finding["severity"] = finding["severity"].lower()
            if not all(finding.values()):
                raise CodingError("review_failed", reason_code="review_schema_invalid")
            try:
                # The severity vocabulary is the contract's, checked where the
                # type is defined. Repeating the set here gave two places to
                # change and one to forget.
                findings.append(LocalReviewFinding(**finding))
            except ValueError as error:
                raise CodingError(
                    "review_failed", reason_code="review_schema_invalid"
                ) from error
        return tuple(findings)

    def _verify(
        self,
        request: CodingRequest,
        workspace: CodingWorkspace,
        changed_files: tuple[str, ...],
        commands: list[CodingCommandRecord],
    ) -> tuple[VerificationEvidence, bool, bool | None]:
        """Run every check this job's final file set requires, and record each.

        The policy is derived from `changed_files` — the set after the local
        reviewer's corrections landed — and from nothing else. Task wording and
        the model's own plan used to seed candidate commands here; they no
        longer do. A required check is one the repository's rules attach to a
        path that actually changed, which is a fact, whereas a command parsed
        out of prose is a suggestion from the thing being verified.

        Each command is still passed through `command_permitted`. The policy
        cannot widen the allowlist: a check whose command the allowlist refuses
        is recorded as required and not run, and a job with such a check has not
        passed its verification.

        `tests_run` and `tests_passed` are preserved alongside the new evidence
        because they are a real, separately meaningful fact about the job, and
        Core and the durable record already read them.
        """
        worktree = self._root(request)
        policy = required_verification(changed_files, worktree)
        blocked = tuple(request.blocked_paths)
        results: list[VerificationCheck] = []
        tests_run = False
        tests_passed: bool | None = None
        # The bound is a ceiling on work, never a reason to drop a requirement.
        # Truncating the list would leave the dropped checks out of the evidence
        # entirely, so a partly-verified job would read as fully passed. The
        # policy emits at most four checks against a ceiling of eight, so this
        # is unreachable today; it fails closed rather than depending on that
        # headroom surviving a future check class.
        checks = policy.checks
        if len(checks) > MAX_VERIFICATION_COMMANDS:
            return VerificationEvidence(checks), False, None
        for check in checks:
            if check.kind == "content":
                # Performed here rather than through the executor: reading the
                # job's own files is deterministic with one correct answer, so
                # under Law 2 it is code, and it needs no command allowlisted.
                # It is also the half `git diff --check` cannot see, because a
                # file the job created is still untracked at this point.
                findings = content_violations(changed_files, workspace.root)
                results.append(
                    replace(
                        check, ran=True, passed=not findings, findings=findings
                    )
                )
                continue
            argv = list(check.argv)
            if not command_permitted(argv, worktree, blocked):
                commands.append(
                    CodingCommandRecord(
                        check.argv, -1, "", "command_not_permitted", False, False
                    )
                )
                results.append(check)
                continue
            try:
                record = run_permitted_command(
                    argv, workspace.root,
                    # The full suite is the one check the shared bound cannot
                    # accommodate; everything else keeps it.
                    timeout_seconds=(
                        FULL_SUITE_COMMAND_SECONDS
                        if check.name == "pytest_full"
                        else DEFAULT_VERIFICATION_COMMAND_SECONDS
                    ),
                    blocked_paths=workspace.blocked_paths,
                )
            except CodingError as error:
                commands.append(
                    CodingCommandRecord(
                        check.argv, -1, "", error.code, False,
                        error.code != "command_not_permitted",
                    )
                )
                results.append(check)
                continue
            commands.append(record)
            passed = _check_passed(record, check.name)
            results.append(replace(check, ran=True, passed=passed))
            if is_test_command(record.argv):
                tests_run = True
                if not passed:
                    tests_passed = False
                elif tests_passed is None:
                    tests_passed = True
        return VerificationEvidence(tuple(results)), tests_run, tests_passed

    @staticmethod
    def _root(request: CodingRequest) -> Path:
        """The worktree allocated to this job.

        Read from the request, which `_run` rewrote with the allocated path
        before anything else saw it. The request is a frozen value local to one
        `run`, so two overlapping jobs cannot resolve to each other's worktree;
        this used to consult a mutable attribute on the agent, which they
        could.
        """
        if not request.worktree.strip():
            raise CodingError("worktree_unusable", reason_code="worktree_not_allocated")
        return Path(request.worktree).expanduser().resolve()

    def _planning_phase(
        self, request: CodingRequest, workspace: CodingWorkspace
    ) -> tuple[Mapping[str, Any] | None, dict[str, object]]:
        feedback: list[str] = []
        last: dict[str, object] = {}
        for attempt in range(1, MAX_PLANNING_ATTEMPTS + 1):
            try:
                return self._plan(request, workspace, feedback), {}
            except CodingError as error:
                last = {
                    "phase": "planning",
                    "failure_code": error.code,
                    "reason_code": error.details.get("reason_code", error.code),
                    "planning_attempts": attempt,
                    "structured_output_received": error.code != "provider_failed",
                    "parsing_succeeded": error.code != "provider_failed",
                    "validation_succeeded": False,
                    **error.details,
                }
                # A selected transport failure fails closed. Only a parsed but
                # invalid plan receives bounded corrective feedback.
                if error.code == "provider_failed":
                    return None, last
                feedback.append("plan_validation_error:" + str(last["reason_code"]))
        return None, last

    def _plan(
        self, request: CodingRequest, workspace: CodingWorkspace,
        feedback: list[str],
    ) -> Mapping[str, Any]:
        material = {
            "phase": "planning",
            "task": request.task,
            "worktree": str(workspace.root),
            # A bounded, blocked-path-filtered root listing grounds the plan in
            # the assigned repository without granting the planning turn a
            # shell or an unbounded file search.
            "worktree_entries": list(workspace.list_dir(".")),
            "acceptance_criteria": list(request.acceptance_criteria),
            "context": request.context,
            "test_guidance": request.test_guidance,
            "blocked_paths": list(request.blocked_paths),
            "execution_model": {
                "tools": "native file reading, searching and editing",
                "terminal": False,
                "tests_run_by": "alx_after_session",
            },
            "planning_feedback": feedback[-2:],
        }
        values = self._complete(
            PLAN_INSTRUCTION, material, "alx_coding_plan", PLAN_SCHEMA,
            model=self._model,
        )
        required_lists = (
            "hypotheses", "inspection_targets", "intended_changes",
            "verification", "risks_constraints",
        )
        if (not isinstance(values.get("problem_understanding"), str)
                or not values["problem_understanding"].strip()
                or not isinstance(values.get("more_context_required"), bool)
                or any(not isinstance(values.get(field), (list, tuple))
                       or any(not isinstance(item, str) or not item.strip()
                              for item in values[field])
                       for field in required_lists)):
            raise CodingError("plan_unusable", reason_code="plan_schema_invalid")
        try:
            for target in values["inspection_targets"]:
                workspace.validate_inspection_target(target)
        except CodingError as error:
            raise CodingError("plan_unusable", reason_code=error.code) from error
        return values

    def _complete(
        self, instruction: str, material: Mapping[str, Any], affinity: str,
        schema: Mapping[str, Any], *, model: ReasoningModel,
    ) -> Mapping[str, Any]:
        model_request = ModelRequest(
            (
                ModelMessage(ModelRole.SYSTEM, instruction),
                ModelMessage(ModelRole.USER, json.dumps(material, ensure_ascii=False)),
            ),
            affinity,
            schema,
            None,
            "alx-coding-v1",
            kind="coding",
        )
        details: dict[str, object] = {}
        try:
            completion = model.complete(model_request)
        except ProviderError as error:
            details = {
                "reason_code": error.reason,
                "provider": error.provider,
                **error.details,
            }
        except Exception:
            details = {"reason_code": "provider_failed"}
        else:
            values = completion.output
            if not isinstance(values, Mapping):
                raise CodingError("provider_failed", reason_code="output_not_object")
            return values
        raise CodingError("provider_failed", **details)

    @staticmethod
    def _deletions(workspace: CodingWorkspace) -> frozenset[str]:
        """Paths git reports deleted from the worktree at this moment.

        Read once before the session and once after: the difference is what
        this job deleted. Absent rather than fatal when git cannot answer, for
        the same reason the baseline is — a job that was not asked for a commit
        still works in a directory that is not a repository.
        """
        try:
            return deleted_paths(workspace.root)
        except CodingError:
            return frozenset()

    @staticmethod
    def _read_baseline(workspace: CodingWorkspace) -> GitWorkspaceState | None:
        """The worktree's branch, HEAD and inherited dirt before the job runs.

        Absent rather than fatal when git cannot answer: a job that was not
        asked for a branch or a commit still works in a directory that is not
        a repository, and refusing it here would withdraw a capability that
        already exists. A job that *was* asked for one fails at the branch
        step instead, where the refusal is the right answer.
        """
        try:
            return read_workspace_state(workspace.root)
        except CodingError:
            return None

    def _git_evidence(
        self, workspace: CodingWorkspace, paths: tuple[str, ...] = ()
    ) -> tuple[str, str]:
        """Status for the whole tree, diff for the files this job touched.

        The worktree is not the job's to own. On 2026-09-11 a 109k diff of
        somebody else's uncommitted work clipped at the 32k bound before
        reaching any file under repair, and four sessions were handed the same
        truncated prefix. Narrowing the diff spends the budget on this job;
        status still covers everything, because what else is dirty is a fact
        the job needs to know.
        """
        try:
            evidence = inspect_git(workspace.root, paths)
        except CodingError:
            return "", ""
        diff = evidence.diff
        if evidence.diff_truncated:
            # Never present clipped evidence as complete. The reader decides
            # what a partial diff is worth; it may not be left to infer it.
            diff = (
                f"[diff truncated: showing {len(diff)} of "
                f"{evidence.diff_characters} characters]\n{diff}"
            )
        return evidence.status, diff

    def _files_changed(
        self,
        written: tuple[str, ...],
        git_status: str,
        preexisting_dirty: tuple[str, ...],
        modified_preexisting: tuple[str, ...] = (),
        limit: int | None = MAX_REPORTED_FILES,
    ) -> tuple[str, ...]:
        """The job's own changed files. `limit=None` returns the whole set.

        Reporting is clipped; authorisation is not. A clipped set reaching the
        commit would stage its prefix and call the result a finished repair.
        """
        preexisting = set(preexisting_dirty)
        modified = set(modified_preexisting)
        names = list(written)
        for item in files_from_git_status(git_status):
            if item in preexisting and item not in modified:
                continue
            if item not in names:
                names.append(item)
        return tuple(names if limit is None else names[:limit])

    @staticmethod
    def _file_fingerprints(
        workspace: CodingWorkspace, paths: tuple[str, ...]
    ) -> dict[str, str | None]:
        """Streaming fingerprints distinguish job edits from inherited dirt."""
        fingerprints: dict[str, str | None] = {}
        for name in paths:
            try:
                target = workspace.resolve(name)
                digest = hashlib.sha256()
                with target.open("rb") as source:
                    for chunk in iter(lambda: source.read(64 * 1024), b""):
                        digest.update(chunk)
            except (CodingError, OSError):
                fingerprints[name] = None
            else:
                fingerprints[name] = digest.hexdigest()
        return fingerprints

    @classmethod
    def _modified_preexisting(
        cls, workspace: CodingWorkspace, before: Mapping[str, str | None]
    ) -> tuple[str, ...]:
        """Only inherited paths whose bytes changed become this job's evidence."""
        after = cls._file_fingerprints(workspace, tuple(before))
        return tuple(name for name, value in before.items() if after.get(name) != value)

    def _outcome(
        self,
        *,
        status: str,
        summary: str,
        files: tuple[str, ...],
        preexisting_dirty: tuple[str, ...] = (),
        commands: list[CodingCommandRecord],
        tests_run: bool,
        tests_passed: bool | None,
        git_status: str,
        git_diff: str,
        issues: tuple[str, ...],
        review: bool,
        review_findings: tuple[LocalReviewFinding, ...] = (),
        verification: VerificationEvidence | None = None,
        failure_status: bool = False,
        diagnostics: dict[str, object] | None = None,
        plan_summary: str = "",
        baseline: GitWorkspaceState | None = None,
        commit: CodingCommit | None = None,
        allocated: CodingWorktree | None = None,
    ) -> CodingOutcome:
        if status not in ("succeeded", "failed", "blocked"):
            status = "failed"
        if failure_status:
            status = "failed"
        return CodingOutcome(
            status,
            summary,
            files,
            tuple(commands[:MAX_REPORTED_COMMANDS]),
            tests_run,
            tests_passed,
            git_status,
            git_diff,
            issues,
            review,
            datetime.now(UTC),
            _digest(git_diff),
            preexisting_dirty,
            diagnostics,
            plan_summary,
            baseline,
            commit,
            # D-031. Reported for every job: the worktree is retained until an
            # explicit release, so `worktree_retained` is true whenever a job
            # ends. Release is a later, separate capability call. Passed in
            # from the run that owns it rather than read off the agent, which
            # a concurrent job would have moved.
            allocated.job_id if allocated is not None else "",
            str(allocated.path) if allocated is not None else "",
            True,
            verification,
            review_findings,
        )
