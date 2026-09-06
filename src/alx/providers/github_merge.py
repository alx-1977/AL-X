"""The one call that merges a reviewed pull request on GitHub.

GitHub is execution plumbing here, not the decision-maker. AL/X has already
read the external review and judged the change mergeable; this performs that
decision against one exact revision.

`sha` is what makes the execution safe. GitHub merges only if the pull
request's head still equals it, and answers 409 otherwise. So an authorisation
made about one revision cannot merge a different one, however much later it is
executed. That is the whole stale-head protection, and it lives on GitHub's
side precisely so nothing here has to track branch state.

Branch protection still applies. A refusal from it is reported as the fact it
is; nothing here retries, escalates or works around it.
"""

from __future__ import annotations

import httpx

from alx.contracts.repository import (
    MERGE_METHOD,
    MergeError,
    MergeOutcome,
    MergeRequest,
)


API_ROOT = "https://api.github.com"

# The whole request, not one socket operation.
TIMEOUT_SECONDS = 30.0


class GitHubMergeProvider:
    """Merges one pull request at one exact head, or reports why it could not."""

    def __init__(self, repository: str, token: str, api_root: str = API_ROOT) -> None:
        if not repository.strip() or "/" not in repository:
            raise ValueError("repository must be owner/name")
        if not token.strip():
            raise ValueError("a GitHub token is required")
        self._repository = repository
        self._token = token
        self._api_root = api_root.rstrip("/")

    def merge(self, request: MergeRequest) -> MergeOutcome:
        url = (
            f"{self._api_root}/repos/{self._repository}"
            f"/pulls/{request.pull_request_number}/merge"
        )
        payload: dict[str, object] = {
            "merge_method": MERGE_METHOD,
            # The exact revision AL/X authorised. GitHub compares this against
            # the live head and refuses if they differ.
            "sha": request.head_sha,
        }
        if request.title:
            payload["commit_title"] = request.title
        if request.message:
            payload["commit_message"] = request.message

        try:
            response = httpx.put(
                url,
                json=payload,
                headers={
                    "Authorization": f"Bearer {self._token}",
                    "Accept": "application/vnd.github+json",
                    "X-GitHub-Api-Version": "2022-11-28",
                    "User-Agent": "alx-merge",
                },
                timeout=TIMEOUT_SECONDS,
            )
        except httpx.HTTPError:
            # Severed from the original so the token cannot travel on it.
            raise MergeError("merge_unavailable") from None

        if response.status_code == 409:
            # GitHub's answer when the head no longer matches `sha`, and also
            # when the branch cannot be merged. Both mean the same thing here:
            # what AL/X judged is not what is on the branch now.
            raise MergeError("head_changed") from None
        if response.status_code in (403, 405, 422):
            # Branch protection, an unmergeable state, or a rejected request.
            raise MergeError("merge_refused") from None
        if response.status_code != 200:
            raise MergeError("merge_unavailable") from None

        try:
            body = response.json()
        except ValueError:
            raise MergeError("merge_unavailable") from None
        if not isinstance(body, dict) or body.get("merged") is not True:
            raise MergeError("merge_refused") from None
        commit = body.get("sha")
        if not isinstance(commit, str):
            raise MergeError("merge_unavailable") from None

        return MergeOutcome(
            pull_request_number=request.pull_request_number,
            head_sha=request.head_sha,
            merged=True,
            merge_commit_sha=commit,
        )
