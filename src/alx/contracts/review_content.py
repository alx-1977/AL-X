"""Records for reading what an external reviewer said about one revision.

Requesting a review and reading one are different acts, and this is only the
second. Nothing here asks for a review, waits for one, or merges anything.

The whole record is bound to one pull request at one exact commit. A review is
advice about a specific revision, and advice about a revision nobody is looking
at is worse than none: on 2026-09-06 a review of an earlier head was nearly
read as though it covered the current one. So retrieval names the revision, and
a result that cannot be tied to that exact commit is reported as unavailable
rather than offered as an approximation.

What a review *says* is carried verbatim and judged nowhere in this layer.
There is deliberately no clean/unclean field, no severity ranking and no
finding count that anything here computes: whether a review permits a merge is
a judgement, and under D-026 it is AL/X's. A tool that answered it would be
deciding the thing she was delegated.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime


# \Z rather than $: $ also matches before a terminal newline, so a value with
# one appended passed validation elsewhere and reached GitHub as an
# authorisation nobody could act on. Same rule, same reason.
_FULL_SHA = re.compile(r"\A[0-9a-f]{40}\Z")


REVIEW_READ_FAILURES = (
    "arguments_unusable",
    # The reviewer, or the transport, could not be read. Distinct from a
    # revision that simply has no review: this one may succeed later.
    "review_unavailable",
)


class ReviewReadError(Exception):
    """A review could not be read, with a declared machine-readable code."""

    def __init__(self, code: str) -> None:
        if code not in REVIEW_READ_FAILURES:
            raise ValueError("review read failures must be declared")
        self.code = code
        super().__init__(code)


def valid_sha(value: str) -> bool:
    """Whether this is a full 40-character commit identifier.

    An abbreviation is refused. The point of naming the revision is that
    exactly one is read, and a prefix could match a commit nobody reviewed.
    """
    return isinstance(value, str) and _FULL_SHA.match(value) is not None


@dataclass(frozen=True, slots=True)
class ReviewContentRequest:
    """One pull request revision whose review AL/X wants to read."""

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
class ReviewComment:
    """One thing the reviewer said, and where it said it.

    `body` is the reviewer's own words, reproduced rather than interpreted.
    Nothing here scores it, and `path` and `line` are only ever what the
    reviewer attached: this does not resolve them against the repository.
    """

    body: str
    path: str = ""
    line: int | None = None

    def as_values(self) -> dict[str, object]:
        values: dict[str, object] = {"body": self.body}
        if self.path:
            values["path"] = self.path
        if self.line is not None:
            values["line"] = self.line
        return values


@dataclass(frozen=True, slots=True)
class ReviewContent:
    """What a reviewer published about one exact revision, or that it has not.

    `available` is the only thing resembling a verdict here, and it is a fact
    about retrieval rather than about the code: whether a review by this
    reviewer, for this exact commit, was found. A review that says nothing is
    wrong and a revision with no review at all are different states, and
    collapsing them would let silence read as approval.

    There is no `clean` field. Whether the findings permit a merge is AL/X's
    judgement, made from `summary` and `comments` as the reviewer wrote them.
    """

    pull_request_number: int
    head_sha: str
    reviewer: str
    available: bool
    # The reviewer's own summary text, empty when there is none.
    summary: str = ""
    comments: tuple[ReviewComment, ...] = ()
    submitted_at: datetime | None = None
    retrieved_at: datetime | None = None
    # Why nothing was found, when nothing was. A fact about the search, never
    # an opinion about the code.
    unavailable_reason: str = ""

    def __post_init__(self) -> None:
        if not valid_sha(self.head_sha):
            raise ValueError("head_sha must be a full 40-character commit id")
        if not self.reviewer.strip():
            raise ValueError("the reviewer must be named")
        if self.available and self.submitted_at is None:
            raise ValueError("a retrieved review records when it was submitted")
        if not self.available and (self.summary or self.comments):
            raise ValueError("an unavailable review carries no review content")

    def as_values(self) -> dict[str, object]:
        values: dict[str, object] = {
            "pull_request_number": self.pull_request_number,
            "head_sha": self.head_sha,
            "reviewer": self.reviewer,
            "available": self.available,
            "summary": self.summary,
            "comments": tuple(item.as_values() for item in self.comments),
        }
        if self.submitted_at is not None:
            values["submitted_at"] = self.submitted_at.isoformat()
        if self.retrieved_at is not None:
            values["retrieved_at"] = self.retrieved_at.isoformat()
        if self.unavailable_reason:
            values["unavailable_reason"] = self.unavailable_reason
        return values


__all__ = [
    "REVIEW_READ_FAILURES",
    "ReviewComment",
    "ReviewContent",
    "ReviewContentRequest",
    "ReviewReadError",
    "valid_sha",
]
