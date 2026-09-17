"""Records for putting a repair where a reviewer and CI can see it.

The Coding Agent commits inside the worktree it was given and stops there. That
is deliberate under D-028: push, fetch and merge are refused by construction, so
a job cannot reach the remote. But a commit nobody can see is not a repair —
law gates run on pull requests, and every external reviewer watches them, so
work that never leaves the worktree cannot be reviewed, cannot be checked, and
cannot reach the merge boundary.

These records name the two steps that close that gap, and nothing else.
Publishing is not committing, and opening a pull request is not merging: the one
revision a merge may act on is still established by `contracts/repository.py`,
which reads the reviewed head rather than trusting whatever the branch points at
now.

The authority is AL/X's, not the Coding Agent's. Widening `_WRITE_SHAPES` so a
job could push would give an implementation capability the power to publish its
own work, and a reviewer would then be looking at whatever the job decided to
send. The separation is the point.
"""

from __future__ import annotations

import re
from dataclasses import dataclass


# \Z rather than $: $ also matches before a terminal newline, so a value with
# one appended passes validation and reaches GitHub as something nobody can act
# on. The same mistake was already made once with a merge sha.
_FULL_SHA = re.compile(r"\A[0-9a-f]{40}\Z")

# A branch name this capability will carry. Deliberately narrower than what git
# permits: no slashes beyond one grouping level, no whitespace, no refspec
# punctuation, nothing that could be read as an option or a second ref.
_BRANCH = re.compile(r"\A[A-Za-z0-9][A-Za-z0-9._-]*(?:/[A-Za-z0-9][A-Za-z0-9._-]*)?\Z")

# Branches this capability refuses to publish to, whatever else is configured.
# The default branch is the one nothing here may write: a repair reaches it by
# being reviewed and merged, never by being pushed.
PROTECTED_BRANCHES = frozenset({"main", "master", "HEAD"})


PUBLICATION_FAILURES = (
    "arguments_unusable",
    "publication_unavailable",
    # The branch is not one this capability may publish.
    "branch_not_permitted",
    # The local branch does not exist, or does not point where it claimed.
    "branch_unknown",
    # The remote holds commits this push would discard. Never forced.
    "branch_diverged",
    # The remote refused: protection, permissions, or a rejected non-fast-forward.
    "publication_refused",
)


PULL_REQUEST_FAILURES = (
    "arguments_unusable",
    "pull_request_unavailable",
    # The head branch has not been published, so there is nothing to open against.
    "head_unpublished",
    # GitHub refused to open it.
    "pull_request_refused",
)


class PublicationError(Exception):
    """A branch could not be published, with a declared machine-readable code."""

    def __init__(self, code: str) -> None:
        if code not in PUBLICATION_FAILURES:
            raise ValueError("publication failures must be declared")
        self.code = code
        super().__init__(code)


class PullRequestError(Exception):
    """A pull request could not be opened, with a declared code."""

    def __init__(self, code: str) -> None:
        if code not in PULL_REQUEST_FAILURES:
            raise ValueError("pull request failures must be declared")
        self.code = code
        super().__init__(code)


def valid_sha(value: str) -> bool:
    """Whether this is a full 40-character commit identifier."""
    return isinstance(value, str) and _FULL_SHA.match(value) is not None


def publishable_branch(value: str) -> bool:
    """Whether this names a branch this capability may publish.

    Shape and identity, both. A name git would accept but this will not is
    refused here rather than sanitised, because quietly publishing something
    adjacent to what was asked for is worse than refusing.
    """
    if not isinstance(value, str) or not _BRANCH.match(value):
        return False
    return value not in PROTECTED_BRANCHES


@dataclass(frozen=True, slots=True)
class PublicationRequest:
    """One repair branch AL/X has decided to publish, at one exact commit.

    `head_sha` is supplied rather than read. The branch is about to leave the
    machine, so what is published must be the revision she decided to publish:
    reading whatever the branch points at now would let work written after her
    decision travel under it.
    """

    branch: str
    head_sha: str

    def __post_init__(self) -> None:
        if not publishable_branch(self.branch):
            raise ValueError("branch is not one this capability may publish")
        if not valid_sha(self.head_sha):
            raise ValueError("head_sha must be a full 40-character commit id")


@dataclass(frozen=True, slots=True)
class PublicationOutcome:
    """That a branch was published, and exactly which commit reached the remote."""

    branch: str
    head_sha: str
    published: bool
    already_current: bool = False

    def __post_init__(self) -> None:
        if not self.branch.strip():
            raise ValueError("the branch must be named")
        if not valid_sha(self.head_sha):
            raise ValueError("head_sha must be a full 40-character commit id")

    def as_values(self) -> dict[str, object]:
        return {
            "branch": self.branch,
            "head_sha": self.head_sha,
            "published": self.published,
            "already_current": self.already_current,
        }


@dataclass(frozen=True, slots=True)
class PullRequestRequest:
    """One pull request AL/X has decided to open for a published branch.

    The base is not an input. Every repair goes to the default branch, and a
    caller-chosen base would let work be proposed into somewhere nobody is
    watching — a pull request against a branch with no gates and no reviewer is
    a review that never happens.
    """

    branch: str
    title: str
    body: str = ""

    def __post_init__(self) -> None:
        if not publishable_branch(self.branch):
            raise ValueError("branch is not one this capability may publish")
        if not self.title.strip():
            raise ValueError("a pull request needs a title")


@dataclass(frozen=True, slots=True)
class PullRequestOutcome:
    """A pull request and the exact revision it currently points at.

    `created` is false when an open pull request for this branch already
    existed. Opening a second one for the same work would split the review
    across two places and leave a clean review attached to a pull request
    nobody merges, so the existing one is returned as the fact it is.
    """

    pull_request_number: int
    branch: str
    head_sha: str
    base: str
    state: str
    created: bool

    def __post_init__(self) -> None:
        if not isinstance(self.pull_request_number, int) or isinstance(
            self.pull_request_number, bool
        ):
            raise TypeError("pull_request_number must be an integer")
        if self.pull_request_number <= 0:
            raise ValueError("pull_request_number must be positive")
        if not valid_sha(self.head_sha):
            raise ValueError("head_sha must be a full 40-character commit id")
        for name, value in (("branch", self.branch), ("base", self.base),
                            ("state", self.state)):
            if not value.strip():
                raise ValueError(f"{name} must not be blank")

    def as_values(self) -> dict[str, object]:
        return {
            "pull_request_number": self.pull_request_number,
            "branch": self.branch,
            "head_sha": self.head_sha,
            "base": self.base,
            "state": self.state,
            "created": self.created,
        }


__all__ = [
    "PROTECTED_BRANCHES",
    "PUBLICATION_FAILURES",
    "PULL_REQUEST_FAILURES",
    "PublicationError",
    "PublicationOutcome",
    "PublicationRequest",
    "PullRequestError",
    "PullRequestOutcome",
    "PullRequestRequest",
    "publishable_branch",
    "valid_sha",
]
