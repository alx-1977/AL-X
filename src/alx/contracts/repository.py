"""Records for merging a reviewed pull request.

Friedl delegated routine merge authorisation to AL/X. An external reviewer
examines the current head and reports what it found; she reads that, decides
whether anything needs correcting, and either withholds the merge or performs
it. The reviewer advises. The decision is hers.

`head_sha` is the whole safety mechanism. It is required, it is what the
reviewer examined, and it travels to GitHub as the exact revision the merge is
permitted to move. If the branch has advanced since she judged it, GitHub
refuses the merge rather than merging code nobody reviewed. That refusal is
plumbing, not judgement: the decision was already made about a specific
revision, and this only holds the execution to it.

Nothing here evaluates a review, scores a finding, or decides whether a merge
is warranted.
"""

from __future__ import annotations

import re
from dataclasses import dataclass


# One merge method, fixed deliberately rather than configured.
#
# `main` requires linear history, and every merge in this repository so far has
# been a squash, so this is the established shape rather than a preference
# expressed in code. Making it configurable would add a policy surface nobody
# has asked for, and making it an AL/X decision would put a choice with one
# correct answer into a reasoning call, which Law 2 says belongs in code.
#
# If the repository's merge policy ever changes, this changes with it.
MERGE_METHOD = "squash"

# Bounded because they are transported and recorded, not because length says
# anything about content.
MAX_TITLE_CHARACTERS = 200
MAX_MESSAGE_CHARACTERS = 8_000

# \Z rather than $: $ also matches before a terminal newline, so a value
# with one appended passed validation and reached GitHub as an
# authorisation nobody could act on.
_FULL_SHA = re.compile(r"\A[0-9a-f]{40}\Z")


MERGE_FAILURES = (
    "arguments_unusable",
    "merge_unavailable",
    # The branch moved after the review AL/X read. Distinct from a refusal by
    # branch protection, because the response is different: this one is fixed
    # by reviewing the new head, not by fixing the code.
    "head_changed",
    # Branch protection, an unmergeable state, or a conflict. Reported as the
    # fact it is; nothing here retries or works around it.
    "merge_refused",
)


class MergeError(Exception):
    """A merge could not be performed, with a declared machine-readable code."""

    def __init__(self, code: str) -> None:
        if code not in MERGE_FAILURES:
            raise ValueError("merge failures must be declared")
        self.code = code
        super().__init__(code)


def valid_sha(value: str) -> bool:
    """Whether this is a full 40-character commit identifier.

    An abbreviation is refused. The point of naming the head is that exactly
    one revision is authorised, and a prefix could match a commit nobody
    reviewed.
    """
    return isinstance(value, str) and _FULL_SHA.match(value) is not None


@dataclass(frozen=True, slots=True)
class MergeRequest:
    """One pull request AL/X has decided may merge, at one exact revision."""

    pull_request_number: int
    head_sha: str
    title: str = ""
    message: str = ""

    def __post_init__(self) -> None:
        if not isinstance(self.pull_request_number, int) or isinstance(
            self.pull_request_number, bool
        ):
            raise TypeError("pull_request_number must be an integer")
        if self.pull_request_number <= 0:
            raise ValueError("pull_request_number must be positive")
        if not valid_sha(self.head_sha):
            raise ValueError("head_sha must be a full 40-character commit id")
        if len(self.title) > MAX_TITLE_CHARACTERS:
            raise ValueError("title exceeds the permitted size")
        if len(self.message) > MAX_MESSAGE_CHARACTERS:
            raise ValueError("message exceeds the permitted size")


@dataclass(frozen=True, slots=True)
class MergeOutcome:
    """What happened when the merge was attempted."""

    pull_request_number: int
    head_sha: str
    merged: bool
    merge_commit_sha: str = ""

    def __post_init__(self) -> None:
        if not valid_sha(self.head_sha):
            raise ValueError("head_sha must be a full 40-character commit id")
        if self.merged and not valid_sha(self.merge_commit_sha):
            raise ValueError("a completed merge reports its merge commit")
        if not self.merged and self.merge_commit_sha:
            raise ValueError("a merge that did not happen has no merge commit")

    def as_values(self) -> dict[str, object]:
        return {
            "pull_request_number": self.pull_request_number,
            "head_sha": self.head_sha,
            "merged": self.merged,
            "merge_commit_sha": self.merge_commit_sha,
        }


__all__ = [
    "MAX_MESSAGE_CHARACTERS",
    "MAX_TITLE_CHARACTERS",
    "MERGE_FAILURES",
    "MERGE_METHOD",
    "MergeError",
    "MergeOutcome",
    "MergeRequest",
    "valid_sha",
]
