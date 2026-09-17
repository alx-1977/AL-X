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

# The reviewer's bot login, as GitHub reports it. The production path matches
# on the login prefix rather than a numeric id, so a transcript only has to be
# honest about the name.
REVIEWER_LOGIN = "coderabbitai[bot]"
OTHER_LOGIN = "alx-1977"

OLD_HEAD = "b" * 40
HEAD = "a" * 40
BASE = "c" * 40

SUMMARY_COMMENT_ID = 7001
STALE_COMMENT_ID = 7000
PUBLISHED_AT = "2026-09-07T06:15:00Z"
STALE_PUBLISHED_AT = "2026-09-07T05:00:00Z"

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


def _user(login: str) -> dict:
    return {"login": login, "type": "Bot" if login.endswith("[bot]") else "User"}


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


def inline_comments() -> list[dict]:
    return [
        {
            "id": 8001,
            "user": _user(REVIEWER_LOGIN),
            "body": INLINE_BODY,
            "path": "src/alx/goals/store.py",
            "line": 412,
        },
        {
            "id": 8002,
            "user": _user(OTHER_LOGIN),
            "body": "noted, thanks",
            "path": "src/alx/goals/store.py",
            "line": 412,
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
):
    """A stand-in for GitHub that answers the production path's real calls.

    Written against the paths the provider actually requests, so a change to
    how it reads GitHub shows up here as an unanswered call rather than as a
    silently different result.
    """
    issued = comments if comments is not None else issue_comments()
    lines = inline if inline is not None else inline_comments()
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
            return Response([])
        raise AssertionError(f"unexpected call: {method} {url}")

    request.posted = posted  # type: ignore[attr-defined]
    return request


__all__ = [
    "BASE",
    "HEAD",
    "INLINE_BODY",
    "OLD_HEAD",
    "OTHER_LOGIN",
    "PUBLISHED_AT",
    "REVIEWER_LOGIN",
    "STALE_SUMMARY_BODY",
    "SUMMARY_BODY",
    "SUMMARY_COMMENT_ID",
    "inline_comments",
    "issue_comments",
    "pull_request",
    "transport",
]
