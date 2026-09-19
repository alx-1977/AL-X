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

from alx.providers.github_http import unavailable
from alx.contracts.github_pull_request import (
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
        # The same reading as the review path, shared rather than restated: a
        # rate limit arrives as 403 with a header, and reading it as a refusal
        # would turn "try later" into a failed capability.
        if unavailable(response):
            raise PullRequestError("pull_request_unavailable")
        if response.status_code >= 400:
            raise PullRequestError("pull_request_refused")
        try:
            return response.json()
        except ValueError as error:
            raise PullRequestError("pull_request_unavailable") from error

    def _existing(self, branch: str) -> object | None:
        """The open pull request for this branch into the fixed base.

        The base is part of the identity, not a detail. Filtering on head alone
        returned any open pull request from this branch, including one into
        some other base — so `open_pull_request` could hand back a proposal
        that no gate runs on and no reviewer watches, reported as though the
        work had been proposed for review.

        GitHub's `base` filter is asked for, and the answer is checked again
        here: a filter is a request, and what the identity rests on should not
        depend on the server having honoured it.
        """
        owner = self._repository.split("/")[0]
        found = self._request(
            "GET",
            f"/repos/{self._repository}/pulls"
            f"?state=open&head={owner}:{branch}&base={BASE}&per_page=10",
        )
        if not isinstance(found, list):
            return None
        for item in found:
            if not isinstance(item, dict):
                continue
            head = item.get("head")
            base = item.get("base")
            if not isinstance(head, dict) or not isinstance(base, dict):
                continue
            if base.get("ref") != BASE:
                continue
            if head.get("ref") != branch:
                continue
            if item.get("state") != "open":
                continue
            return item
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

    # ---- the rest of ordinary pull-request work -------------------------

    def update(self, number: int, title: str = "", body: str = "") -> PullRequestOutcome:
        """Change the title or body of a pull request that is already open.

        Opening one is not the end of the work: a proposal is revised as the
        work is, and rewriting the description used to mean asking Friedl to
        do it. Nothing here touches the head, the base or the state — those are
        decided by pushing and by merging, not by editing text.
        """
        if not isinstance(number, int) or isinstance(number, bool) or number <= 0:
            raise PullRequestError("pull_request_refused")
        payload: dict[str, str] = {}
        if title.strip():
            payload["title"] = title.strip()
        if body.strip():
            payload["body"] = body
        if not payload:
            raise PullRequestError("pull_request_refused")
        data = self._request(
            "PATCH", f"/repos/{self._repository}/pulls/{number}", payload
        )
        if not isinstance(data, dict):
            raise PullRequestError("pull_request_unavailable")
        head = data.get("head")
        branch = head.get("ref") if isinstance(head, dict) else None
        # A blank branch would reach `PullRequestOutcome` and raise ValueError
        # rather than the declared failure, so an incomplete answer is reported
        # as one.
        if not isinstance(branch, str) or not branch.strip():
            raise PullRequestError("pull_request_unavailable")
        return self._outcome(data, branch, created=False)

    def find(self, branch: str) -> PullRequestOutcome | None:
        """The open pull request for this branch, or None.

        The same head-and-base identity `open` uses, exposed because knowing
        whether a proposal already exists is an ordinary question and used to
        require opening one to find out.
        """
        existing = self._existing(branch)
        if existing is None:
            return None
        return self._outcome(existing, branch, created=False)

    def review_threads(self, number: int) -> tuple[dict, ...]:
        """Unresolved review threads on this pull request.

        Read through the GraphQL endpoint because REST does not expose thread
        resolution state at all. What comes back is the reviewer's, so it is
        returned as data for AL/X to read rather than judged here.
        """
        query = (
            "query($owner:String!,$name:String!,$number:Int!){"
            "repository(owner:$owner,name:$name){pullRequest(number:$number){"
            "reviewThreads(first:100){nodes{id isResolved isOutdated path line "
            "comments(first:1){nodes{author{login} body}}}}}}}"
        )
        owner, _, name = self._repository.partition("/")
        data = self._request("POST", "/graphql", {
            "query": query,
            "variables": {"owner": owner, "name": name, "number": number},
        })
        # An empty tuple means the pull request has no unresolved threads, and
        # that is a fact a caller may act on — branch protection can require
        # every thread resolved before a merge. A response that could not be
        # read is a different thing entirely, and returning `()` for both would
        # let "I could not see the threads" be taken as "there are none".
        if not isinstance(data, dict):
            raise PullRequestError("pull_request_unavailable")
        try:
            nodes = data["data"]["repository"]["pullRequest"]["reviewThreads"]["nodes"]
        except (KeyError, TypeError) as error:
            raise PullRequestError("pull_request_unavailable") from error
        # `"nodes": null` parses fine and then fails on iteration, outside the
        # guard above.
        if not isinstance(nodes, list):
            raise PullRequestError("pull_request_unavailable")
        # Only nodes carrying the identity a caller needs to resolve them. A
        # node without an `id` cannot be acted on, and counting it would make
        # "threads remain" true for something nobody can address.
        return tuple(
            item for item in nodes
            if isinstance(item, dict) and isinstance(item.get("id"), str)
            and item["id"].strip()
        )

    def resolve_review_thread(self, thread_id: str) -> bool:
        """Mark one review thread resolved.

        Branch protection can require every thread resolved before a merge, so
        without this a pull request AL/X has genuinely addressed cannot be
        merged by her at all. Resolving is a statement that she has dealt with
        the comment; what counts as dealing with it is her judgement.
        """
        if not isinstance(thread_id, str) or not thread_id.strip():
            raise PullRequestError("pull_request_refused")
        data = self._request("POST", "/graphql", {
            "query": (
                "mutation($id:ID!){resolveReviewThread(input:{threadId:$id})"
                "{thread{isResolved}}}"
            ),
            "variables": {"id": thread_id.strip()},
        })
        if not isinstance(data, dict):
            return False
        try:
            return bool(data["data"]["resolveReviewThread"]["thread"]["isResolved"])
        except (KeyError, TypeError):
            return False

    def comment(self, number: int, body: str) -> bool:
        """Leave one comment on the pull request thread."""
        if not isinstance(number, int) or isinstance(number, bool) or number <= 0:
            raise PullRequestError("pull_request_refused")
        if not body.strip():
            raise PullRequestError("pull_request_refused")
        data = self._request(
            "POST", f"/repos/{self._repository}/issues/{number}/comments",
            {"body": body},
        )
        return isinstance(data, dict)


__all__ = ["API_ROOT", "BASE", "GitHubPullRequests"]
