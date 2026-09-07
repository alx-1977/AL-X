"""Read Qodo's published content for one exact pull request revision."""

from __future__ import annotations

from datetime import UTC, datetime

import httpx

from alx.contracts.review_content import ReviewContent, ReviewContentRequest, ReviewReadError
from alx.providers.qodo_artifact import QodoArtifactReader, QodoArtifactUnavailable

REVIEWER = "qodo"
NO_REVIEW_FOR_REVISION = "no_review_for_revision"


class QodoReviewContentProvider:
    """Expose the same readable artifact that can complete the watcher."""

    def __init__(
        self,
        repository: str,
        token: str,
        api_root: str = "https://api.github.com",
    ) -> None:
        self._reader = QodoArtifactReader(repository, token, api_root)

    @property
    def reviewer(self) -> str:
        return REVIEWER

    def read(self, request: ReviewContentRequest) -> ReviewContent:
        now = datetime.now(UTC)
        try:
            artifact = self._reader.read(
                request.pull_request_number, request.head_sha
            )
        except QodoArtifactUnavailable:
            raise ReviewReadError("review_unavailable") from None
        if artifact is None:
            return ReviewContent(
                pull_request_number=request.pull_request_number,
                head_sha=request.head_sha,
                reviewer=REVIEWER,
                available=False,
                retrieved_at=now,
                unavailable_reason=NO_REVIEW_FOR_REVISION,
            )
        return ReviewContent(
            pull_request_number=request.pull_request_number,
            head_sha=request.head_sha,
            reviewer=REVIEWER,
            available=True,
            summary=artifact.summary,
            comments=artifact.comments,
            submitted_at=artifact.submitted_at,
            retrieved_at=now,
        )
