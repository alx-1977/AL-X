"""Ask Qodo to review a pull request, through its GitHub integration.

Qodo is installed on the repository and watches pull requests, so a review is
requested by leaving its trigger comment on the pull request. Nothing here
retrieves, packages or uploads a diff: the reviewer already has the code.

The head is verified first. If the pull request no longer points at the
revision AL/X named, the request is refused before Qodo is contacted, so a
review is never spent on a revision nobody asked about.

This provider requests a review and reports that it did. It never reads a
review, waits for one, or asks again.
"""

from __future__ import annotations

import httpx

from alx.contracts.review import ReviewError, ReviewOutcome, ReviewRequest


API_ROOT = "https://api.github.com"

# The whole request, not one socket operation.
TIMEOUT_SECONDS = 30.0

# Qodo's documented manual trigger, left as a pull request comment. This is the
# provider-specific detail; everything above it is the same for any reviewer
# that watches a repository.
TRIGGER = "/review"

REVIEWER = "qodo"


class QodoReviewProvider:
    """Requests one Qodo review of one pull request at one exact revision."""

    def __init__(self, repository: str, token: str, api_root: str = API_ROOT) -> None:
        parts = repository.strip().split("/")
        if len(parts) != 2 or not all(part.strip() for part in parts):
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

    def request(self, review: ReviewRequest) -> ReviewOutcome:
        # Check the revision before spending a review on it.
        pull_request = (
            f"{self._api_root}/repos/{self._repository}"
            f"/pulls/{review.pull_request_number}"
        )
        try:
            response = httpx.get(
                pull_request, headers=self._headers(), timeout=TIMEOUT_SECONDS
            )
        except httpx.HTTPError:
            # Severed from the original so the token cannot travel on it.
            raise ReviewError("review_unavailable") from None
        if response.status_code != 200:
            raise ReviewError("review_unavailable") from None
        try:
            body = response.json()
        except ValueError:
            raise ReviewError("review_unavailable") from None
        head = ((body or {}).get("head") or {}).get("sha")
        if not isinstance(head, str):
            raise ReviewError("review_unavailable") from None
        if head != review.head_sha:
            # The branch moved after Friedl asked. Refusing here is the point:
            # a review of a revision nobody named costs the same as one of the
            # revision they did.
            raise ReviewError("head_changed") from None

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
        if posted.status_code != 201:
            raise ReviewError("review_refused") from None

        return ReviewOutcome(
            pull_request_number=review.pull_request_number,
            head_sha=review.head_sha,
            requested=True,
            reviewer=REVIEWER,
        )
