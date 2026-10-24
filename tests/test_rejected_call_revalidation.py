"""A durable mechanical refusal is binding only while its predicate holds."""

from __future__ import annotations

import json
import sys
import tempfile
import unittest
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from alx.contracts import (
    AgentDecision, Approval, ApprovalLifecycle, ApprovalScope, CapabilityAttempt, CapabilityAttemptDisposition,
    CapabilityCall, CapabilityDefinition, CapabilityResult,
    CapabilityResultState, CognitionOrigin, ConversationOrigin,
    ConversationSnapshot, ConversationTurn, ExecutionPlan, ExecutionStep, GoalMutationKind, GoalProposal,
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
        self.path = Path(directory.name) / "goals.sqlite3"
        self.store = SQLiteGoalStore(self.path, clock=lambda: NOW)
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
        self.assertEqual(CoreAgent._consecutive_coding_failures(state), 0)
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
                                 failure={"code": "task_failed"}),
            ) for call_id in ("failure-1", "failure-2", "failure-3", "failure-4")
        )
        state = goal(*failures, refused("coding_retry_exhausted"))
        reasoner = Reasoner(
            AgentDecision(call=CapabilityCall("new-resume", "run_coding_task", RESUME),
                          goal_id="goal-a"),
            AgentDecision(response="Four implementation failures exhausted this job.",
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
        stored = self.store.load("goal-a").state.attempts
        self.assertEqual(stored[:-1], state.attempts)
        self.assertEqual(stored[-1].reason_code, "repeated_rejected_call")

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
        stored = self.store.load("goal-a").state.attempts
        self.assertEqual(stored[:-1], state.attempts)
        self.assertEqual(stored[-1].reason_code, "repeated_rejected_call")

    def test_repeat_provenance_survives_restart_for_both_paths(self):
        earlier_time = NOW - timedelta(hours=2)
        earlier = replace(refused("input_invalid"), recorded_at=earlier_time)
        for path in ("person", "autonomous", "plan"):
            with self.subTest(path=path):
                identifier = f"goal-{path}"
                call = CapabilityCall(f"repeat-{path}", "run_coding_task", RESUME)
                state = replace(goal(earlier), goal_id=identifier)
                if path == "plan":
                    state = replace(state, execution_plan=ExecutionPlan(
                        "plan", state.objective.source_reference, state.objective.summary,
                        None, (ExecutionStep(call),),
                    ))
                self.store.create(state, "conversation", RETENTION)
                reasoner = Reasoner(
                    AgentDecision(call=call, goal_id=identifier),
                    AgentDecision(response="The input remains invalid.", goal_id=identifier),
                )
                agent = CoreAgent(
                    self.store, reasoner, lambda *_: self.fail("dispatched"),
                    (DEFINITION,), clock=lambda: NOW, plan_continuation=path == "plan",
                )
                with self.assertLogs("alx.core.loop", level="INFO") as captured:
                    if path == "plan":
                        self.assertEqual(agent.advance_due_plans(), ())
                    else:
                        agent.process(self.conversation, RETENTION, 3, origin=(
                            CognitionOrigin.PERSON_TURN if path == "person"
                            else CognitionOrigin.EXTERNAL_EVENT
                        ))
                reopened = SQLiteGoalStore(self.path)
                try:
                    stored = reopened.load(identifier).state
                finally:
                    reopened.close()
                repeat = stored.attempts[-1]
                self.assertEqual(repeat.reason_code, "repeated_rejected_call")
                self.assertEqual(repeat.recorded_at, NOW)
                self.assertEqual(repeat.repeated_call_id, "old-refusal")
                self.assertEqual(repeat.repeated_recorded_at, earlier_time)
                self.assertEqual(repeat.repeated_reason_code, "input_invalid")
                log = next(line for line in captured.output if "repeated_rejected_call" in line)
                for value in ("old-refusal", earlier_time.isoformat(), "input_invalid"):
                    self.assertIn(value, log)
                self.assertEqual(CoreAgent._binding_rejected_call(stored, call, NOW), earlier)

    def test_legacy_repeat_time_is_explicitly_unknown(self):
        from alx.goals.store import _attempts

        legacy = _attempts({"attempts": [[
            "old-refusal", "run_coding_task", RESUME, None,
            "rejected", False, None, "input_invalid",
        ]]})[0]
        self.assertIsNone(legacy.recorded_at)
        with self.assertLogs("alx.core.loop", level="INFO") as captured:
            self.run_core(
                goal(legacy), Reasoner(AgentDecision(
                    call=CapabilityCall("again", "run_coding_task", RESUME),
                    goal_id="goal-a",
                )), lambda *_: self.fail("dispatched"),
                origin=CognitionOrigin.EXTERNAL_EVENT,
            )
        repeat = self.store.load("goal-a").state.attempts[-1]
        self.assertIsNone(repeat.repeated_recorded_at)
        self.assertIn("repeated_recorded_at=unknown", "\n".join(captured.output))

    def test_recorded_repeat_does_not_bind_a_repaired_approval(self):
        earlier = replace(refused("approval_required"), recorded_at=NOW)
        snapshot = self.store.create(goal(earlier), "conversation", RETENTION)
        agent = CoreAgent(self.store, Reasoner(), lambda *_: self.fail("dispatched"),
                          (DEFINITION,))
        call = CapabilityCall("again", "run_coding_task", RESUME)
        snapshot = agent._record_repeated_rejection(snapshot, call, earlier, NOW)
        permitted = replace(call, call_id="approved", approval_id="fresh-approval")
        repaired = replace(snapshot.state, approvals=(Approval(
            "fresh-approval", ApprovalScope(permitted.capability_id, permitted.arguments),
            ApprovalLifecycle.GRANTED,
        ),))
        self.assertIsNone(CoreAgent._binding_rejected_call(repaired, permitted, NOW))

    def test_new_dispatch_refusal_gets_its_recorded_time(self):
        call = CapabilityCall("first-refusal", "run_coding_task", RESUME)
        self.run_core(goal(), Reasoner(
            AgentDecision(call=call, goal_id="goal-a"),
            AgentDecision(response="The input is invalid.", goal_id="goal-a"),
        ), lambda proposed, _: CapabilityAttempt(
            proposed, CapabilityAttemptDisposition.REJECTED, False,
            reason_code="input_invalid",
        ))
        attempt = self.store.load("goal-a").state.attempts[-1]
        self.assertEqual(attempt.recorded_at, NOW)

    def test_first_step_selection_reprojects_goal_referenced_old_turn(self):
        old = ConversationTurn(
            "conversation", "old-request", ConversationOrigin.TYPED,
            "Preserve the work from my original request", NOW, "friedl",
        )
        fillers = tuple(
            ConversationTurn(
                "conversation", f"filler-{index}", ConversationOrigin.TYPED,
                "Other conversation", NOW, "friedl",
            ) for index in range(12)
        )
        self.conversation = ConversationSnapshot(
            "conversation", (old, *fillers, self.conversation.turns[0]),
            14, RETENTION,
        )
        state = replace(
            goal(refused("input_invalid")),
            objective=Objective("turn:old-request", "Continue the preserved job"),
        )
        reasoner = Reasoner(
            AgentDecision(call=CapabilityCall("again", "run_coding_task", RESUME),
                          goal_id="goal-a"),
            AgentDecision(response="The prior input remains invalid.", goal_id="goal-a"),
        )
        outcome = self.run_core(state, reasoner, lambda *_: self.fail("dispatched"))
        self.assertIs(outcome.state, CoreState.RESPONDED)
        self.assertNotIn(old, reasoner.contexts[0].turns)
        self.assertIn(old, reasoner.contexts[1].turns)


if __name__ == "__main__":
    unittest.main()
