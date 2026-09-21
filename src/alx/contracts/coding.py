"""Bounds and evidence records for one Core-delegated coding job, under D-028.

The Coding Agent is an execution capability, not a second AL/X. Core decides
that a bounded software-engineering task should be delegated; this module
describes the structured job, the mechanical limits around it, and the
evidence that returns. Nothing here interprets Friedl, chooses the next
product step, merges, pushes, deploys, or requests a review.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from pathlib import PurePosixPath
from typing import Protocol

from alx.contracts.coding_verification import (
    VerificationCheck,
    VerificationEvidence,
    VerificationPolicy,
    required_verification,
)


DEFAULT_STEP_BUDGET = 16
MAX_STEP_BUDGET = 32
MAX_PLANNING_ATTEMPTS = 3
# The native agent runs its own tool loop, so AL/X no longer counts model
# turns. What remains bounded is the verification AL/X performs afterwards.
MAX_VERIFICATION_COMMANDS = 8
DEFAULT_COMMAND_SECONDS = 60
# Verification keeps its own bound rather than inheriting the short default
# meant for git inspection.
#
# This comment used to say the full suite takes about 90 seconds. Measured on
# 2026-09-21 it takes about 190, so the claim was not merely stale but
# inverted: the bound below cannot complete the suite at all. That is not
# fixed by raising the number. Most jobs have no business running the whole
# suite, and `coding_verification` now decides from the job's changed files
# which checks it actually owes. The full suite remains the escalation for a
# Python change with no safe targeted mapping, and whether its bound should
# rise for that case is a separate question from this one.
DEFAULT_VERIFICATION_COMMAND_SECONDS = 180
# The full suite gets its own bound, because it is the one check the shared
# bound cannot accommodate: at ~190 seconds measured it exceeds 180, so every
# unmapped Python change selected a check that was guaranteed to time out and
# could never be committed. Raised in review on PR #54.
#
# This is not the documentation-job fix. That was not a timeout problem and was
# not solved with one: the policy simply no longer selects the suite for a
# change that has no reason to run it. This makes the deliberately narrow
# fallback that remains actually able to finish, with real headroom over the
# measurement rather than a margin that erodes as the suite grows.
FULL_SUITE_COMMAND_SECONDS = 600
MAX_COMMAND_SECONDS = 600
MAX_FILE_CHARACTERS = 256_000
MAX_COMMAND_OUTPUT_CHARACTERS = 16_000
MAX_DIFF_CHARACTERS = 32_000
MAX_REPORTED_FILES = 50
MAX_REPORTED_COMMANDS = 32
MAX_TASK_CHARACTERS = 16_000
MAX_CONTEXT_CHARACTERS = 16_000
MAX_CRITERIA = 16
MAX_CRITERION_CHARACTERS = 1_000
MAX_BLOCKED_PATHS = 32
MAX_BLOCKED_PATH_CHARACTERS = 512
MAX_LOCAL_REVIEW_CYCLES = 2
MAX_LOCAL_REVIEW_CONTEXT_CHARACTERS = 16_000
MAX_BRANCH_NAME_CHARACTERS = 200
MAX_COMMIT_MESSAGE_CHARACTERS = 4_000
# One commit per job. Staging is a named path list, and a job that touched
# more files than this has outgrown the bounded repair the capability is for.
MAX_STAGED_FILES = MAX_REPORTED_FILES
# How many NUL-delimited entries a structural git listing may carry before the
# reader refuses it. Authorisation is decided from those listings, so they are
# read whole rather than clipped: an entry truncated away is an entry the check
# cannot refuse. This bounds memory without ever shortening the answer, because
# exceeding it fails closed. Well above any real worktree; the live incident
# that motivated the git capability involved roughly 2,000 dirty paths.
MAX_INSPECTED_ENTRIES = 10_000
CODING_STALL_SECONDS = 120


# Broker-accepted CapabilityResult failure codes. Review outcomes must be
# listed here or the broker rewrites them to result_failure_invalid.
CODING_FAILURES = (
    "arguments_unusable",
    "coding_unavailable",
    "worktree_unusable",
    "path_outside_worktree",
    "path_not_permitted",
    "file_too_large",
    "command_not_permitted",
    "execution_timeout",
    "provider_failed",
    "plan_unusable",
    "planning_failed",
    "review_failed",
    "local_review_material_findings",
    "git_refused",
    "git_unavailable",
    "unrelated_changes_staged",
    # A check the job's own changed files required did not pass, or could
    # not be run. Distinct from a test failure: the failing check may be a
    # law gate or the diff check, and no test need have been required.
    "required_verification_failed",
    "sandbox_unusable",
    "session_failed",
    "task_failed",
    # D-031. A workspace release refuses when the job it names did not finish
    # successfully, which is a different fact from the workspace being
    # unusable: the directory is fine, the job is not.
    "job_not_successful",
)


class CodingError(Exception):
    """A coding job could not be performed, with a declared machine-readable code."""

    def __init__(self, code: str, **details: object) -> None:
        if code not in CODING_FAILURES:
            raise ValueError("coding failures must be declared")
        self.code = code
        self.details = {
            key: value
            for key, value in details.items()
            if value is not None
        }
        super().__init__(code)


@dataclass(frozen=True, slots=True)
class CodingTelemetry:
    """One authoritative, transient observation of a bounded coding job.

    This is runtime telemetry, not durable goal state and not job evidence.
    The Coding Agent emits it at real lifecycle boundaries; the diagnostic
    transport only presents it.
    """

    job_id: str
    phase: str
    started_at: datetime
    phase_started_at: datetime
    last_activity_at: datetime
    provider: str = ""
    model: str = ""
    attempt: int = 1
    correction_cycle: int = 0
    in_flight: bool = False
    waiting: bool = False
    terminal: bool = False
    outcome: str = ""
    transition: str = ""

    def __post_init__(self) -> None:
        _required(self.job_id, "job_id")
        _required(self.phase, "phase")
        for name in ("started_at", "phase_started_at", "last_activity_at"):
            _aware(getattr(self, name), name)
        if self.attempt < 1 or self.correction_cycle < 0:
            raise ValueError("telemetry attempt and correction cycle are bounded")


# A job identity reaching the filesystem becomes one path segment and part of
# a branch name, so it is held to a narrower grammar than the broker's call ids
# happen to use. No separator, no dot segment, no leading dash: a `..` or an
# absolute-looking identity cannot climb out of the worktree root, and a
# dash-led one cannot be read as an option by a git command it reaches.
#
# Stated here rather than in the allocator because both the capability boundary
# that receives the broker's call id and the allocator that turns it into a
# directory have to agree about it, and a contract is the one place both may
# depend on.
_JOB_ID_ALLOWED = frozenset(
    "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789-_"
)
MAX_JOB_ID_CHARACTERS = 128


def job_id_permitted(job_id: str) -> bool:
    """Whether a job identity may become a path segment and a branch element."""
    if not isinstance(job_id, str):
        return False
    candidate = job_id.strip()
    if not candidate or candidate != job_id:
        return False
    if len(candidate) > MAX_JOB_ID_CHARACTERS:
        return False
    if any(character not in _JOB_ID_ALLOWED for character in candidate):
        return False
    if candidate.startswith("-"):
        return False
    return True


def lexical_worktree_path(relative: str) -> str:
    """Collapse . and .. without leaving the worktree. Absolute paths refuse.

    Purely lexical, so it is safe before a path exists and shared by the
    workspace bound and the sandbox-profile generator.
    """
    if not isinstance(relative, str) or not relative.strip():
        raise CodingError("path_outside_worktree")
    if "\x00" in relative:
        raise CodingError("path_outside_worktree")
    path = PurePosixPath(relative.replace("\\", "/"))
    if path.is_absolute():
        raise CodingError("path_outside_worktree")
    parts: list[str] = []
    for part in path.parts:
        if part in ("", "."):
            continue
        if part == "..":
            if not parts:
                raise CodingError("path_outside_worktree")
            parts.pop()
            continue
        parts.append(part)
    return "/".join(parts)


def path_matches_blocked(relative: str, blocked: tuple[str, ...]) -> bool:
    """True if a worktree-relative path is a blocked path or a descendant."""
    folded = relative.casefold()
    if not folded:
        return any(not spec for spec in blocked)
    for spec in blocked:
        target = spec.casefold()
        if not target:
            return True
        if folded == target or folded.startswith(target + "/"):
            return True
    return False


def _required(value: str, name: str) -> None:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{name} must not be blank")


def _aware(value: datetime, name: str) -> None:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{name} must be timezone-aware")


@dataclass(frozen=True, slots=True)
class CodingRequest:
    """One bounded coding job Core has decided to delegate.

    `job_id` is not a model-supplied field. Under D-031 the executor injects
    the broker's durable capability call ID, and the worktree that identity
    allocates is where the job runs. Core names no filesystem path at all:
    the field it used to supply resolved against the runtime's own working
    directory, so `"."` was the live AL/X checkout and the kernel sandbox
    faithfully made the whole repository writable.

    `worktree` remains on this record because the session has to be told where
    to work, but it is no longer an input: it is written here by the Coding
    Agent after the allocator created it, so the only value it can hold is one
    AL/X generated. It is absent from the capability schema entirely, and an
    argument spelled `worktree` is refused rather than ignored.
    """

    task: str
    job_id: str
    worktree: str = ""
    acceptance_criteria: tuple[str, ...] = ()
    context: str = ""
    test_guidance: str = ""
    step_budget: int = DEFAULT_STEP_BUDGET
    blocked_paths: tuple[str, ...] = ()
    # Core decides whether a job's result should be handed back as a branch and
    # a commit at all, and what to call it. Left unset, the job behaves as it
    # did before: it edits the worktree and returns evidence, and AL/X sees the
    # change as a diff rather than as a commit. Naming the branch is a judgment
    # about what this repair is, so it does not belong in deterministic code.
    repair_branch: str = ""
    commit_message: str = ""

    def __post_init__(self) -> None:
        _required(self.task, "task")
        _required(self.job_id, "job_id")
        if len(self.task) > MAX_TASK_CHARACTERS:
            raise ValueError("task exceeds the permitted size")
        if len(self.context) > MAX_CONTEXT_CHARACTERS:
            raise ValueError("context exceeds the permitted size")
        if len(self.test_guidance) > MAX_CONTEXT_CHARACTERS:
            raise ValueError("test_guidance exceeds the permitted size")
        criteria = tuple(self.acceptance_criteria)
        object.__setattr__(self, "acceptance_criteria", criteria)
        if len(criteria) > MAX_CRITERIA:
            raise ValueError("too many acceptance criteria")
        if any(
            not isinstance(item, str)
            or not item.strip()
            or len(item) > MAX_CRITERION_CHARACTERS
            for item in criteria
        ):
            raise ValueError("acceptance criteria must be non-blank bounded strings")
        if not isinstance(self.step_budget, int) or isinstance(self.step_budget, bool):
            raise TypeError("step_budget must be an integer")
        if not 1 <= self.step_budget <= MAX_STEP_BUDGET:
            raise ValueError("step_budget must be within the permitted bound")
        blocked = tuple(self.blocked_paths)
        object.__setattr__(self, "blocked_paths", blocked)
        if len(blocked) > MAX_BLOCKED_PATHS:
            raise ValueError("too many blocked paths")
        if any(
            not isinstance(item, str)
            or not item.strip()
            or len(item) > MAX_BLOCKED_PATH_CHARACTERS
            for item in blocked
        ):
            raise ValueError("blocked paths must be non-blank bounded strings")
        if len(self.repair_branch) > MAX_BRANCH_NAME_CHARACTERS:
            raise ValueError("repair_branch exceeds the permitted size")
        if len(self.commit_message) > MAX_COMMIT_MESSAGE_CHARACTERS:
            raise ValueError("commit_message exceeds the permitted size")
        if self.commit_message.strip() and not self.repair_branch.strip():
            raise ValueError("a commit_message requires a repair_branch")


@dataclass(frozen=True, slots=True)
class CodingSessionResult:
    """What one native coding-agent session reports about itself.

    This is the agent's own account, not evidence. It says what the agent
    believes it did; AL/X verifies the repository and the tests separately and
    Core decides what the two together mean.
    """

    completed: bool
    report: str
    turns: int = 0
    failure_code: str = ""
    diagnostics: dict[str, object] | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "report", str(self.report))
        object.__setattr__(self, "diagnostics", dict(self.diagnostics or {}))
        if not isinstance(self.turns, int) or isinstance(self.turns, bool):
            raise TypeError("turns must be an integer")
        if self.turns < 0:
            raise ValueError("turns must not be negative")


class CodingSession(Protocol):
    """Run one native coding-agent session inside an assigned worktree.

    The implementation launches a real agent with its own tool loop. It never
    receives raw user language as a routing decision and never decides whether
    the coding job mattered; it returns what happened.
    """

    def run_session(
        self, request: "CodingRequest", briefing: str
    ) -> CodingSessionResult: ...


@dataclass(frozen=True, slots=True)
class GitWorkspaceState:
    """What the assigned worktree's git state is at one moment.

    Read before the session so a job can prove its baseline, and read again
    after so the difference is a measured fact rather than the session's word.
    `inherited_dirty` is the tree's dirt at the baseline: files somebody else
    left modified, which this job did not write and must never stage.
    """

    branch: str
    head_sha: str
    inherited_dirty: tuple[str, ...] = ()
    clean: bool = True
    detached: bool = False

    def __post_init__(self) -> None:
        object.__setattr__(self, "branch", str(self.branch))
        object.__setattr__(self, "head_sha", str(self.head_sha))
        object.__setattr__(self, "inherited_dirty", tuple(self.inherited_dirty))

    def as_values(self) -> dict[str, object]:
        return {
            "branch": self.branch,
            "head_sha": self.head_sha,
            "inherited_dirty": list(self.inherited_dirty),
            "clean": self.clean,
            "detached": self.detached,
        }


@dataclass(frozen=True, slots=True)
class CodingCommit:
    """One commit this job created in its assigned worktree.

    The branch and SHA are read back out of git after the commit rather than
    predicted from it, so what Core receives is what the repository holds.
    """

    branch: str
    commit_sha: str
    committed_files: tuple[str, ...]
    worktree_clean: bool

    def __post_init__(self) -> None:
        _required(self.branch, "branch")
        _required(self.commit_sha, "commit_sha")
        object.__setattr__(self, "committed_files", tuple(self.committed_files))

    def as_values(self) -> dict[str, object]:
        return {
            "branch": self.branch,
            "commit_sha": self.commit_sha,
            "committed_files": list(self.committed_files),
            "worktree_clean": self.worktree_clean,
        }


@dataclass(frozen=True, slots=True)
class LocalReviewFinding:
    """One advisory finding from the job's local reviewer.

    The reviewer is advisory: it reads the job's diff and says what it thinks
    is wrong. It cannot edit, run a command, commit or merge. A finding is
    therefore evidence for AL/X to weigh, never a verdict.

    These used to exist only as a local variable inside the review loop. They
    were computed, used to brief the coding session for a correction, and then
    discarded — so when findings survived the bounded cycle, AL/X was told the
    job had failed review and never told what the reviewer had said. The
    severity filter was a lossy read of a value nothing retained.
    """

    severity: str
    title: str
    evidence: str
    correction: str

    def __post_init__(self) -> None:
        _required(self.severity, "severity")
        _required(self.title, "title")
        if self.severity not in ("low", "medium", "high"):
            raise ValueError("severity must be low, medium or high")

    @property
    def material(self) -> bool:
        """Whether this finding is one the reviewer considered substantive.

        Materiality here is the reviewer's own severity label, not a judgement
        this code makes about whether the finding matters to the job. Under
        Law 3 that judgement is AL/X's, which is why a material finding is
        reported to her rather than resolved here.
        """
        return self.severity in _MATERIAL_REVIEW_SEVERITIES

    def as_values(self) -> dict[str, object]:
        return {
            "severity": self.severity,
            "title": self.title,
            "evidence": self.evidence,
            "correction": self.correction,
        }


_MATERIAL_REVIEW_SEVERITIES = frozenset({"medium", "high"})


@dataclass(frozen=True, slots=True)
class CodingCommandRecord:
    """One development command the job actually ran."""

    argv: tuple[str, ...]
    exit_status: int
    stdout: str
    stderr: str
    timed_out: bool
    permitted: bool

    def as_values(self) -> dict[str, object]:
        return {
            "argv": list(self.argv),
            "exit_status": self.exit_status,
            "stdout": self.stdout,
            "stderr": self.stderr,
            "timed_out": self.timed_out,
            "permitted": self.permitted,
        }

    def durable_values(self) -> dict[str, object]:
        return {
            "argv": list(self.argv),
            "exit_status": self.exit_status,
            "timed_out": self.timed_out,
            "permitted": self.permitted,
            "stdout_characters": len(self.stdout),
            "stderr_characters": len(self.stderr),
        }


@dataclass(frozen=True, slots=True)
class CodingOutcome:
    """Evidence about one coding job. Core interprets what it means."""

    status: str
    summary: str
    files_changed: tuple[str, ...]
    commands: tuple[CodingCommandRecord, ...]
    tests_run: bool
    tests_passed: bool | None
    git_status: str
    git_diff: str
    unresolved_issues: tuple[str, ...]
    external_review_recommended: bool
    finished_at: datetime
    diff_digest: str = ""
    preexisting_dirty: tuple[str, ...] = ()
    diagnostics: dict[str, object] | None = None
    plan_summary: str = ""
    baseline: "GitWorkspaceState | None" = None
    commit: "CodingCommit | None" = None
    # D-031 audit evidence: where this job ran, and whether that directory is
    # still there afterwards. Reported for every job regardless of outcome, so
    # retained stale state is visible rather than merely present.
    job_id: str = ""
    worktree: str = ""
    worktree_retained: bool = True
    # What this job was required to verify, what ran, and how each check ended.
    # `tests_run` and `tests_passed` above remain the test-specific facts they
    # always were; this is the whole picture, and it is what decides whether the
    # job may be committed. Absent on the failure paths that end before
    # verification is reached, where nothing was required and nothing ran.
    verification: "VerificationEvidence | None" = None
    # What the local reviewer said about this job's final candidate, whatever
    # the outcome. Findings that survived the bounded correction cycle travel
    # here with the commit rather than replacing it: AL/X reads the SHA, the
    # diff and the reviewer's own words together, and decides whether to
    # revise, push, or stop. Low-severity findings on an otherwise clean
    # review ride along too, because "the reviewer had nothing material to say"
    # and "the reviewer said nothing" are different facts.
    review_findings: tuple["LocalReviewFinding", ...] = ()

    def __post_init__(self) -> None:
        if self.status not in ("succeeded", "failed", "blocked"):
            raise ValueError("status must be succeeded, failed, or blocked")
        _required(self.summary, "summary")
        _aware(self.finished_at, "finished_at")
        object.__setattr__(self, "files_changed", tuple(self.files_changed))
        object.__setattr__(self, "preexisting_dirty", tuple(self.preexisting_dirty))
        object.__setattr__(self, "commands", tuple(self.commands))
        object.__setattr__(self, "unresolved_issues", tuple(self.unresolved_issues))
        object.__setattr__(self, "diagnostics", dict(self.diagnostics or {}))
        object.__setattr__(self, "plan_summary", str(self.plan_summary).strip())
        object.__setattr__(self, "review_findings", tuple(self.review_findings))
        if self.tests_passed is not None and not self.tests_run:
            raise ValueError("tests cannot have passed or failed if none ran")

    def as_values(self) -> dict[str, object]:
        return {
            **self.durable_values(),
            "summary": self.summary,
            "git_status": self.git_status,
            "git_diff": self.git_diff,
            "unresolved_issues": list(self.unresolved_issues),
            "commands": [item.as_values() for item in self.commands],
        }

    def durable_values(self) -> dict[str, object]:
        """What survives in goal state: no file contents or command output."""
        values: dict[str, object] = {
            "status": self.status,
            "files_changed": list(self.files_changed),
            "preexisting_dirty": list(self.preexisting_dirty),
            "plan_summary": self.plan_summary,
            "file_count": len(self.files_changed),
            "command_count": len(self.commands),
            "tests_run": self.tests_run,
            "external_review_recommended": self.external_review_recommended,
            "unresolved_count": len(self.unresolved_issues),
            "diff_digest": self.diff_digest,
            "finished_at": self.finished_at.isoformat(),
            "plan_summary": self.plan_summary,
            "commands": [item.durable_values() for item in self.commands],
            "job_id": self.job_id,
            "worktree": self.worktree,
            "worktree_retained": self.worktree_retained,
        }
        if self.tests_passed is not None:
            values["tests_passed"] = self.tests_passed
        if self.review_findings:
            # Durable, because "what did the reviewer actually say" is the
            # question AL/X asks when she sees external_review_recommended,
            # and a bare failure code never answered it.
            values["review_findings"] = [
                item.as_values() for item in self.review_findings
            ]
            values["material_review_findings"] = len(
                [item for item in self.review_findings if item.material]
            )
        if self.verification is not None:
            # Durable, because "which checks did this job have to pass"
            # is the question a later reader of a commit will ask, and the
            # answer is not reconstructable from the command list alone.
            values["verification"] = self.verification.as_values()
            values["all_required_verification_passed"] = (
                self.verification.all_required_passed
            )
        if self.baseline is not None:
            values["baseline"] = self.baseline.as_values()
        if self.commit is not None:
            values["commit"] = self.commit.as_values()
            # Promoted to the top level because these two are what Core hands
            # on when it asks for the repair: a branch and the SHA on it.
            values["branch"] = self.commit.branch
            values["commit_sha"] = self.commit.commit_sha
        return values


__all__ = [
    "CODING_FAILURES",
    "CodingCommandRecord",
    "CodingCommit",
    "CodingError",
    "CodingOutcome",
    "CodingRequest",
    "CodingSession",
    "CodingSessionResult",
    "GitWorkspaceState",
    "LocalReviewFinding",
    "VerificationCheck",
    "VerificationEvidence",
    "VerificationPolicy",
    "required_verification",
    "DEFAULT_COMMAND_SECONDS",
    "DEFAULT_VERIFICATION_COMMAND_SECONDS",
    "FULL_SUITE_COMMAND_SECONDS",
    "DEFAULT_STEP_BUDGET",
    "MAX_COMMAND_OUTPUT_CHARACTERS",
    "MAX_COMMAND_SECONDS",
    "MAX_DIFF_CHARACTERS",
    "MAX_FILE_CHARACTERS",
    "MAX_LOCAL_REVIEW_CONTEXT_CHARACTERS",
    "MAX_LOCAL_REVIEW_CYCLES",
    "MAX_REPORTED_COMMANDS",
    "MAX_REPORTED_FILES",
    "MAX_STEP_BUDGET",
    "MAX_PLANNING_ATTEMPTS",
    "MAX_VERIFICATION_COMMANDS",
    "MAX_BRANCH_NAME_CHARACTERS",
    "MAX_COMMIT_MESSAGE_CHARACTERS",
    "MAX_INSPECTED_ENTRIES",
    "MAX_STAGED_FILES",
    "MAX_BLOCKED_PATHS",
    "MAX_BLOCKED_PATH_CHARACTERS",
    "MAX_JOB_ID_CHARACTERS",
    "job_id_permitted",
    "lexical_worktree_path",
    "path_matches_blocked",
]
