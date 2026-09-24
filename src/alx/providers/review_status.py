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

Completion is also bound in time, but time alone was the wrong test. A review
published before the request was made is usually the answer to the previous
request, and letting it complete the new one reports a review as finished the
moment it is asked for. It is not always that, though: a reviewer that reviews
a new pull request unasked publishes before the request that follows it, and
that verdict is about this revision and has been read by nobody. Waiting for a
second one leaves the task stuck on a revision already reviewed.

What separates the two is not when the verdict arrived but whether anything has
already taken it. So the caller may supply that fact, and an older verdict for
this exact revision completes the task when no earlier task consumed it.

Nothing here reads the review's meaning. It answers whether there is something
to read, and AL/X reads it. A closed or merged PR with no fresh readable result
ends the outstanding request as failed; closure never certifies a review.
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
    """Observe exact-head completion or closure of an unresolved request."""

    def __init__(self, provider: Any, clock: Any = None) -> None:
        self._provider = provider
        self._now = clock or (lambda: datetime.now(UTC))

    @property
    def service(self) -> str:
        """The reviewer being watched, as the task store records it."""
        return self._provider.reviewer

    def _unresolved(
        self, number: int, now: datetime, state: TaskState
    ) -> TaskObservation:
        # Closing a PR ends its outstanding review request. It does not prove
        # that this exact head was reviewed: report a terminal failure to
        # obtain the requested result, never a fabricated review completion.
        closed = getattr(self._provider, "pull_request_closed", None)
        if closed is not None:
            try:
                if closed(number):
                    return TaskObservation(TaskState.FAILED, now)
            except (ReviewReadError, TypeError, ValueError):
                pass
        return TaskObservation(state, now)

    def observe(
        self,
        subject: str,
        since: datetime | None = None,
        already_consumed: bool = False,
    ) -> TaskObservation:
        """Whether a readable verdict exists for this exact revision.

        `already_consumed` says an earlier task for this same subject and head
        already took a verdict. The caller knows that — it holds the durable
        task history — and this stays a reader of review evidence rather than
        of its own past.
        """
        now = self._now()
        match = _SUBJECT.match(subject)
        if match is None:
            return TaskObservation(TaskState.STATUS_UNKNOWN, now)
        number = int(match.group("number"))
        head_sha = match.group("sha")
        if not head_sha:
            # Without a revision there is nothing to bind a review to, and a
            # review of some other head must never be reported as this one's.
            return self._unresolved(number, now, TaskState.STATUS_UNKNOWN)
        try:
            content = self._provider.read(
                ReviewContentRequest(
                    pull_request_number=number,
                    head_sha=head_sha,
                )
            )
        except (ReviewReadError, TypeError, ValueError):
            # Unreadable is not incomplete: the review may be published and the
            # transport merely unavailable, so nothing is claimed either way.
            return self._unresolved(number, now, TaskState.STATUS_UNKNOWN)
        if not content.available:
            return self._unresolved(number, now, TaskState.WAITING_FOR_RESULT)
        # Available means the reader bound this verdict to the exact revision
        # asked about: the reviewer's prose names the forty-character sha, or
        # it is the review object GitHub records that commit against. Nothing
        # else becomes available.
        if (
            already_consumed
            and since is not None
            and content.submitted_at is not None
            and content.submitted_at <= since
        ):
            # An earlier task already took this verdict, so asking again is a
            # request for a second look at an unchanged revision. Handing back
            # what was already delivered would report a review that never ran.
            return self._unresolved(number, now, TaskState.WAITING_FOR_RESULT)
        return TaskObservation(TaskState.COMPLETED, now)


__all__ = ["ReviewStatusObserver", "subject_reference"]
