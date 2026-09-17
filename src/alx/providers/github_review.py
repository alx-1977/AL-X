"""Ask the configured reviewer for a look, and read what it published.

One provider serves every reviewer AL/X can use, because every one of them
works the same way: it watches this repository through GitHub, reviews a pull
request when one is opened, reviews again when asked in a comment, and
publishes what it found as pull request comments and reviews. The differences
between them are a name, a trigger phrase and a bot login, and those live in
`contracts/review_provider.py` rather than here.

## Requesting

A new pull request is reviewed without being asked. The trigger exists for the
other case: a corrective commit moved the head, the previous review is evidence
about a revision that no longer exists, and a fresh look is needed. So a request
is a comment, and the outcome records the revision the pull request pointed at
when it was left.

## Reading

`ReviewContent` reports a review of one exact revision or reports that there is
none. The exactness is the point: a review is evidence about the commit it
examined, and a clean review of an earlier head must never read as approval of
the code that replaced it. Where a reviewer states the range it covered, that
statement is what binds the review to a head; where it does not, the review's
own commit is used, and where neither is available the content is reported
unavailable rather than guessed at.

Nothing here judges. There is no severity, no finding count, no clean flag and
no merge opinion: the reviewer's words are returned as the reviewer wrote them,
marked as external content, and what they mean is AL/X's to decide.
"""

from __future__ import annotations

import logging
import re
from datetime import UTC, datetime
from typing import Any

import httpx

from alx.contracts.review import ReviewError, ReviewOutcome, ReviewRequest
from alx.contracts.review_content import (
    ReviewComment,
    ReviewContent,
    ReviewContentRequest,
    ReviewReadError,
)
from alx.contracts.review_provider import ReviewProviderProfile
from alx.providers.github_http import unavailable

LOGGER = logging.getLogger(__name__)

API_ROOT = "https://api.github.com"

# One path segment: no slashes, traversal or query characters.
_SEGMENT = re.compile(r"^[A-Za-z0-9._-]+$")

_SHA = re.compile(r"\b[0-9a-f]{40}\b")

# The whole request, not one socket operation.
TIMEOUT_SECONDS = 30.0

# How many pages of comments are read before giving up. A pull request with
# more than this is not one a reviewer's latest word is being missed on; it is
# one where something else has gone wrong.
MAX_PAGES = 10

# Why a read found nothing. A fact about the search — this reviewer has not
# published about this revision — never an opinion about the code.
NO_REVIEW_FOR_REVISION = "no_review_for_revision"


def _moment(value: object) -> datetime | None:
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo is not None else parsed.replace(tzinfo=UTC)


