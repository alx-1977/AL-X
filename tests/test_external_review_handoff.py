"""A real review completion reaches a Core turn and is read there.

The whole handoff, end to end and through the production path: a reviewer
publishes, the watcher observes it as complete, that becomes an occasion, the
Core wakes on it and calls the real read capability, and the reviewer's own
words arrive as evidence with the revision they were written about.

Provider-neutral throughout. The reviewer is configuration, so the only thing
this test names about it is the account that published.
"""

from __future__ import annotations

import tempfile
import unittest
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from alx.contracts import (
    AgentDecision,
    CapabilityAttempt,
    CapabilityAttemptDisposition,
    CapabilityCall,
    CognitionOrigin,
    GoalMutationKind,
    GoalProposal,
    SuccessCriterion,
)
from alx.continuity import CompletedWorkSource
from alx.continuity.tasks import SQLiteTaskStore
from alx.contracts.task import ExternalTask, TaskState
from alx.conversation.gateway import ConversationGateway
from alx.conversation.store import SQLiteConversationStore
from alx.core import CoreAgent
from alx.goals import SQLiteGoalStore
from alx.interfaces.task_poller import TaskPoller
from alx.contracts.review_provider import ReviewProvider, profile_for
from alx.providers import github_review
from alx.providers.github_review import GitHubReviewProvider
from alx.providers.review_status import ReviewStatusObserver, subject_reference
from alx.tools.review_content import (
    DEFINITION,
    READ_EXTERNAL_REVIEW,
    build_review_content_executors,
)
from tests.review_transcript import HEAD, PUBLISHED_AT, REVIEWER_LOGIN

REVIEWER = "coderabbit"
SUMMARY = f"Formal review body. Reviewed up to {HEAD}."


class Ledger:
    def __init__(self) -> None:
        self.identifiers: set[str] = set()

    def exists(self, identifier: str) -> bool:
        return identifier in self.identifiers

    def record_created(self, opportunity) -> bool:
        if opportunity.opportunity_id in self.identifiers:
            return False
        self.identifiers.add(opportunity.opportunity_id)
        return True

    def release(self, identifier: str) -> None:
        self.identifiers.discard(identifier)


class Decisions:
    def __init__(self) -> None:
        self.contexts = []

    def decide(self, context):
        self.contexts.append(context)
        if len(self.contexts) == 1:
            return AgentDecision(
                call=CapabilityCall(
                    "read-1",
                    READ_EXTERNAL_REVIEW,
                    {"pull_request_number": 21, "head_sha": HEAD},
                ),
                goal_proposal=GoalProposal(
                    GoalMutationKind.CREATE,
                    "Evaluate the completed external review",
                    (SuccessCriterion("review-read", "the review is read"),),
                ),
            )
        return AgentDecision(response="I read the completed review.")


class ExternalReviewHandoffTests(unittest.TestCase):
    def test_work_completed_invokes_the_real_read_capability(self) -> None:
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        root = Path(directory.name)
        summary = {
            "id": 99,
            "user": {"login": REVIEWER_LOGIN},
            "body": SUMMARY,
            # After the request, as a real review is.
            "created_at": "2026-09-07T06:15:30Z",
        }
        # The review round the finding was published in. Inline comments are
        # bound to the head through this object, so a handoff that carries a
        # finding needs the real artefact rather than a bare comment.
        submitted = [{
            "id": 55,
            "user": {"login": REVIEWER_LOGIN},
            "body": "",
            "state": "COMMENTED",
            "commit_id": HEAD,
            "submitted_at": "2026-09-07T06:15:30Z",
        }]
        inline = [{
            "id": 100,
            "user": {"login": REVIEWER_LOGIN},
            "body": "Inline finding",
            "path": "x.py",
            "line": 4,
            "commit_id": HEAD,
            "original_commit_id": HEAD,
            "pull_request_review_id": 55,
        }]

        class Response:
            def __init__(self, payload) -> None:
                self.status_code = 200
                self.headers: dict = {}
                self._payload = payload

            def json(self):
                return self._payload

        def request(method, url, **keywords):
            if method != "GET":
                raise AssertionError("the handoff reads; it must not write")
            base = url.split("?")[0]
            first = "page=1" in url
            if base.endswith("/issues/21/comments"):
                return Response([summary] if first else [])
            if base.endswith("/pulls/21/comments"):
                return Response(inline if first else [])
            if base.endswith("/pulls/21/reviews"):
                return Response(submitted if first else [])
            return Response({"head": {"sha": HEAD}})

        original = github_review.httpx.request
        github_review.httpx.request = request
        self.addCleanup(setattr, github_review.httpx, "request", original)
        provider = GitHubReviewProvider(
            "owner/repo", "token", profile_for(ReviewProvider.CODERABBIT)
        )

        task_store = SQLiteTaskStore(root / "tasks.sqlite3")
        requested_at = datetime.fromisoformat(PUBLISHED_AT) - timedelta(seconds=1)
        task_store.record(
            ExternalTask(
                task_id="review-task-1",
                kind="external_review",
                service=REVIEWER,
                subject_reference=subject_reference(21, HEAD),
                state=TaskState.REQUESTED,
                requested_at=requested_at,
                conversation_id="conversation-1",
            )
        )
        TaskPoller(
            task_store,
            {REVIEWER: ReviewStatusObserver(provider)},
            1.0,
            lambda conversation, values: None,
            lambda task: None,
        ).tick()

        ledger = Ledger()
        source = CompletedWorkSource(task_store, ledger, enabled=True)
        opportunity = source.due_opportunities()[0]
        self.assertIs(opportunity.origin, CognitionOrigin.WORK_COMPLETED)

        executor = build_review_content_executors(provider.read, lambda: "read-1")
        attempts: list[CapabilityAttempt] = []

        def dispatch(call, state):
            attempt = CapabilityAttempt(
                call,
                CapabilityAttemptDisposition.EXECUTED,
                True,
                executor[call.capability_id](call.arguments),
            )
            attempts.append(attempt)
            return attempt

        goals = SQLiteGoalStore(root / "goals.sqlite3")
        self.addCleanup(goals.close)
        conversations = SQLiteConversationStore(root / "conversations.sqlite3")
        self.addCleanup(conversations.close)
        decisions = Decisions()
        core = CoreAgent(
            goals,
            decisions,
            dispatch,
            (DEFINITION,),
            clock=lambda: datetime(2026, 9, 7, 7, 0, tzinfo=UTC),
            identifier_factory=lambda: "goal-1",
            approval_free_capabilities=frozenset({READ_EXTERNAL_REVIEW}),
        )
        gateway = ConversationGateway(
            core,
            conversations,
            identifier_factory=lambda: "turn-1",
            clock=lambda: datetime(2026, 9, 7, 7, 0, tzinfo=UTC),
        )
        outcome = gateway.receive_cognition_opportunity(
            "conversation-1",
            opportunity,
            4,
            datetime(2027, 9, 7, tzinfo=UTC),
        )
        source.mark_honoured(opportunity)

        self.assertEqual(outcome.response, "I read the completed review.")
        self.assertIs(decisions.contexts[0].origin, CognitionOrigin.WORK_COMPLETED)
        self.assertEqual(len(attempts), 1)
        result = attempts[0].result
        self.assertTrue(result.values["available"])
        self.assertEqual(result.values["head_sha"], HEAD)
        self.assertEqual(result.values["summary"], SUMMARY)
        self.assertEqual(result.values["comments"][0]["body"], "Inline finding")
        self.assertEqual(task_store.completed_unhandled(), ())
