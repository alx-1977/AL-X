"""Open one pull request for a published repair branch, or find the open one.

A pull request is where everything else in the loop happens: law gates run on
it, every configured reviewer watches it, and the merge capability acts on the
revision it points at. Until one exists, a published branch is invisible to all
of them.

Opening is not merging. Nothing here approves, merges, or reads a review, and
the base is fixed rather than accepted: a pull request against some other
branch would be a review nobody performs and gates nobody runs.

Reuse rather than duplication. A branch that already has an open pull request
gets that one back, because a second pull request for the same work splits the
review across two places and leaves a clean review attached to something nobody
merges. `created` says which happened, so AL/X can tell a new proposal from a
branch she had already published.
"""

from __future__ import annotations

import logging
import re

import httpx

from alx.contracts.publication import (
    PullRequestError,
    PullRequestOutcome,
    PullRequestRequest,
    valid_sha,
)

LOGGER = logging.getLogger(__name__)

API_ROOT = "https://api.github.com"

# The one base a repair may be proposed into.
BASE = "main"

# One path segment: no slashes, traversal or query characters.
_SEGMENT = re.compile(r"^[A-Za-z0-9._-]+$")

# The whole request, not one socket operation.
TIMEOUT_SECONDS = 30.0


class GitHubPullRequests:
    """Open and find pull requests for one repository."""

    def __init__(self, repository: str, token: str, api_root: str = API_ROOT) -> None:
        owner, _, name = repository.strip().partition("/")
        if not _SEGMENT.match(owner) or not _SEGMENT.match(name):
            raise ValueError("repository must be exactly owner/name")
        if not token.strip():
            raise ValueError("a GitHub token is required")
        self._repository = f"{owner}/{name}"
        self._token = token.strip()
        self._api_root = api_root.rstrip("/")

    @property
    def base(self) -> str:
        return BASE

    def _headers(self) -> dict[str, str]:
        return {
            "Authorization": f"Bearer {self._token}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
        }

    def _request(self, method: str, path: str, payload: object = None) -> object:
        try:
            response = httpx.request(
                method,
                f"{self._api_root}{path}",
                headers=self._headers(),
                json=payload,
                timeout=TIMEOUT_SECONDS,
            )
        except httpx.HTTPError as error:
            # The type, never the message: a transport exception's wording can
            # carry a URL with a token in it.
            LOGGER.warning("GitHub request failed: %s", type(error).__name__)
            raise PullRequestError("pull_request_unavailable") from error
        if response.status_code >= 500 or response.status_code == 429:
            raise PullRequestError("pull_request_unavailable")
        if response.status_code >= 400:
            raise PullRequestError("pull_request_refused")
        try:
            return response.json()
        except ValueError as error:
            raise PullRequestError("pull_request_unavailable") from error

    def _existing(self, branch: str) -> object | None:
        """The open pull request for this branch, if GitHub already has one."""
        owner = self._repository.split("/")[0]
        found = self._request(
            "GET",
            f"/repos/{self._repository}/pulls"
            f"?state=open&head={owner}:{branch}&per_page=1",
        )
        if isinstance(found, list) and found and isinstance(found[0], dict):
            return found[0]
        return None

    @staticmethod
    def _outcome(data: object, branch: str, created: bool) -> PullRequestOutcome:
        if not isinstance(data, dict):
            raise PullRequestError("pull_request_unavailable")
        number = data.get("number")
        head = data.get("head")
        base = data.get("base")
        state = data.get("state")
        if not isinstance(number, int) or not isinstance(head, dict):
            raise PullRequestError("pull_request_unavailable")
        head_sha = head.get("sha")
        if not valid_sha(head_sha):
            raise PullRequestError("pull_request_unavailable")
        base_ref = base.get("ref") if isinstance(base, dict) else None
        return PullRequestOutcome(
            pull_request_number=number,
            branch=branch,
            head_sha=head_sha,
            base=base_ref if isinstance(base_ref, str) and base_ref else BASE,
            state=state if isinstance(state, str) and state else "open",
            created=created,
        )

    def open(self, request: PullRequestRequest) -> PullRequestOutcome:
        """Open a pull request for a published branch, or return the open one."""
        existing = self._existing(request.branch)
        if existing is not None:
            return self._outcome(existing, request.branch, created=False)

        created = self._request(
            "POST",
            f"/repos/{self._repository}/pulls",
            {
                "title": request.title,
                "body": request.body,
                "head": request.branch,
                # Fixed, never taken from the caller.
                "base": BASE,
            },
        )
        return self._outcome(created, request.branch, created=True)


__all__ = ["API_ROOT", "BASE", "GitHubPullRequests"]