class GitHubReviewProvider:
    """Request and read reviews by one configured reviewer, through GitHub."""

    def __init__(
        self,
        repository: str,
        token: str,
        profile: ReviewProviderProfile,
        api_root: str = API_ROOT,
        clock: Any = None,
    ) -> None:
        owner, _, name = repository.strip().partition("/")
        if not _SEGMENT.match(owner) or not _SEGMENT.match(name):
            raise ValueError("repository must be exactly owner/name")
        if not token.strip():
            raise ValueError("a GitHub token is required")
        self._owner = owner
        self._name = name
        self._repository = f"{owner}/{name}"
        self._token = token.strip()
        self._profile = profile
        self._api_root = api_root.rstrip("/")
        self._now = clock or (lambda: datetime.now(UTC))

    @property
    def reviewer(self) -> str:
        return self._profile.reviewer

    def _headers(self) -> dict[str, str]:
        return {
            "Authorization": f"Bearer {self._token}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
        }

    def _call(self, method: str, path: str, payload: object = None) -> object:
        try:
            response = httpx.request(
                method,
                f"{self._api_root}{path}",
                headers=self._headers(),
                json=payload,
                timeout=TIMEOUT_SECONDS,
            )
        except httpx.HTTPError as error:
            # The type, never the message: the wording can carry a URL with a
            # token in it.
            LOGGER.warning("GitHub request failed: %s", type(error).__name__)
            raise _Unavailable() from error
        if unavailable(response):
            raise _Unavailable()
        if response.status_code >= 400:
            raise _Refused()
        try:
            return response.json()
        except ValueError as error:
            raise _Unavailable() from error

    def _pages(self, path: str) -> list[Any]:
        items: list[Any] = []
        for page in range(1, MAX_PAGES + 1):
            joiner = "&" if "?" in path else "?"
            found = self._call("GET", f"{path}{joiner}per_page=100&page={page}")
            if not isinstance(found, list) or not found:
                return items
            items.extend(found)
            if len(found) < 100:
                return items
        return items

    def _head(self, number: int) -> str:
        pull = self._call("GET", f"/repos/{self._repository}/pulls/{number}")
        if not isinstance(pull, dict):
            raise _Unavailable()
        head = pull.get("head")
        sha = head.get("sha") if isinstance(head, dict) else None
        return sha if isinstance(sha, str) and _SHA.fullmatch(sha) else ""

    # ---- requesting -----------------------------------------------------

    def request(self, review: ReviewRequest) -> ReviewOutcome:
        """Ask the configured reviewer to look at this pull request again."""
        number = review.pull_request_number
        try:
            head = self._head(number)
            self._call(
                "POST",
                f"/repos/{self._repository}/issues/{number}/comments",
                {"body": self._profile.trigger},
            )
            # Read again after the comment lands. If the head moved while the
            # request was going out, the revision the reviewer will examine is
            # not established, and saying so is better than naming the commit
            # that happened to be current a moment earlier.
            confirmed = self._head(number)
        except _Refused as error:
            raise ReviewError("review_refused") from error
        except _Unavailable as error:
            raise ReviewError("review_unavailable") from error
        return ReviewOutcome(
            pull_request_number=number,
            head_sha=head if head and head == confirmed else "",
            requested=True,
            reviewer=self._profile.reviewer,
            requested_at=self._now(),
        )

    # ---- reading --------------------------------------------------------

    def _authored(self, item: object) -> bool:
        return (
            isinstance(item, dict)
            and isinstance(item.get("user"), dict)
            and self._profile.authored_by_reviewer(item["user"].get("login"))
        )

    @staticmethod
    def _comment(item: dict) -> ReviewComment | None:
        body = item.get("body")
        if not isinstance(body, str) or not body.strip():
            return None
        line = item.get("line")
        return ReviewComment(
            body=body,
            path=item.get("path") if isinstance(item.get("path"), str) else "",
            line=line if isinstance(line, int) and not isinstance(line, bool) else None,
        )

    @staticmethod
    def _about_revision(item: dict, head_sha: str) -> bool:
        """Whether GitHub says this comment belongs to this revision.

        Read from the comment's own revision fields rather than inferred from
        when it was posted: a timestamp says a comment exists, never what it
        was written about.
        """
        for field in ("commit_id", "original_commit_id"):
            value = item.get(field)
            if isinstance(value, str) and value == head_sha:
                return True
        return False

    def _covers(self, body: str, head_sha: str) -> bool:
        """Whether this text is the reviewer saying it looked at this revision.

        Reviewers state the range they covered — CodeRabbit writes "between
        <base> and <head>". Where such a statement is present it is what binds
        the review to a commit, because it is the reviewer's own account of
        what it read rather than an inference from when something was posted.
        """
        return head_sha in body

    def read(self, request: ReviewContentRequest) -> ReviewContent:
        """Return what the reviewer published about this exact revision."""
        number = request.pull_request_number
        head_sha = request.head_sha
        try:
            issue_comments = self._pages(
                f"/repos/{self._repository}/issues/{number}/comments"
            )
            reviews = self._pages(f"/repos/{self._repository}/pulls/{number}/reviews")
            inline = self._pages(f"/repos/{self._repository}/pulls/{number}/comments")
        except (_Refused, _Unavailable) as error:
            # Both mean the same thing to a reader: nothing is known about the
            # review right now, and it may be readable later. The contract
            # declares one code for that, and inventing a second here would be
            # a distinction nothing downstream can act on.
            raise ReviewReadError("review_unavailable") from error

        # The reviewer's own summaries, newest first, restricted to ones that
        # name this revision. A summary about an earlier head is evidence about
        # that head and must not answer a question about this one.
        summaries = [
            item
            for item in issue_comments + reviews
            if self._authored(item)
            and isinstance(item.get("body"), str)
            and self._covers(item["body"], head_sha)
        ]
        if not summaries:
            return ReviewContent(
                pull_request_number=number,
                head_sha=head_sha,
                reviewer=self._profile.reviewer,
                available=False,
                retrieved_at=self._now(),
                unavailable_reason=NO_REVIEW_FOR_REVISION,
            )

        def when(item: dict) -> datetime:
            return (
                _moment(item.get("submitted_at"))
                or _moment(item.get("created_at"))
                or datetime.min.replace(tzinfo=UTC)
            )

        latest = max(summaries, key=when)
        # Bound to the revision, not merely to the pull request. A comment
        # written against an earlier head is a finding about code that has
        # since changed, and returning it as this revision's would undo the
        # exactness the summary is selected for: `ReviewContent` for head A
        # would carry findings somebody wrote about head B.
        #
        # GitHub states this on the comment itself. `commit_id` is where it
        # applies now and `original_commit_id` is where it was written; either
        # matching is enough to say it belongs to this revision, and a comment
        # carrying neither is excluded rather than guessed at.
        comments = tuple(
            comment
            for comment in (
                self._comment(item)
                for item in inline
                if self._authored(item) and self._about_revision(item, head_sha)
            )
            if comment is not None
        )
        return ReviewContent(
            pull_request_number=number,
            head_sha=head_sha,
            reviewer=self._profile.reviewer,
            available=True,
            summary=latest["body"],
            comments=comments,
            submitted_at=when(latest),
            retrieved_at=self._now(),
        )


class _Unavailable(Exception):
    """GitHub could not answer; nothing is known about the review."""


class _Refused(Exception):
    """GitHub refused the call."""


__all__ = ["API_ROOT", "NO_REVIEW_FOR_REVISION", "GitHubReviewProvider"]
