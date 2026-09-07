"""Observer and reader share one realistic Qodo/GitHub result contract."""

from __future__ import annotations

import unittest
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from alx.contracts.review_content import ReviewContentRequest, ReviewReadError
from alx.contracts.task import TaskState
from alx.providers import qodo_artifact
from alx.providers.qodo_review_content import QodoReviewContentProvider
from alx.providers.qodo_status import QodoStatusObserver, subject_reference
from tests.qodo_transcript import (
    GitHubTranscript,
    HEAD,
    PERSISTENT_COMMENT_ID,
    PUBLISHED_AT,
    SUMMARY,
    realistic_issue_comments,
)


class SharedTranscriptTests(unittest.TestCase):
    def use(self, transcript: GitHubTranscript) -> None:
        original = qodo_artifact.httpx.get
        qodo_artifact.httpx.get = transcript.get
        self.addCleanup(setattr, qodo_artifact.httpx, "get", original)

    def test_the_artifact_that_completes_is_readable(self) -> None:
        self.use(GitHubTranscript())
        observer = QodoStatusObserver("owner/repo", "token")
        observation = observer.observe(
            subject_reference(21),
            datetime.fromisoformat(PUBLISHED_AT) - timedelta(seconds=1),
        )
        self.assertIs(observation.state, TaskState.COMPLETED)
        self.assertEqual(observation.subject_reference, subject_reference(21, HEAD))

        content = QodoReviewContentProvider("owner/repo", "token").read(
            ReviewContentRequest(21, HEAD)
        )
        self.assertTrue(content.available)
        self.assertEqual(content.summary, SUMMARY)

    def test_persistent_history_is_not_a_completion_marker(self) -> None:
        comments = realistic_issue_comments()
        comments[0]["body"] += f"\nEarlier review: /commit/{HEAD}"
        self.use(GitHubTranscript(issue_comments=[comments[0]]))
        state = QodoStatusObserver("owner/repo", "token").observe(
            subject_reference(21)
        ).state
        self.assertIs(state, TaskState.WAITING_FOR_RESULT)

    def test_in_progress_text_is_not_a_completion_marker(self) -> None:
        marker = realistic_issue_comments()[1]
        marker["body"] = f"Qodo is still working on /commit/{HEAD}"
        self.use(GitHubTranscript(issue_comments=[marker]))
        state = QodoStatusObserver("owner/repo", "token").observe(
            subject_reference(21)
        ).state
        self.assertIs(state, TaskState.WAITING_FOR_RESULT)

    def test_a_marker_without_readable_content_does_not_complete(self) -> None:
        comments = realistic_issue_comments()
        comments[0]["body"] = ""
        self.use(GitHubTranscript(issue_comments=comments))
        observer = QodoStatusObserver("owner/repo", "token")
        self.assertIs(
            observer.observe(subject_reference(21)).state,
            TaskState.WAITING_FOR_RESULT,
        )
        content = QodoReviewContentProvider("owner/repo", "token").read(
            ReviewContentRequest(21, HEAD)
        )
        self.assertFalse(content.available)

    def test_an_undated_marker_cannot_cross_the_request_boundary(self) -> None:
        comments = realistic_issue_comments()
        comments[1]["created_at"] = "not-a-time"
        self.use(GitHubTranscript(issue_comments=comments))
        state = QodoStatusObserver("owner/repo", "token").observe(
            subject_reference(21), datetime.now(UTC) - timedelta(days=1)
        ).state
        self.assertIs(state, TaskState.WAITING_FOR_RESULT)

    def test_a_full_pagination_cap_is_unknown_not_a_truncated_answer(self) -> None:
        comments = [
            {
                "id": index,
                "user": {"id": 1},
                "body": "ordinary comment",
                "created_at": PUBLISHED_AT,
            }
            for index in range(1000)
        ]
        self.use(GitHubTranscript(issue_comments=comments, reviews=[]))
        observer = QodoStatusObserver("owner/repo", "token")
        self.assertIs(
            observer.observe(subject_reference(21)).state,
            TaskState.STATUS_UNKNOWN,
        )
        with self.assertRaises(ReviewReadError):
            QodoReviewContentProvider("owner/repo", "token").read(
                ReviewContentRequest(21, HEAD)
            )

    def test_formal_empty_review_is_not_available_empty(self) -> None:
        review = {
            "id": 99,
            "user": {"id": qodo_artifact.REVIEWER_ID},
            "commit_id": HEAD,
            "body": "",
            "submitted_at": PUBLISHED_AT,
        }
        self.use(GitHubTranscript(issue_comments=[], reviews=[review]))
        self.assertIs(
            QodoStatusObserver("owner/repo", "token").observe(
                subject_reference(21, HEAD)
            ).state,
            TaskState.WAITING_FOR_RESULT,
        )
        content = QodoReviewContentProvider("owner/repo", "token").read(
            ReviewContentRequest(21, HEAD)
        )
        self.assertFalse(content.available)

    def test_formal_inline_only_review_is_readable(self) -> None:
        review = {
            "id": 99,
            "user": {"id": qodo_artifact.REVIEWER_ID},
            "commit_id": HEAD,
            "body": "",
            "submitted_at": PUBLISHED_AT,
        }
        transcript = GitHubTranscript(
            issue_comments=[],
            reviews=[review],
            review_comments={99: [{"body": "Inline finding", "path": "x.py", "line": 4}]},
        )
        self.use(transcript)
        observer = QodoStatusObserver("owner/repo", "token")
        self.assertIs(
            observer.observe(subject_reference(21, HEAD)).state,
            TaskState.COMPLETED,
        )
        content = QodoReviewContentProvider("owner/repo", "token").read(
            ReviewContentRequest(21, HEAD)
        )
        self.assertEqual(content.comments[0].body, "Inline finding")

    def test_marker_must_link_the_existing_qodo_summary(self) -> None:
        comments = realistic_issue_comments()
        comments[1]["body"] = comments[1]["body"].replace(
            str(PERSISTENT_COMMENT_ID), "999999"
        )
        self.use(GitHubTranscript(issue_comments=comments))
        self.assertIs(
            QodoStatusObserver("owner/repo", "token").observe(
                subject_reference(21)
            ).state,
            TaskState.WAITING_FOR_RESULT,
        )

    def test_edited_persistent_content_is_never_attributed_to_an_old_marker(self) -> None:
        old_sha = "b" * 40
        comments = realistic_issue_comments(HEAD)
        old_marker = dict(comments[1])
        old_marker["id"] = 6000
        old_marker["body"] = old_marker["body"].replace(HEAD, old_sha)
        old_marker["created_at"] = "2026-09-06T06:15:00Z"
        comments.insert(1, old_marker)
        self.use(GitHubTranscript(issue_comments=comments))
        old = QodoReviewContentProvider("owner/repo", "token").read(
            ReviewContentRequest(21, old_sha)
        )
        self.assertFalse(old.available)
        current = QodoReviewContentProvider("owner/repo", "token").read(
            ReviewContentRequest(21, HEAD)
        )
        self.assertEqual(current.summary, SUMMARY)
