"""Records for reading GitHub's check results for one exact revision.

Requesting a review, merging, and reading checks are different acts. This is
only the third. Nothing here reruns a job, cancels one, or decides whether a
result permits a merge.

The read is bound to one pull request at one exact commit. A check is evidence
about the revision it ran on, and evidence about a revision the pull request
no longer points at is not evidence about the one under consideration. When
the live head differs, the result says so and carries that head, and nothing
else is read.

What GitHub reports is carried as GitHub reported it. There is no pass count
and no blocking flag: whether the results permit a merge is a judgement, and
it is not made here. The read's execution outcome says only whether the
checks have settled and how, by GitHub's own required-check vocabulary, so a
plan AL/X already decided knows whether to keep waiting; anything outside
that vocabulary is returned to her as ambiguous.
"""

from __future__ import annotations

import re
from dataclasses import dataclass


# \Z rather than $: $ also matches before a terminal newline, so a value with
# one appended passed validation elsewhere and reached GitHub as an
# authorisation nobody could act on. Same rule the review and merge records
# already use.
_FULL_SHA = re.compile(r"\A[0-9a-f]{40}\Z")


CHECK_READ_FAILURES = (
    "arguments_unusable",
    "not_found",
    "head_changed",
    "permission_denied",
    "rate_limited",
    "provider_failed",
    # A single Actions job log could not be read. The check list is still
    # returned; this code names that one job, and is declared because a read
    # that loses the log entirely reports it as the call's failure instead.
    "log_unavailable",
)


# Conclusions for which an Actions job has a finished log. This selects which
# artifact to fetch. It is not a verdict about the revision.
ACTIONS_APP_SLUG = "github-actions"
LOGGED_CONCLUSIONS = frozenset({
    "failure",
    "timed_out",
    "cancelled",
    "action_required",
})

# How much of a job log is returned. The rest is counted, not discarded
# silently: `characters_omitted` is that count. The kept part is the suffix,
# which is where a failed job says why it stopped.
LOG_TAIL_CHARACTERS = 8_000


class CheckReadError(Exception):
    """A check list could not be read, with a declared machine-readable code."""

    def __init__(self, code: str, *, actual_head: str | None = None) -> None:
        if code not in CHECK_READ_FAILURES:
            raise ValueError("check read failures must be declared")
        if code != "head_changed" and actual_head is not None:
            raise ValueError("only a changed head carries the actual head")
        if actual_head is not None and not isinstance(actual_head, str):
            raise TypeError("actual head must be text")
        self.code = code
        self.actual_head = actual_head
        super().__init__(code)


def valid_sha(value: str) -> bool:
    """Whether this is a full 40-character lowercase commit identifier.

    An abbreviation is refused. The point of naming the revision is that
    exactly one is read, and a prefix could match a commit whose checks are
    not the ones asked for.
    """
    return isinstance(value, str) and _FULL_SHA.match(value) is not None


@dataclass(frozen=True, slots=True)
class PullRequestChecksRequest:
    """One pull request revision whose check results AL/X wants to read."""

    pull_request_number: int
    head_sha: str

    def __post_init__(self) -> None:
        if not isinstance(self.pull_request_number, int) or isinstance(
            self.pull_request_number, bool
        ):
            raise TypeError("pull_request_number must be an integer")
        if self.pull_request_number <= 0:
            raise ValueError("pull_request_number must be positive")
        if not valid_sha(self.head_sha):
            raise ValueError("head_sha must be a full 40-character commit id")


@dataclass(frozen=True, slots=True)
class CommitStatus:
    """One legacy commit status, as GitHub listed it.

    No context is dropped and no two are collapsed. A rollup would be a
    judgement about which status counts.
    """

    context: object
    state: object
    description: object
    target_url: object

    def as_values(self) -> dict[str, object]:
        return {
            "context": self.context,
            "state": self.state,
            "description": self.description,
            "target_url": self.target_url,
        }


@dataclass(frozen=True, slots=True)
class CheckRun:
    """One check run, plus an Actions job log when this read fetched one.

    The log fields are present only for a failed Actions job. Their absence
    means this read did not ask for a log, not that the job passed.
    """

    name: object
    status: object
    conclusion: object
    started_at: object
    completed_at: object
    details_url: object
    app_slug: object
    app_name: object
    output_title: object
    output_summary: object
    steps: tuple[tuple[object, object], ...] | None = None
    log_tail: str | None = None
    characters_omitted: int | None = None
    log_failure: str | None = None

    def as_values(self) -> dict[str, object]:
        values: dict[str, object] = {
            "name": self.name,
            "status": self.status,
            "conclusion": self.conclusion,
            "started_at": self.started_at,
            "completed_at": self.completed_at,
            "details_url": self.details_url,
            "app": {"slug": self.app_slug, "name": self.app_name},
            "output": {"title": self.output_title, "summary": self.output_summary},
        }
        if self.steps is not None:
            values["steps"] = tuple(
                {"name": name, "conclusion": conclusion}
                for name, conclusion in self.steps
            )
        if self.log_tail is not None:
            values["log_tail"] = self.log_tail
        if self.characters_omitted is not None:
            values["characters_omitted"] = self.characters_omitted
        if self.log_failure is not None:
            values["log_failure"] = self.log_failure
        return values


@dataclass(frozen=True, slots=True)
class PullRequestChecks:
    """GitHub's check runs and commit statuses for one exact head."""

    pull_request_number: int
    head_sha: str
    check_runs: tuple[CheckRun, ...]
    commit_statuses: tuple[CommitStatus, ...]

    def __post_init__(self) -> None:
        if not isinstance(self.pull_request_number, int) or isinstance(
            self.pull_request_number, bool
        ):
            raise TypeError("pull_request_number must be an integer")
        if self.pull_request_number <= 0:
            raise ValueError("pull_request_number must be positive")
        if not valid_sha(self.head_sha):
            raise ValueError("head_sha must be a full 40-character commit id")
        object.__setattr__(self, "check_runs", tuple(self.check_runs))
        object.__setattr__(self, "commit_statuses", tuple(self.commit_statuses))

    def as_values(self) -> dict[str, object]:
        return {
            "pull_request_number": self.pull_request_number,
            "head_sha": self.head_sha,
            "check_runs": tuple(item.as_values() for item in self.check_runs),
            "commit_statuses": tuple(
                item.as_values() for item in self.commit_statuses
            ),
        }


__all__ = [
    "ACTIONS_APP_SLUG",
    "CHECK_READ_FAILURES",
    "LOG_TAIL_CHARACTERS",
    "LOGGED_CONCLUSIONS",
    "CheckReadError",
    "CheckRun",
    "CommitStatus",
    "PullRequestChecks",
    "PullRequestChecksRequest",
    "valid_sha",
]
