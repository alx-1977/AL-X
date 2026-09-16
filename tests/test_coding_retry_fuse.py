"""The Coding Agent failed-run fuse is durable, goal-scoped, and pre-dispatch."""

from __future__ import annotations

import sys
import tempfile
import unittest
from datetime import UTC, datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from alx.contracts import (  # noqa: E402
    CapabilityAttempt, CapabilityAttemptDisposition, CapabilityCall,
    CapabilityResult, CapabilityResultState, GoalState, Objective,
    SuccessCriterion, AgentDecision, CapabilityDefinition, ConversationOrigin,
    ConversationSnapshot, ConversationTurn, SideEffect, StructuredSchema, ValueKind,
)
from alx.core import CoreAgent  # noqa: E402
from alx.goals import SQLiteGoalStore  # noqa: E402


def attempt(call_id: str, *, failed=True, invoked=True, disposition=CapabilityAttemptDisposition.EXECUTED):
    call = CapabilityCall(call_id, "run_coding_task", {"task": call_id, "worktree": "."})
    if disposition is CapabilityAttemptDisposition.REJECTED:
        return CapabilityAttempt(call, disposition, False, reason_code="input_invalid")
    result = CapabilityResult(call_id, "run_coding_task", CapabilityResultState.FAILED if failed else CapabilityResultState.SUCCEEDED, failure={"code": "task_failed"} if failed else None)
    return CapabilityAttempt(call, disposition, invoked, result, "executor_error" if disposition is CapabilityAttemptDisposition.BROKER_FAILURE else None)


def state(*attempts):
    return GoalState("goal-a", Objective("turn:t", "bounded work"), (SuccessCriterion("c", "done"),), attempts=attempts)


class CodingRetryFuseTests(unittest.TestCase):
    def test_changed_arguments_do_not_change_goal_scoped_identity(self):
        self.assertEqual(CoreAgent._failed_coding_executions(state(attempt("first"), attempt("rewritten"))), 2)

    def test_success_and_pre_effect_rejection_do_not_consume_allowance(self):
        self.assertEqual(CoreAgent._failed_coding_executions(state(attempt("ok", failed=False), attempt("rejected", disposition=CapabilityAttemptDisposition.REJECTED))), 0)

    def test_invoked_broker_failure_counts_but_noninvoked_does_not(self):
        counted = attempt("reached", disposition=CapabilityAttemptDisposition.BROKER_FAILURE)
        not_counted = attempt("not-reached", disposition=CapabilityAttemptDisposition.BROKER_FAILURE, invoked=False)
        self.assertEqual(CoreAgent._failed_coding_executions(state(counted, not_counted)), 1)

    def test_exhaustion_never_dispatches_or_creates_pending_and_reaches_reasoner(self):
        now = datetime(2026, 9, 16, tzinfo=UTC)
        retention = now + timedelta(days=1)

        class Reasoner:
            def __init__(self):
                self.contexts = []
                self.decisions = [
                    AgentDecision(call=CapabilityCall("third", "run_coding_task", {"task": "changed", "worktree": "."}), goal_id="goal-a"),
                    AgentDecision(response="The prior jobs failed.", goal_id="goal-a"),
                ]
            def decide(self, context):
                self.contexts.append(context)
                return self.decisions.pop(0)

        dispatched = []
        def dispatch(call, authority):
            dispatched.append(call)
            raise AssertionError("exhaustion must not reach dispatch")

        with tempfile.TemporaryDirectory() as directory:
            store = SQLiteGoalStore(Path(directory) / "goals.sqlite3")
            self.addCleanup(store.close)
            store.create(state(attempt("first"), attempt("second")), "conversation", retention)
            schema = StructuredSchema(ValueKind.OBJECT)
            capability = CapabilityDefinition("run_coding_task", "bounded job", schema, schema, SideEffect.EFFECTFUL)
            reasoner = Reasoner()
            agent = CoreAgent(store, reasoner, dispatch, (capability,), clock=lambda: now)
            conversation = ConversationSnapshot("conversation", (
                ConversationTurn("conversation", "t", ConversationOrigin.TYPED, "continue", now, "friedl"),
            ), 1, retention)
            agent.process(conversation, retention, 3)
            attempts = store.load("goal-a").state.attempts
            self.assertEqual(dispatched, [])
            self.assertEqual(attempts[-1].reason_code, "coding_retry_exhausted")
            self.assertFalse(attempts[-1].implementation_invoked)
            self.assertIsNot(attempts[-1].disposition, CapabilityAttemptDisposition.PENDING)
            self.assertEqual(len(reasoner.contexts[1].active_goal.attempts), 3)

    def test_reworded_attempt_after_exhaustion_does_not_append_another_refusal(self):
        now = datetime(2026, 9, 16, tzinfo=UTC)
        retention = now + timedelta(days=1)

        class Reasoner:
            def __init__(self):
                self.decisions = [
                    AgentDecision(call=CapabilityCall("first-refusal", "run_coding_task", {"task": "first wording", "worktree": "."}), goal_id="goal-a"),
                    AgentDecision(call=CapabilityCall("reworded", "run_coding_task", {"task": "different wording", "worktree": "."}), goal_id="goal-a"),
                ]
            def decide(self, context):
                return self.decisions.pop(0)

        with tempfile.TemporaryDirectory() as directory:
            store = SQLiteGoalStore(Path(directory) / "goals.sqlite3")
            self.addCleanup(store.close)
            store.create(state(attempt("first"), attempt("second")), "conversation", retention)
            schema = StructuredSchema(ValueKind.OBJECT)
            capability = CapabilityDefinition("run_coding_task", "bounded job", schema, schema, SideEffect.EFFECTFUL)
            agent = CoreAgent(store, Reasoner(), lambda *_: self.fail("must not dispatch"), (capability,), clock=lambda: now)
            conversation = ConversationSnapshot("conversation", (ConversationTurn("conversation", "t", ConversationOrigin.TYPED, "continue", now, "friedl"),), 1, retention)
            outcome = agent.process(conversation, retention, 3)
            refusals = [item for item in store.load("goal-a").state.attempts if item.reason_code == "coding_retry_exhausted"]
            self.assertEqual(len(refusals), 1)
            self.assertEqual(outcome.reason, "coding_retry_exhausted")


if __name__ == "__main__":
    unittest.main()
