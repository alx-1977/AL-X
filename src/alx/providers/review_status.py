"""Notice when the configured reviewer has published something readable.

A review is asked for and then arrives later, so something has to watch for it.
This is that watcher, and it reports completion on one condition only: that the
review can actually be read for the exact revision it was requested about. A
reviewer that has commented on the pull request but not on this head has not
finished the work that was asked for, and saying otherwise would hand AL/X a
completion for evidence she cannot use.

It reuses the same provider that reads reviews, so the account matching, the
head binding and the notion of "available" are one implementation rather than
two that have to agree. Watching a different reviewer is configuration.

Completion is also bound in time. A review published before the request was
made is the answer to the previous request, and letting it complete the new one
reports a review as finished the moment it is asked for.

Nothing here reads the review's meaning. It answers whether there is something
to read, and AL/X reads it.
"""

from __future__ import annotations

import re
from datetime import UTC, datetime
from typing import Any

from alx.contracts.review_content import ReviewContentRequest, ReviewReadError
from alx.contracts.task import TaskObservation, TaskState

# `pull/<number>` or `pull/<number>@<sha>`, as `subject_reference` writes it.
_SUBJECT = re.compile(
    r"\Apull/(?P<number>[1-9][0-9]*)(?:@(?P<sha>[0-9a-f]{40}))?\Z"
)


def subject_reference(pull_request_number: int, head_sha: str = "") -> str:
    """Name the exact thing being watched: one pull request at one revision."""
    suffix = f"@{head_sha}" if head_sha else ""
    return f"pull/{pull_request_number}{suffix}"


class ReviewStatusObserver:
    """Report a review complete only when it can be read for that revision."""

    def __init__(self, provider: Any, clock: Any = None) -> None:
        self._provider = provider
        self._now = clock or (lambda: datetime.now(UTC))

    @property
    def service(self) -> str:
        """The reviewer being watched, as the task store records it."""
        return self._provider.reviewer

    def observe(self, subject: str, since: datetime | None = None) -> TaskObservation:
        now = self._now()
        match = _SUBJECT.match(subject)
        if match is None:
            return TaskObservation(TaskState.STATUS_UNKNOWN, now)
        head_sha = match.group("sha")
        if not head_sha:
            # Without a revision there is nothing to bind a review to, and a
            # review of some other head must never be reported as this one's.
            return TaskObservation(TaskState.STATUS_UNKNOWN, now)
        try:
            content = self._provider.read(
                ReviewContentRequest(
                    pull_request_number=int(match.group("number")),
                    head_sha=head_sha,
                )
            )
        except (ReviewReadError, TypeError, ValueError):
            # Unreadable is not incomplete: the review may be published and the
            # transport merely unavailable, so nothing is claimed either way.
            return TaskObservation(TaskState.STATUS_UNKNOWN, now)
        if not content.available:
            return TaskObservation(TaskState.WAITING_FOR_RESULT, now)
        if since is not None and content.submitted_at is not None:
            # A review published before this request was made answers the
            # previous one. Asking again for an unchanged revision is a new
            # occasion, and without this the old answer completes the new
            # request the moment it is made — a review reported as done that
            # never ran.
            if content.submitted_at <= since:
                return TaskObservation(TaskState.WAITING_FOR_RESULT, now)
        return TaskObservation(TaskState.COMPLETED, now)


__all__ = ["ReviewStatusObserver", "subject_reference"]
