"""A durable mechanical refusal is binding only while its predicate holds."""

from __future__ import annotations

import json
import sys
import tempfile
import unittest
from datetime import UTC, datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from alx.contracts import (
    AgentDecision, CapabilityAttempt, CapabilityAttemptDisposition,
    CapabilityCall, CapabilityDefinition, CapabilityResult,
    CapabilityResultState, CognitionOrigin, ConversationOrigin,
    ConversationSnapshot, ConversationTurn, GoalMutationKind, GoalProposal,
    GoalState, Objective,
    SideEffect, StructuredSchema, SuccessCriterion, ValueKind,
)
from alx.core.loop import CoreAgent, CoreState
from alx.goals import SQLiteGoalStore


NOW = datetime(2026, 10, 1, tzinfo=UTC)
RETENTION = NOW + timedelta(days=1)
DEFINITION = CapabilityDefinition(
    "run_coding_task", "Continue bounded coding work",
    StructuredSchema(ValueKind.OBJECT), StructuredSchema(ValueKind.OBJECT),
    SideEffect.EFFECTFUL,
)
RESUME = {"resume_job_id": "resume-01"}


def old_timeout(call_id: str, arguments: dict[str, str], digest: str) -> CapabilityAttempt:
    call = CapabilityCall(call_id, "run_coding_task", arguments)
    return CapabilityAttempt(
        call, CapabilityAttemptDisposition.EXECUTED, True,
        CapabilityResult(
            call_id, "run_coding_task", CapabilityResultState.FAILED,
            {"status": "failed", "checkpoint": json.dumps({
                "stage": "execution", "head_sha": "a" * 40,
                "state_digest": digest,
            })},
            {"code": "session_failed", "reason_code": "session_timeout"},
        ),
    )


def refused(reason: str, arguments: dict[str, str] = RESUME) -> CapabilityAttempt:
    return CapabilityAttempt(
        CapabilityCall("old-refusal", "run_coding_task", arguments),
        CapabilityAttemptDisposition.REJECTED, False, reason_code=reason,
    )


def goal(*attempts: CapabilityAttempt) -> GoalState:
    return GoalState(
        "goal-a", Objective("turn:person", "Continue the preserved job"),
        (SuccessCriterion("done", "Verified result"),), attempts=attempts,
    )


class Reasoner:
    def __init__(self, *decisions: AgentDecision) -> None:
        self.decisions = list(decisions)
        self.contexts = []

    def decide(self, context):
        self.contexts.append(context)
        return self.decisions.pop(0)


