"""Records for asking an external reviewer to look at a pull request.

This requests a review. It does not read one, judge one, or decide anything
about what a reviewer says. Whether findings matter, and whether the change may
merge, are judgements AL/X makes from the review itself.

A request names a pull request, not a revision. Friedl asks for the pull
request to be reviewed; which commit is current is a fact to be read, not
something he should have to supply. The provider reads it when it fetches the
pull request, and the outcome reports the revision that was current at that
moment, so the record says exactly what was sent for review.

One invocation is one request. Nothing here retries, re-requests after a
failure, or asks again because a review found something.
"""

from __future__ import annotations

import re
from dataclasses import dataclass


_FULL_SHA = re.compile(r"^[0-9a-f]{40}$")


REVIEW_FAILURES = (
    "arguments_unusable",
    "review_unavailable",
    # The reviewer or the transport refused the request.
    "review_refused",
)


class ReviewError(Exception):
    """A review could not be requested, with a declared machine-readable code."""

    def __init__(self, code: str) -> None:
        if code not in REVIEW_FAILURES:
            raise ValueError("review failures must be declared")
        self.code = code
        super().__init__(code)


def valid_sha(value: str) -> bool:
    """Whether this is a full 40-character commit identifier."""
    return isinstance(value, str) and _FULL_SHA.match(value) is not None


@dataclass(frozen=True, slots=True)
class ReviewRequest:
    """One pull request AL/X has been asked to have reviewed."""

    pull_request_number: int

    def __post_init__(self) -> None:
        if not isinstance(self.pull_request_number, int) or isinstance(
            self.pull_request_number, bool
        ):
            raise TypeError("pull_request_number must be an integer")
        if self.pull_request_number <= 0:
            raise ValueError("pull_request_number must be positive")


@dataclass(frozen=True, slots=True)
class ReviewOutcome:
    """That a review was requested, and of which revision. Never what it says.

    `head_sha` is read rather than supplied: it is the commit the pull request
    pointed at when the review was requested, so the record is a fact about
    what was sent rather than a claim about what someone intended.
    """

    pull_request_number: int
    head_sha: str
    requested: bool
    reviewer: str

    def __post_init__(self) -> None:
        if not valid_sha(self.head_sha):
            raise ValueError("head_sha must be a full 40-character commit id")
        if not self.reviewer.strip():
            raise ValueError("the reviewer must be named")

    def as_values(self) -> dict[str, object]:
        return {
            "pull_request_number": self.pull_request_number,
            "head_sha": self.head_sha,
            "requested": self.requested,
            "reviewer": self.reviewer,
        }


__all__ = [
    "REVIEW_FAILURES",
    "ReviewError",
    "ReviewOutcome",
    "ReviewRequest",
    "valid_sha",
]
