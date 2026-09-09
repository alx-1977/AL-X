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


DEFAULT_STEP_BUDGET = 16
MAX_STEP_BUDGET = 32
DEFAULT_COMMAND_SECONDS = 60
MAX_COMMAND_SECONDS = 120
MAX_FILE_CHARACTERS = 256_000
MAX_COMMAND_OUTPUT_CHARACTERS = 16_000
MAX_DIFF_CHARACTERS = 32_000
MAX_REPORTED_FILES = 50
MAX_REPORTED_COMMANDS = 32
MAX_TASK_CHARACTERS = 16_000
MAX_CONTEXT_CHARACTERS = 16_000
MAX_CRITERIA = 16
MAX_CRITERION_CHARACTERS = 1_000


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
    "step_budget_exhausted",
    "command_budget_exhausted",
    "task_failed",
)


class CodingError(Exception):
    """A coding job could not be performed, with a declared machine-readable code."""

    def __init__(self, code: str) -> None:
        if code not in CODING_FAILURES:
            raise ValueError("coding failures must be declared")
        self.code = code
        super().__init__(code)


def _required(value: str, name: str) -> None:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{name} must not be blank")


def _aware(value: datetime, name: str) -> None:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{name} must be timezone-aware")


@dataclass(frozen=True, slots=True)
class CodingRequest:
    """One bounded coding job Core has decided to delegate."""

    task: str
    worktree: str
    acceptance_criteria: tuple[str, ...] = ()
    context: str = ""
    test_guidance: str = ""
    step_budget: int = DEFAULT_STEP_BUDGET

    def __post_init__(self) -> None:
        _required(self.task, "task")
        _required(self.worktree, "worktree")
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

    def __post_init__(self) -> None:
        if self.status not in ("succeeded", "failed", "blocked"):
            raise ValueError("status must be succeeded, failed, or blocked")
        _required(self.summary, "summary")
        _aware(self.finished_at, "finished_at")
        object.__setattr__(self, "files_changed", tuple(self.files_changed))
        object.__setattr__(self, "commands", tuple(self.commands))
        object.__setattr__(self, "unresolved_issues", tuple(self.unresolved_issues))
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
            "file_count": len(self.files_changed),
            "command_count": len(self.commands),
            "tests_run": self.tests_run,
            "external_review_recommended": self.external_review_recommended,
            "unresolved_count": len(self.unresolved_issues),
            "diff_digest": self.diff_digest,
            "finished_at": self.finished_at.isoformat(),
            "commands": [item.durable_values() for item in self.commands],
        }
        if self.tests_passed is not None:
            values["tests_passed"] = self.tests_passed
        return values


__all__ = [
    "CODING_FAILURES",
    "CodingCommandRecord",
    "CodingError",
    "CodingOutcome",
    "CodingRequest",
    "DEFAULT_COMMAND_SECONDS",
    "DEFAULT_STEP_BUDGET",
    "MAX_COMMAND_OUTPUT_CHARACTERS",
    "MAX_COMMAND_SECONDS",
    "MAX_DIFF_CHARACTERS",
    "MAX_FILE_CHARACTERS",
    "MAX_REPORTED_COMMANDS",
    "MAX_REPORTED_FILES",
    "MAX_STEP_BUDGET",
]
