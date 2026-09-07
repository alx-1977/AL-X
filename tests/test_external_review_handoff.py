"""A real Qodo completion reaches a Core turn and is read there."""

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
from alx.providers import qodo_artifact
from alx.providers.qodo_review_content import QodoReviewContentProvider
from alx.providers.qodo_status import QodoStatusObserver, subject_reference
from alx.tools.review_content import (
    DEFINITION,
    READ_EXTERNAL_REVIEW,
    build_review_content_executors,
)
from tests.qodo_transcript import GitHubTranscript, HEAD, PUBLISHED_AT, SUMMARY


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
        transcript = GitHubTranscript()
        original = qodo_artifact.httpx.get
        qodo_artifact.httpx.get = transcript.get
        self.addCleanup(setattr, qodo_artifact.httpx, "get", original)

        task_store = SQLiteTaskStore(root / "tasks.sqlite3")
        requested_at = datetime.fromisoformat(PUBLISHED_AT) - timedelta(seconds=1)
        task_store.record(
            ExternalTask(
                task_id="review-task-1",
                kind="external_review",
                service="qodo",
                subject_reference=subject_reference(21),
                state=TaskState.REQUESTED,
                requested_at=requested_at,
                conversation_id="conversation-1",
            )
        )
        TaskPoller(
            task_store,
            {"qodo": QodoStatusObserver("owner/repo", "token")},
            1.0,
            lambda conversation, values: None,
            lambda task: None,
        ).tick()

        ledger = Ledger()
        source = CompletedWorkSource(task_store, ledger, enabled=True)
        opportunity = source.due_opportunities()[0]
        self.assertIs(opportunity.origin, CognitionOrigin.WORK_COMPLETED)

        reader = QodoReviewContentProvider("owner/repo", "token")
        executor = build_review_content_executors(reader.read, lambda: "read-1")
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
        self.assertEqual(task_store.completed_unhandled(), ())
