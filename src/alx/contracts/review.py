"""Records for asking an external reviewer to look at a pull request.

This requests a review. It does not read one, judge one, or decide anything
about what a reviewer says. Whether findings matter, and whether the change may
merge, are judgements AL/X makes from the review itself.

`head_sha` is required so the request names the revision she meant to have
reviewed. It is checked against the pull request before the reviewer is
contacted: if the branch has moved, the request is refused rather than spending
a review on a revision nobody asked about.

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
    # The branch moved after Friedl asked. Refused before the reviewer is
    # contacted, because a review of a revision nobody named is wasted.
    "head_changed",
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
    """One pull request AL/X has been asked to have reviewed, at one revision."""

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
class ReviewOutcome:
    """That a review was requested. Never what it will say."""

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
