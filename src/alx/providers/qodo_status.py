"""Look at what Qodo has published for one pull request revision.

Read-only, and deliberately incapable of anything else. It performs GET
requests and returns a state. It cannot request a review, retry one, spend
anything, change code or merge: it imports nothing that could, and a test
asserts that structurally.

Two things count as a result, both agreed with Friedl:

A formal review object whose `commit_id` is the revision. GitHub sets that
field, so it is the stronger signal.

Or Qodo's own output saying it was updated up to that revision. That marker is
written by Qodo rather than attested by GitHub, and it is used here for one
purpose only: noticing that a result has appeared. It is not evidence of what
was reviewed, and nothing downstream treats it as such - a clean Qodo review
publishes no review object at all, so without this a finished review would look
like one that never arrived.

What the result says is never read here. Findings are the Core's to evaluate.
"""

from __future__ import annotations

import re
from datetime import UTC, datetime

import httpx

from alx.contracts.task import TaskObservation, TaskState


API_ROOT = "https://api.github.com"
TIMEOUT_SECONDS = 30.0

# Enough for any realistic pull request; a bound so one tick cannot run on.
_MAX_PAGES = 10

# Qodo's account, which is what makes a comment or review its output rather
# than someone else's.
REVIEWER_ID = 151058649

_SUBJECT = re.compile(r"^pull/(?P<number>\d+)@(?P<sha>[0-9a-f]{40})$")

# One path segment: no slashes, spaces, traversal or query characters. The
# same rule as the review and merge providers, because the same value builds
# the same kind of URL. A weaker check here accepted "owner/re?po" and
# "own er/repo", which registered fine and then made every read malformed.
_SEGMENT = re.compile(r"^[A-Za-z0-9._-]+$")


def subject_reference(pull_request_number: int, head_sha: str) -> str:
    """How a review task names what it is about."""
    return f"pull/{pull_request_number}@{head_sha}"


def _after(moment: object, since: datetime | None) -> bool:
    """Whether a published result is newer than the request that awaits it.

    An unreadable timestamp counts as new. A result that exists and cannot be
    dated is better reported than left outstanding forever.
    """
    if since is None:
        return True
    if not isinstance(moment, str):
        return True
    try:
        published = datetime.fromisoformat(moment.replace("Z", "+00:00"))
    except ValueError:
        return True
    return published >= since


class QodoStatusObserver:
    """Reports whether a Qodo result now covers one exact revision."""

    service = "qodo"

    def __init__(self, repository: str, token: str, api_root: str = API_ROOT) -> None:
        parts = repository.strip().split("/")
        if len(parts) != 2 or not all(_SEGMENT.match(part) for part in parts):
            raise ValueError("repository must be owner/name")
        if not token.strip():
            raise ValueError("a GitHub token is required")
        self._repository = repository.strip()
        self._token = token
        self._api_root = api_root.rstrip("/")

    def _get(self, url: str) -> object | None:
        try:
            response = httpx.get(
                url,
                headers={
                    "Authorization": f"Bearer {self._token}",
                    "Accept": "application/vnd.github+json",
                    "X-GitHub-Api-Version": "2022-11-28",
                    "User-Agent": "alx-task-status",
                },
                timeout=TIMEOUT_SECONDS,
            )
            if response.status_code != 200:
                return None
            return response.json()
        except (httpx.HTTPError, ValueError):
            # Severed from the original so the token cannot travel on it.
            return None

    def _pages(self, url: str) -> list | None:
        """Every page of a listing, or None when it cannot be read.

        A busy pull request outgrows one page, and a result on a later page
        would otherwise leave the task outstanding forever. Bounded so an
        unexpectedly long history cannot make a tick run without end.
        """
        items: list = []
        for page in range(1, _MAX_PAGES + 1):
            batch = self._get(f"{url}?per_page=100&page={page}")
            if not isinstance(batch, list):
                return None
            items.extend(batch)
            if len(batch) < 100:
                break
        return items

    def observe(self, subject: str, since: datetime | None = None) -> TaskObservation:
        """Whether a Qodo result now covers the revision this task names.

        `since` excludes results that already existed when the review was
        asked for. Without it, asking again for an unchanged revision would be
        completed instantly by the previous answer.
        """
        now = datetime.now(UTC)
        match = _SUBJECT.match(subject)
        if match is None:
            # A subject this observer cannot read is one it cannot report on.
            return TaskObservation(TaskState.STATUS_UNKNOWN, now)
        number = int(match.group("number"))
        sha = match.group("sha")

        reviews = self._pages(
            f"{self._api_root}/repos/{self._repository}/pulls/{number}/reviews"
        )
        if reviews is None:
            return TaskObservation(TaskState.STATUS_UNKNOWN, now)
        for review in reviews:
            if not isinstance(review, dict):
                continue
            if (review.get("user") or {}).get("id") != REVIEWER_ID:
                continue
            if review.get("commit_id") == sha and _after(
                review.get("submitted_at"), since
            ):
                return TaskObservation(TaskState.COMPLETED, now)

        comments = self._pages(
            f"{self._api_root}/repos/{self._repository}/issues/{number}/comments"
        )
        if comments is None:
            return TaskObservation(TaskState.STATUS_UNKNOWN, now)
        for comment in comments:
            if not isinstance(comment, dict):
                continue
            if (comment.get("user") or {}).get("id") != REVIEWER_ID:
                continue
            body = comment.get("body")
            # Completion observation only. The marker says a result now covers
            # this revision; it is never read as evidence of what was reviewed.
            if (
                isinstance(body, str)
                and f"/commit/{sha}" in body
                and _after(comment.get("updated_at"), since)
            ):
                return TaskObservation(TaskState.COMPLETED, now)

        return TaskObservation(TaskState.WAITING_FOR_RESULT, now)
