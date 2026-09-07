"""Observe whether Qodo has published a readable result for a pull request."""

from __future__ import annotations

import re
from datetime import UTC, datetime

import httpx

from alx.contracts.task import TaskObservation, TaskState
from alx.providers.qodo_artifact import QodoArtifactReader, QodoArtifactUnavailable

_SUBJECT = re.compile(r"^pull/(?P<number>\d+)(?:@(?P<sha>[0-9a-f]{40}))?$")


def subject_reference(pull_request_number: int, head_sha: str = "") -> str:
    suffix = f"@{head_sha}" if head_sha else ""
    return f"pull/{pull_request_number}{suffix}"


class QodoStatusObserver:
    """Reports completion only when the result can also be read."""

    service = "qodo"

    def __init__(
        self,
        repository: str,
        token: str,
        api_root: str = "https://api.github.com",
    ) -> None:
        self._reader = QodoArtifactReader(repository, token, api_root)

    def observe(self, subject: str, since: datetime | None = None) -> TaskObservation:
        now = datetime.now(UTC)
        match = _SUBJECT.match(subject)
        if match is None:
            return TaskObservation(TaskState.STATUS_UNKNOWN, now)
        try:
            artifact = self._reader.read(
                int(match.group("number")), match.group("sha"), since
            )
        except QodoArtifactUnavailable:
            return TaskObservation(TaskState.STATUS_UNKNOWN, now)
        if artifact is None:
            return TaskObservation(TaskState.WAITING_FOR_RESULT, now)
        return TaskObservation(
            TaskState.COMPLETED,
            now,
            subject_reference(int(match.group("number")), artifact.head_sha),
        )
