"""Read one Qodo result through the GitHub artifacts Qodo actually publishes."""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime
from typing import Any
from urllib.parse import urlparse

import httpx

from alx.contracts.review_content import ReviewComment

API_ROOT = "https://api.github.com"
TIMEOUT_SECONDS = 30.0
MAX_PAGES = 10
PAGE_SIZE = 100
REVIEWER_ID = 151058649
_SEGMENT = re.compile(r"^[A-Za-z0-9._-]+$")
_SHA = re.compile(r"^[0-9a-f]{40}$")


class QodoArtifactUnavailable(Exception):
    """GitHub could not establish whether a readable result exists."""


@dataclass(frozen=True, slots=True)
class QodoArtifact:
    """Reviewer-authored content tied to one exact commit."""

    head_sha: str
    summary: str
    comments: tuple[ReviewComment, ...]
    submitted_at: datetime


def moment(value: object) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo is not None and parsed.utcoffset() is not None else None


class QodoArtifactReader:
    """Load the same complete artifact for observation and content reading."""

    def __init__(self, repository: str, token: str, api_root: str = API_ROOT) -> None:
        parts = repository.strip().split("/")
        if len(parts) != 2 or not all(_SEGMENT.match(part) for part in parts):
            raise ValueError("repository must be owner/name")
        if not token.strip():
            raise ValueError("a GitHub token is required")
        self._repository = repository.strip()
        self._token = token
        self._api_root = api_root.rstrip("/")
        self._owner, self._name = self._repository.split("/")

    def _headers(self) -> dict[str, str]:
        return {
            "Authorization": f"Bearer {self._token}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
            "User-Agent": "alx-qodo-artifact",
        }

    def _get(self, url: str) -> object:
        try:
            response = httpx.get(url, headers=self._headers(), timeout=TIMEOUT_SECONDS)
            if response.status_code != 200:
                raise ValueError
            return response.json()
        except (httpx.HTTPError, ValueError):
            raise QodoArtifactUnavailable from None

    def _pages(self, url: str) -> list[Any]:
        items: list[Any] = []
        for page in range(1, MAX_PAGES + 1):
            batch = self._get(f"{url}?per_page={PAGE_SIZE}&page={page}")
            if not isinstance(batch, list):
                raise QodoArtifactUnavailable
            items.extend(batch)
            if len(batch) < PAGE_SIZE:
                return items
        raise QodoArtifactUnavailable

    @staticmethod
    def _qodo(item: object) -> bool:
        return (
            isinstance(item, dict)
            and isinstance(item.get("user"), dict)
            and item["user"].get("id") == REVIEWER_ID
        )

    @staticmethod
    def _comments(items: list[Any]) -> tuple[ReviewComment, ...]:
        comments: list[ReviewComment] = []
        for item in items:
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
        return tuple(comments)

    @staticmethod
    def _urls(body: str) -> tuple[str, ...]:
        prefix = "https://github.com/"
        urls: list[str] = []
        cursor = 0
        while True:
            start = body.find(prefix, cursor)
            if start < 0:
                return tuple(urls)
            end = start
            while end < len(body) and not body[end].isspace() and body[end] not in ")]<>\"'":
                end += 1
            urls.append(body[start:end].rstrip(".,;:"))
            cursor = max(end, start + len(prefix))

    def _completion_marker(self, body: str, number: int) -> tuple[int, str] | None:
        comment_ids: set[int] = set()
        revisions: set[str] = set()
        for value in self._urls(body):
            parsed = urlparse(value)
            if parsed.scheme != "https" or parsed.netloc != "github.com":
                continue
            parts = parsed.path.strip("/").split("/")
            if parts[:2] != [self._owner, self._name]:
                continue
            if (
                len(parts) == 4
                and parts[2] == "pull"
                and parts[3] == str(number)
                and parsed.fragment.startswith("issuecomment-")
                and parsed.fragment.removeprefix("issuecomment-").isdigit()
            ):
                comment_ids.add(int(parsed.fragment.removeprefix("issuecomment-")))
            if len(parts) == 4 and parts[2] == "commit" and _SHA.fullmatch(parts[3]):
                revisions.add(parts[3])
        if len(comment_ids) != 1 or len(revisions) != 1:
            return None
        return next(iter(comment_ids)), next(iter(revisions))

    def _comment_artifacts(
        self, number: int, expected_sha: str | None, since: datetime | None
    ) -> list[QodoArtifact]:
        url = f"{self._api_root}/repos/{self._repository}/issues/{number}/comments"
        issue_comments = self._pages(url)
        by_id = {
            item.get("id"): item
            for item in issue_comments
            if self._qodo(item) and isinstance(item.get("id"), int)
        }
        candidates: list[tuple[datetime, int, int, str]] = []
        for marker in issue_comments:
            if not self._qodo(marker):
                continue
            body = marker.get("body")
            completion = (
                self._completion_marker(body, number) if isinstance(body, str) else None
            )
            published = moment(marker.get("created_at"))
            marker_id = marker.get("id")
            if completion is None or published is None or not isinstance(marker_id, int):
                continue
            if since is not None and published < since:
                continue
            comment_id, sha = completion
            candidates.append((published, marker_id, comment_id, sha))
        if not candidates:
            return []
        # Qodo edits one persistent review comment in place. Only its newest
        # completion marker can describe that comment's current content; an
        # older marker paired with the now-updated body would misattribute a
        # newer review to an older SHA.
        published, _, comment_id, sha = max(
            candidates, key=lambda item: (item[0], item[1])
        )
        if expected_sha is not None and sha != expected_sha:
            return []
        source = by_id.get(comment_id)
        summary = source.get("body") if isinstance(source, dict) else None
        if not isinstance(summary, str) or not summary.strip():
            return []
        return [QodoArtifact(sha, summary, (), published)]

    def _formal_artifacts(
        self, number: int, expected_sha: str | None, since: datetime | None
    ) -> list[QodoArtifact]:
        url = f"{self._api_root}/repos/{self._repository}/pulls/{number}/reviews"
        reviews = self._pages(url)
        artifacts: list[QodoArtifact] = []
        for review in reviews:
            if not self._qodo(review):
                continue
            sha = review.get("commit_id")
            submitted = moment(review.get("submitted_at"))
            review_id = review.get("id")
            if not isinstance(sha, str) or submitted is None:
                continue
            if expected_sha is not None and sha != expected_sha:
                continue
            if since is not None and submitted < since:
                continue
            if not isinstance(review_id, int) or isinstance(review_id, bool):
                raise QodoArtifactUnavailable
            listed = self._pages(f"{url}/{review_id}/comments")
            comments = self._comments(listed)
            body = review.get("body")
            summary = body if isinstance(body, str) else ""
            if not summary.strip() and not comments:
                continue
            artifacts.append(QodoArtifact(sha, summary, comments, submitted))
        return artifacts

    def read(
        self,
        number: int,
        expected_sha: str | None = None,
        since: datetime | None = None,
    ) -> QodoArtifact | None:
        artifacts: list[QodoArtifact] = []
        failures = 0
        for load in (self._comment_artifacts, self._formal_artifacts):
            try:
                artifacts.extend(load(number, expected_sha, since))
            except QodoArtifactUnavailable:
                failures += 1
        if artifacts:
            artifacts.sort(key=lambda item: item.submitted_at)
            return artifacts[-1]
        if failures:
            raise QodoArtifactUnavailable
        return None