class RejectedCallRevalidationTests(unittest.TestCase):
    def setUp(self) -> None:
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.store = SQLiteGoalStore(Path(directory.name) / "goals.sqlite3")
        self.addCleanup(self.store.close)
        self.conversation = ConversationSnapshot(
            "conversation", (ConversationTurn(
                "conversation", "person", ConversationOrigin.TYPED,
                "Continue the existing job", NOW, "friedl",
            ),), 1, RETENTION,
        )

    def run_core(self, state, reasoner, dispatch, *, origin=CognitionOrigin.PERSON_TURN):
        self.store.create(state, "conversation", RETENTION)
        return CoreAgent(
            self.store, reasoner, dispatch, (DEFINITION,), clock=lambda: NOW,
        ).process(self.conversation, RETENTION, 3, origin=origin)

    def test_old_timeout_refusal_is_revalidated_and_resume_dispatches_once(self):
        state = goal(
            old_timeout("first", {"task": "work"}, "1" * 64),
            old_timeout("resume-01", {"resume_job_id": "first"}, "2" * 64),
            refused("coding_retry_exhausted"),
        )
        call = CapabilityCall("new-resume", "run_coding_task", RESUME)
        self.assertEqual(CoreAgent._failed_coding_executions(state), 0)
        self.assertIsNone(CoreAgent._coding_exhaustion_reason(state))
        self.assertFalse(CoreAgent._repeats_rejected_call(state, call, NOW))
        dispatched = []

        def dispatch(proposed, authority):
            dispatched.append(proposed)
            return CapabilityAttempt(
                proposed, CapabilityAttemptDisposition.EXECUTED, True,
                CapabilityResult(proposed.call_id, proposed.capability_id,
                                 CapabilityResultState.PARTIAL,
                                 {"status": "interrupted"}),
            )

        outcome = self.run_core(
            state, Reasoner(
                AgentDecision(call=call, goal_id="goal-a"),
                AgentDecision(response="The preserved work is continuing.", goal_id="goal-a"),
            ), dispatch,
        )
        self.assertIs(outcome.state, CoreState.RESPONDED)
        self.assertEqual(dispatched, [call])
        stored = self.store.load("goal-a").state.attempts
        self.assertEqual(stored[2], state.attempts[2])
        self.assertEqual(stored[-1].call.call_id, "new-resume")

    def test_live_exhaustion_stops_without_dispatch_and_explains(self):
        failures = tuple(
            CapabilityAttempt(
                CapabilityCall(call_id, "run_coding_task", {"task": call_id}),
                CapabilityAttemptDisposition.EXECUTED, True,
                CapabilityResult(call_id, "run_coding_task", CapabilityResultState.FAILED,
                                 failure={"code": "session_failed"}),
            ) for call_id in ("failure-1", "failure-2")
        )
        state = goal(*failures, refused("coding_retry_exhausted"))
        reasoner = Reasoner(
            AgentDecision(call=CapabilityCall("new-resume", "run_coding_task", RESUME),
                          goal_id="goal-a"),
            AgentDecision(response="Two implementation failures exhausted this job.",
                          goal_id="goal-a"),
        )
        outcome = self.run_core(state, reasoner, lambda *_: self.fail("dispatched"))
        self.assertIs(outcome.state, CoreState.RESPONDED)
        self.assertEqual(outcome.reason, "coding_retry_exhausted")
        self.assertEqual(len(reasoner.contexts), 2)
        self.assertEqual(reasoner.contexts[-1].response_only_reason,
                         "coding_retry_exhausted")
        self.assertEqual(self.store.load("goal-a").state.attempts, state.attempts)

    def test_unsafe_repeat_stays_blocked_without_second_dispatch(self):
        state = goal(refused("input_invalid"))
        reasoner = Reasoner(
            AgentDecision(call=CapabilityCall("again", "run_coding_task", RESUME),
                          goal_id="goal-a"),
            AgentDecision(response="The same input is still invalid.", goal_id="goal-a"),
        )
        outcome = self.run_core(state, reasoner, lambda *_: self.fail("dispatched"))
        self.assertIs(outcome.state, CoreState.RESPONDED)
        self.assertEqual(outcome.reason, "repeated_rejected_call")
        self.assertEqual(reasoner.contexts[-1].refused_calls[-1]["subject"],
                         "input_invalid")
        self.assertEqual(self.store.load("goal-a").state.attempts, state.attempts)

    def test_autonomous_repeat_checkpoints_without_dispatch(self):
        state = goal(refused("input_invalid"))
        outcome = self.run_core(
            state, Reasoner(AgentDecision(
                call=CapabilityCall("again", "run_coding_task", RESUME),
                goal_id="goal-a",
            )), lambda *_: self.fail("dispatched"),
            origin=CognitionOrigin.EXTERNAL_EVENT,
        )
        self.assertIs(outcome.state, CoreState.CHECKPOINTED)
        self.assertEqual(outcome.reason, "repeated_rejected_call")

    def test_terminal_response_only_pass_cannot_resume_or_mutate(self):
        state = goal(refused("input_invalid"))
        reasoner = Reasoner(
            AgentDecision(call=CapabilityCall("again", "run_coding_task", RESUME),
                          goal_id="goal-a"),
            AgentDecision(
                call=CapabilityCall("unsafe", "run_coding_task", {"task": "new"}),
                goal_proposal=GoalProposal(GoalMutationKind.REQUEST_COMPLETION),
                goal_id="goal-a",
            ),
        )
        outcome = self.run_core(state, reasoner, lambda *_: self.fail("dispatched"))
        self.assertIs(outcome.state, CoreState.CHECKPOINTED)
        self.assertEqual(outcome.reason, "repeated_rejected_call")
        self.assertEqual(len(reasoner.contexts), 2)
        self.assertEqual(self.store.load("goal-a").state.attempts, state.attempts)


if __name__ == "__main__":
    unittest.main()
