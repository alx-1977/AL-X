"""Ask Qodo to review a pull request, through its GitHub integration.

Qodo is installed on the repository and watches pull requests, so a review is
requested by leaving its trigger comment on the pull request. Nothing here
retrieves, packages or uploads a diff: the reviewer already has the code.

The pull request is fetched to learn which commit it points at, and fetched
again after the trigger is posted. The trigger is a comment on the pull
request rather than a request pinned to a commit, so the reviewer examines
whatever is current when it gets to the work. Reporting the revision read
beforehand would name a commit the reviewer may not have looked at.

So the revision is confirmed rather than assumed: if the head moved between
the two reads, the outcome says the revision is unknown instead of naming one.
AL/X can then see that the request went out but that what was reviewed is not
established, which is a true and useful thing to know, and act on it. Claiming
a revision that was merely current a moment earlier would be a false record.

This provider requests a review and reports that it did. It never reads a
review, waits for one, or asks again.
"""

from __future__ import annotations

import re
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime

import httpx

from alx.contracts.review import (
    ReviewError,
    ReviewOutcome,
    ReviewRequest,
    valid_sha,
)


API_ROOT = "https://api.github.com"

# The whole request, not one socket operation.
TIMEOUT_SECONDS = 30.0

# Qodo's documented manual trigger, left as a pull request comment. This is the
# provider-specific detail; everything above it is the same for any reviewer
# that watches a repository.
TRIGGER = "/review"

REVIEWER = "qodo"

# One path segment: no slashes, spaces, traversal or query characters.
_SEGMENT = re.compile(r"^[A-Za-z0-9._-]+$")


class QodoReviewProvider:
    """Requests one Qodo review of one pull request at one exact revision."""

    def __init__(self, repository: str, token: str, api_root: str = API_ROOT) -> None:
        # Exactly one owner and one name, each a legal path segment. A value
        # with an embedded space or a query character passes a non-blank check,
        # registers the capability, and then makes every URL malformed. Same
        # rule as the merge provider, for the same reason.
        parts = repository.strip().split("/")
        if len(parts) != 2 or not all(_SEGMENT.match(part) for part in parts):
            raise ValueError("repository must be owner/name")
        if not token.strip():
            raise ValueError("a GitHub token is required")
        self._repository = repository.strip()
        self._token = token
        self._api_root = api_root.rstrip("/")

    @property
    def reviewer(self) -> str:
        return REVIEWER

    def _headers(self) -> dict[str, str]:
        return {
            "Authorization": f"Bearer {self._token}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
            "User-Agent": "alx-review",
        }

    def _head(self, number: int, strict: bool) -> str:
        """The commit the pull request points at, or "" when unknowable.

        `strict` distinguishes the two reads. Before the trigger, a head that
        cannot be read means there is nothing to spend a review on, so it
        raises. Afterwards the review has already been requested, and failing
        to confirm is a fact to report rather than a reason to fail: the caller
        records the revision as unknown instead.
        """
        url = (
            f"{self._api_root}/repos/{self._repository}/pulls/{number}"
        )
        try:
            response = httpx.get(
                url, headers=self._headers(), timeout=TIMEOUT_SECONDS
            )
            # Severed from the original so the token cannot travel on it.
            if response.status_code != 200:
                raise ValueError
            body = response.json()
        except (httpx.HTTPError, ValueError):
            if strict:
                raise ReviewError("review_unavailable") from None
            return ""
        if not isinstance(body, dict):
            if strict:
                raise ReviewError("review_unavailable") from None
            return ""
        head = (body.get("head") or {})
        head = head.get("sha") if isinstance(head, dict) else None
        if not isinstance(head, str) or not valid_sha(head):
            if strict:
                # Without a revision there is nothing to record about what was
                # reviewed, and a request whose subject cannot be named is not
                # worth spending.
                raise ReviewError("review_unavailable") from None
            return ""
        return head

    def request(self, review: ReviewRequest) -> ReviewOutcome:
        # Read the revision the pull request currently points at. Friedl asks
        # for a pull request to be reviewed; which commit that is now is a fact
        # to look up, not something he should have to carry.
        head = self._head(review.pull_request_number, strict=True)
        local_requested_at = datetime.now(UTC)

        comments = (
            f"{self._api_root}/repos/{self._repository}"
            f"/issues/{review.pull_request_number}/comments"
        )
        try:
            posted = httpx.post(
                comments,
                json={"body": TRIGGER},
                headers=self._headers(),
                timeout=TIMEOUT_SECONDS,
            )
        except httpx.HTTPError:
            raise ReviewError("review_unavailable") from None
        headers = getattr(posted, "headers", {})
        throttled = (
            posted.status_code == 403
            and (
                "Retry-After" in headers
                or headers.get("X-RateLimit-Remaining") == "0"
            )
        )
        if (
            throttled
            or posted.status_code in (408, 429)
            or 500 <= posted.status_code < 600
        ):
            # Throttling and server errors are availability, not refusal. The
            # right response to "try later" is not the response to "no".
            raise ReviewError("review_unavailable") from None
        if posted.status_code != 201:
            raise ReviewError("review_refused") from None

        try:
            posted_body = posted.json()
        except ValueError:
            posted_body = {}
        requested_at = None
        if isinstance(posted_body, dict):
            value = posted_body.get("created_at")
            if isinstance(value, str):
                try:
                    requested_at = datetime.fromisoformat(value.replace("Z", "+00:00"))
                except ValueError:
                    requested_at = None
        if requested_at is None:
            try:
                requested_at = parsedate_to_datetime(headers.get("Date", ""))
            except (TypeError, ValueError):
                requested_at = local_requested_at
        if requested_at.tzinfo is None or requested_at.utcoffset() is None:
            requested_at = local_requested_at

        # Confirm the revision after the trigger. The reviewer works from
        # whatever the pull request points at when it reaches the request, so
        # a head that moved in between means the revision reviewed is not the
        # one read beforehand, and must not be reported as though it were.
        confirmed = self._head(review.pull_request_number, strict=False)
        return ReviewOutcome(
            pull_request_number=review.pull_request_number,
            head_sha=head if confirmed == head else "",
            requested=True,
            reviewer=REVIEWER,
            requested_at=requested_at,
        )
