"""One realistic reviewer/GitHub transcript shared across integration tests.

Shaped from what CodeRabbit actually publishes on this repository, because the
production path reads GitHub rather than a reviewer API: a summary comment on
the issue thread that names the range it covered, inline comments on the pull
request, and the reviewer's own bot login on every one of them.

The head binding is the part that matters. A summary is evidence about the
revision it names, so a transcript carrying two summaries — one for an earlier
head, one for the current — is what proves a review of the previous commit
cannot answer a question about this one.
"""

from __future__ import annotations

from urllib.parse import parse_qs, urlparse

# The reviewer's bot login, exactly as GitHub reports it. The production path
# matches against an allowlist of exact logins, so a transcript that named a
# lookalike would be correctly ignored.
REVIEWER_LOGIN = "coderabbitai[bot]"
OTHER_LOGIN = "alx-1977"

OLD_HEAD = "b" * 40
HEAD = "a" * 40
BASE = "c" * 40

SUMMARY_COMMENT_ID = 7001
STALE_COMMENT_ID = 7000
# Review objects: the round a finding was submitted in.
REVIEW_ID = 5001
STALE_REVIEW_ID = 5000
PUBLISHED_AT = "2026-09-07T06:15:00Z"
STALE_PUBLISHED_AT = "2026-09-07T05:00:00Z"
# The reviewer's own round markers on a revision, as PR #77 recorded them: a
# `pending` status when the round starts, the findings, then `success` seconds
# after the findings are published.
ROUND_STARTED_AT = "2026-09-07T06:12:00Z"
ROUND_ENDED_AT = "2026-09-07T06:15:07Z"
STATUS_CONTEXT = "CodeRabbit"

SUMMARY_BODY = (
    "**Actionable comments posted: 1**\n\n"
    f"Reviewing files that changed from the base of the PR and between {BASE} "
    f"and {HEAD}.\n"
)
STALE_SUMMARY_BODY = (
    "No actionable comments were generated in the recent review.\n\n"
    f"Reviewing files that changed from the base of the PR and between {BASE} "
    f"and {OLD_HEAD}.\n"
)
INLINE_BODY = "This branch is never taken when the store is empty."
STALE_INLINE_BODY = "The earlier revision left this cursor unclosed."


def _user(login: str) -> dict:
    return {"login": login, "type": "Bot" if login.endswith("[bot]") else "User"}


def status(state: str = "success", created_at: str = ROUND_ENDED_AT,
           login: str = REVIEWER_LOGIN, context: str = STATUS_CONTEXT) -> dict:
    """One commit status, as GitHub lists it for a revision."""
    return {
        "context": context,
        "state": state,
        "description": {
            "pending": "Review in progress",
            "success": "Review completed",
        }.get(state, "Review failed"),
        "creator": _user(login),
        "created_at": created_at,
    }


def ended_round() -> list[dict]:
    """A revision whose review round has finished, newest first like GitHub."""
    return [status("success", ROUND_ENDED_AT), status("pending", ROUND_STARTED_AT)]


def running_round() -> list[dict]:
    """A revision the reviewer has started on and not finished."""
    return [status("pending", ROUND_STARTED_AT)]


def statuses_route(url: str, by_revision: dict[str, list[dict]] | None = None):
    """The statuses listed for the revision a statuses URL names, or None.

    Every fake GitHub in the tests answers this through here. Absent a
    mapping, every revision's round has ended, which is what a reviewer that
    has published its review of a revision has also done.
    """
    parsed = urlparse(url)
    parts = parsed.path.rstrip("/").split("/")
    if len(parts) < 3 or parts[-1] != "statuses" or parts[-3] != "commits":
        return None
    if int(parse_qs(parsed.query).get("page", ["1"])[0]) != 1:
        return []
    if by_revision is None:
        return ended_round()
    return list(by_revision.get(parts[-2], []))


def issue_comments() -> list[dict]:
    """The summary thread: two reviewer summaries and one from a person."""
    return [
        {
            "id": STALE_COMMENT_ID,
            "user": _user(REVIEWER_LOGIN),
            "body": STALE_SUMMARY_BODY,
            "created_at": STALE_PUBLISHED_AT,
        },
        {
            "id": 6999,
            "user": _user(OTHER_LOGIN),
            # A person quoting the head must not be read as the reviewer
            # having said anything about it.
            "body": f"please look at {HEAD} again",
            "created_at": STALE_PUBLISHED_AT,
        },
        {
            "id": SUMMARY_COMMENT_ID,
            "user": _user(REVIEWER_LOGIN),
            "body": SUMMARY_BODY,
            "created_at": PUBLISHED_AT,
        },
    ]


