"""Read what Qodo published about one exact pull request revision.

This is the other half of the review path. The watcher establishes that a
result now exists for a revision; this fetches what that result says, so AL/X
can read the findings herself and decide. Until it existed the only route to a
review's wording was a notification email, and on 2026-09-06 that is exactly
what happened: the completion event carried no findings, and the conclusion
came from an email that nothing had declared a dependency on.

Read-only, and structurally so. It issues GET requests and nothing else: it
cannot request a review, cannot merge, and imports nothing that could. A test
asserts that rather than trusting it.

Retrieval is bound to one commit. Qodo publishes a review object against the
commit it examined, so a review is matched by `commit_id` and by author, and a
review of a different revision is never returned for this one. When no such
review exists the answer says so plainly: an empty result is a fact about what
was found, and reporting a nearby review instead would let advice about other
code look like approval of this code.

Nothing here judges. There is no clean/unclean decision, no severity ranking
and no merge opinion: the reviewer's words are carried as they were written,
and what they mean is AL/X's to say under D-026.
"""

from __future__ import annotations

import re
from datetime import UTC, datetime

import httpx

from alx.contracts.review_content import (
    ReviewComment,
    ReviewContent,
    ReviewContentRequest,
    ReviewReadError,
)


API_ROOT = "https://api.github.com"

# The whole request, not one socket operation.
TIMEOUT_SECONDS = 30.0

# Enough for any realistic pull request; a bound so one read cannot run on.
_MAX_PAGES = 10

# Qodo's account, which is what makes a review its output rather than someone
# else's. The same identity the watcher matches on.
REVIEWER_ID = 151058649

REVIEWER = "qodo"

# One path segment: no slashes, spaces, traversal or query characters. The same
# rule as every other provider that builds a repository URL.
_SEGMENT = re.compile(r"^[A-Za-z0-9._-]+$")

# Said when the reviewer has published nothing for this exact commit.
NO_REVIEW_FOR_REVISION = "no_review_for_revision"


def _moment(value: object) -> datetime | None:
    """A GitHub timestamp, or None when it cannot be read."""
    if not isinstance(value, str):
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


class QodoReviewContentProvider:
    """Reads one Qodo review of one pull request at one exact revision."""

    def __init__(self, repository: str, token: str, api_root: str = API_ROOT) -> None:
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
            "User-Agent": "alx-review-content",
        }

    def _get(self, url: str) -> object | None:
        try:
            response = httpx.get(
                url, headers=self._headers(), timeout=TIMEOUT_SECONDS
            )
            if response.status_code != 200:
                return None
            return response.json()
        except (httpx.HTTPError, ValueError):
            # Severed from the original so the token cannot travel on it.
            return None

    def _pages(self, url: str) -> list | None:
        """Every page of a listing, or None when it cannot be read."""
        items: list = []
        for page in range(1, _MAX_PAGES + 1):
            batch = self._get(f"{url}?per_page=100&page={page}")
            if not isinstance(batch, list):
                return None
            items.extend(batch)
            if len(batch) < 100:
                break
        return items

    def read(self, request: ReviewContentRequest) -> ReviewContent:
        """What the reviewer said about this exact revision, or that it said nothing."""
        number = request.pull_request_number
        sha = request.head_sha
        now = datetime.now(UTC)

        reviews = self._pages(
            f"{self._api_root}/repos/{self._repository}/pulls/{number}/reviews"
        )
        if reviews is None:
            # Could not be read, which is not the same as "there is none".
            raise ReviewReadError("review_unavailable")

        # Every review this reviewer published for this exact commit. Qodo can
        # post more than one, so the latest is taken rather than the first
        # encountered; ordering by submission keeps that honest when GitHub's
        # own order changes.
        matching = [
            review
            for review in reviews
            if isinstance(review, dict)
            and (review.get("user") or {}).get("id") == REVIEWER_ID
            and review.get("commit_id") == sha
        ]
        if not matching:
            # No review of this revision. Said plainly: a review of a different
            # commit is not evidence about this one, and returning one would
            # let advice about other code read as approval of this code.
            return ReviewContent(
                pull_request_number=number,
                head_sha=sha,
                reviewer=REVIEWER,
                available=False,
                retrieved_at=now,
                unavailable_reason=NO_REVIEW_FOR_REVISION,
            )

        matching.sort(key=lambda item: str(item.get("submitted_at") or ""))
        review = matching[-1]
        review_id = review.get("id")

        # The line comments belonging to that review, where they can be read.
        # Their absence is not an error: a review may have only a summary, and
        # failing the whole read because the comments call did not answer would
        # discard findings that were already retrieved.
        comments: list[ReviewComment] = []
        if isinstance(review_id, int):
            listed = self._pages(
                f"{self._api_root}/repos/{self._repository}"
                f"/pulls/{number}/reviews/{review_id}/comments"
            )
            for item in listed or ():
                if not isinstance(item, dict):
                    continue
                body = item.get("body")
                if not isinstance(body, str) or not body.strip():
                    continue
                line = item.get("line")
                comments.append(
                    ReviewComment(
                        body=body,
                        path=item.get("path") if isinstance(item.get("path"), str) else "",
                        line=line if isinstance(line, int) and not isinstance(line, bool) else None,
                    )
                )

        body = review.get("body")
        submitted = _moment(review.get("submitted_at"))
        return ReviewContent(
            pull_request_number=number,
            head_sha=sha,
            reviewer=REVIEWER,
            available=True,
            summary=body if isinstance(body, str) else "",
            comments=tuple(comments),
            # A review that was found but cannot be dated still happened. The
            # retrieval time stands in so the record is never undated, and the
            # contract keeps `available` honest.
            submitted_at=submitted or now,
            retrieved_at=now,
        )
