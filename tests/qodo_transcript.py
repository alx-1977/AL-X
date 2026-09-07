"""One realistic Qodo/GitHub transcript shared across integration tests."""

from __future__ import annotations

from urllib.parse import parse_qs, urlparse

from alx.providers.qodo_artifact import REVIEWER_ID

HEAD = "a" * 40
PERSISTENT_COMMENT_ID = 7001
MARKER_COMMENT_ID = 7002
PUBLISHED_AT = "2026-09-07T06:15:00Z"
SUMMARY = "## Qodo review\n\nA blocking handoff defect remains."


def realistic_issue_comments(sha: str = HEAD) -> list[dict]:
    return [
        {
            "id": PERSISTENT_COMMENT_ID,
            "user": {"id": REVIEWER_ID},
            "body": SUMMARY,
            "created_at": "2026-09-07T06:10:00Z",
            "updated_at": PUBLISHED_AT,
        },
        {
            "id": MARKER_COMMENT_ID,
            "user": {"id": REVIEWER_ID},
            "body": (
                "[Code review](https://github.com/owner/repo/pull/21"
                f"#issuecomment-{PERSISTENT_COMMENT_ID}) by qodo was updated up "
                "to the latest commit "
                f"https://github.com/owner/repo/commit/{sha}"
            ),
            "created_at": PUBLISHED_AT,
            "updated_at": PUBLISHED_AT,
        },
    ]


class Response:
    def __init__(self, payload, status_code: int = 200) -> None:
        self.status_code = status_code
        self._payload = payload

    def json(self):
        return self._payload


class GitHubTranscript:
    def __init__(
        self,
        *,
        issue_comments: list[dict] | None = None,
        reviews: list[dict] | None = None,
        review_comments: dict[int, list[dict]] | None = None,
    ) -> None:
        self.issue_comments = (
            realistic_issue_comments() if issue_comments is None else issue_comments
        )
        self.reviews = reviews or []
        self.review_comments = review_comments or {}
        self.requested: list[str] = []

    @staticmethod
    def _page(items: list[dict], page: int, size: int) -> list[dict]:
        start = (page - 1) * size
        return items[start : start + size]

    def get(self, url: str, **_kwargs) -> Response:
        self.requested.append(url)
        parsed = urlparse(url)
        query = parse_qs(parsed.query)
        page = int(query.get("page", ["1"])[0])
        size = int(query.get("per_page", ["100"])[0])
        if parsed.path.endswith("/issues/21/comments"):
            return Response(self._page(self.issue_comments, page, size))
        if parsed.path.endswith("/pulls/21/reviews"):
            return Response(self._page(self.reviews, page, size))
        if "/reviews/" in parsed.path and parsed.path.endswith("/comments"):
            review_id = int(parsed.path.rsplit("/reviews/", 1)[1].split("/", 1)[0])
            return Response(self._page(self.review_comments.get(review_id, []), page, size))
        return Response([], 404)