def reviews() -> list[dict]:
    """The submitted review objects, one per round.

    A review is submitted against one commit and is never re-pointed, which is
    what makes it the binding for the comments below.
    """
    return [
        {
            "id": STALE_REVIEW_ID,
            "user": _user(REVIEWER_LOGIN),
            "body": "",
            "state": "COMMENTED",
            "commit_id": OLD_HEAD,
            "submitted_at": STALE_PUBLISHED_AT,
        },
        {
            "id": REVIEW_ID,
            "user": _user(REVIEWER_LOGIN),
            "body": "",
            "state": "COMMENTED",
            "commit_id": HEAD,
            "submitted_at": PUBLISHED_AT,
        },
    ]


def inline_comments() -> list[dict]:
    """Inline findings, shaped as GitHub really returns them.

    The stale comment is the important one. It was submitted in the review of
    `OLD_HEAD`, and its `commit_id` has been re-anchored to `HEAD` because the
    line it marks still exists there — exactly what GitHub was observed doing
    on this repository. A fixture that left `commit_id` on the old head would
    never exercise the case the binding exists for.
    """
    return [
        {
            "id": 8001,
            "user": _user(REVIEWER_LOGIN),
            "body": INLINE_BODY,
            "path": "src/alx/goals/store.py",
            "line": 412,
            "commit_id": HEAD,
            "original_commit_id": HEAD,
            "pull_request_review_id": REVIEW_ID,
        },
        {
            # Written about the previous revision, carried onto this one by
            # GitHub. Not a finding about HEAD.
            "id": 8000,
            "user": _user(REVIEWER_LOGIN),
            "body": STALE_INLINE_BODY,
            "path": "src/alx/goals/store.py",
            "line": 88,
            "commit_id": HEAD,
            "original_commit_id": OLD_HEAD,
            "pull_request_review_id": STALE_REVIEW_ID,
        },
        {
            "id": 8002,
            "user": _user(OTHER_LOGIN),
            "body": "noted, thanks",
            "path": "src/alx/goals/store.py",
            "line": 412,
            "commit_id": HEAD,
            "original_commit_id": HEAD,
            "pull_request_review_id": REVIEW_ID,
        },
    ]


def pull_request(head_sha: str = HEAD, number: int = 42) -> dict:
    return {
        "number": number,
        "state": "open",
        "head": {"sha": head_sha, "ref": "fix/thing"},
        "base": {"ref": "main"},
    }


def transport(
    *,
    head_sha: str = HEAD,
    number: int = 42,
    comments: list[dict] | None = None,
    inline: list[dict] | None = None,
    submitted: list[dict] | None = None,
    statuses: dict[str, list[dict]] | None = None,
):
    """A stand-in for GitHub that answers the production path's real calls.

    Written against the paths the provider actually requests, so a change to
    how it reads GitHub shows up here as an unanswered call rather than as a
    silently different result.
    """
    issued = comments if comments is not None else issue_comments()
    lines = inline if inline is not None else inline_comments()
    rounds = submitted if submitted is not None else reviews()
    posted: list[dict] = []

    class Response:
        def __init__(self, payload: object, status: int = 200) -> None:
            self._payload = payload
            self.status_code = status

        def json(self) -> object:
            return self._payload

    def request(method: str, url: str, **keywords) -> Response:
        parsed = urlparse(url)
        path = parsed.path
        page = int(parse_qs(parsed.query).get("page", ["1"])[0])
        if method == "POST" and path.endswith(f"/issues/{number}/comments"):
            posted.append(keywords.get("json") or {})
            return Response({"id": 9001})
        if method == "GET" and path.endswith(f"/pulls/{number}"):
            return Response(pull_request(head_sha, number))
        if method == "GET" and path.endswith(f"/issues/{number}/comments"):
            return Response(issued if page == 1 else [])
        if method == "GET" and path.endswith(f"/pulls/{number}/comments"):
            return Response(lines if page == 1 else [])
        if method == "GET" and path.endswith(f"/pulls/{number}/reviews"):
            return Response(rounds if page == 1 else [])
        listed = statuses_route(url, statuses) if method == "GET" else None
        if listed is not None:
            return Response(listed)
        raise AssertionError(f"unexpected call: {method} {url}")

    request.posted = posted  # type: ignore[attr-defined]
    return request


__all__ = [
    "BASE",
    "HEAD",
    "INLINE_BODY",
    "OLD_HEAD",
    "REVIEW_ID",
    "STALE_INLINE_BODY",
    "STALE_REVIEW_ID",
    "OTHER_LOGIN",
    "PUBLISHED_AT",
    "REVIEWER_LOGIN",
    "STALE_SUMMARY_BODY",
    "SUMMARY_BODY",
    "SUMMARY_COMMENT_ID",
    "ended_round",
    "inline_comments",
    "issue_comments",
    "reviews",
    "pull_request",
    "running_round",
    "status",
    "statuses_route",
    "transport",
]
