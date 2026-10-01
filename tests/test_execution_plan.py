from __future__ import annotations

import sys
import tempfile
import unittest
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from alx.contracts import (  # noqa: E402
    AgentDecision, CapabilityAttempt, CapabilityAttemptDisposition,
    CapabilityCall, CapabilityDefinition, CapabilityResult, CapabilityResultState,
    CognitionOrigin, ConversationOrigin, ConversationSnapshot, ConversationTurn, ExecutionPlan,
    ExecutionStep, GoalState, Objective, PlanCondition, SideEffect,
    StructuredSchema, SuccessCriterion, ValueKind,
)
from alx.core import CoreAgent, CoreState  # noqa: E402
from alx.continuity.plan_source import PlanContinuationSource  # noqa: E402
from alx.goals import SQLiteGoalStore  # noqa: E402

NOW = datetime(2026, 10, 1, tzinfo=UTC)
RETENTION = NOW + timedelta(days=30)
SCHEMA = StructuredSchema(ValueKind.OBJECT)


def conversation(turn_id="person-1"):
    return ConversationSnapshot(
        "thread", (ConversationTurn("thread", turn_id, ConversationOrigin.TYPED,
                                    "Please do the work", NOW, "friedl"),), 1, RETENTION,
    )


def goal():
    return GoalState("goal", Objective("turn:person-1", "Do the work"),
                     (SuccessCriterion("done", "verified"),))


def plan(*steps):
    return ExecutionPlan("plan-1", "turn:person-1", "Do the work",
                         "person-1", tuple(steps))


def step(name, *, completion=(), waiting=(), wake=False):
    if not completion:
        completion = (PlanCondition("state", "succeeded"),)
    return ExecutionStep(
        CapabilityCall(f"call-{name}", name, {}),
        tuple(completion), tuple(waiting), 10 if waiting else 0, wake,
    )


class Reasoner:
    def __init__(self, *decisions):
        self.decisions = list(decisions)
        self.calls = 0
        self.contexts = []

    def decide(self, context):
        self.calls += 1
        self.contexts.append(context)
        return self.decisions.pop(0)


class ExecutionPlanTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.path = Path(directory.name) / "goals.sqlite3"
        self.store = SQLiteGoalStore(self.path)
        self.addCleanup(self.store.close)
        self.store.create(goal(), "thread", RETENTION)
        self.now = NOW
        self.calls = []
        self.outputs = {}

    def agent(self, reasoner, *, turn_bound=frozenset(),
              review_requires_judgment=False, effectful=frozenset()):
        names = ("coding", "review", "ci", "merge", "sync", "cleanup",
                 "other", "request_external_review")
        definitions = tuple(CapabilityDefinition(
            name, name, SCHEMA, SCHEMA,
            SideEffect.EFFECTFUL if name in effectful else SideEffect.NONE,
            requires_core_judgment=(name == "review" and review_requires_judgment),
            repeat_safe_observation=(name == "review"),
        ) for name in names)

        def dispatch(call, _state):
            self.calls.append(call.capability_id)
            value = self.outputs.get(call.capability_id, {"state": "done"})
            if isinstance(value, list):
                value = value.pop(0)
            if isinstance(value, tuple):
                state, body = value
            else:
                state, body = CapabilityResultState.SUCCEEDED, value
            result = CapabilityResult(
                call.call_id, call.capability_id, state,
                body if state is CapabilityResultState.SUCCEEDED else {},
                body if state is CapabilityResultState.FAILED else None,
            )
            return CapabilityAttempt(call, CapabilityAttemptDisposition.EXECUTED,
                                     True, result)

        return CoreAgent(self.store, reasoner, dispatch, definitions,
                         clock=lambda: self.now,
                         identifier_factory=lambda: f"repeat-{len(self.calls)}",
                         turn_bound_capabilities=turn_bound)

    def test_clean_sequence_uses_one_planning_and_one_final_core_call(self):
        workflow = plan(*(step(name) for name in (
            "coding", "review", "ci", "merge", "sync", "cleanup"
        )))
        reasoner = Reasoner(AgentDecision(execution_plan=workflow, goal_id="goal"),
                            AgentDecision(response="Finished.", goal_id="goal"))
        outcome = self.agent(reasoner).process(conversation(), RETENTION, 3)
        self.assertEqual(outcome.state, CoreState.RESPONDED)
        self.assertEqual(reasoner.calls, 2)
        self.assertEqual(self.calls, ["coding", "review", "ci", "merge", "sync", "cleanup"])
        self.assertEqual(self.store.load("goal").state.execution_plan.cursor, 6)

    def test_pending_ci_waits_without_core_and_changed_result_wakes_once(self):
        workflow = plan(step("ci", completion=(PlanCondition("values.state", "passed"),),
                             waiting=(PlanCondition("values.state", "pending"),)))
        self.outputs["ci"] = [{"state": "pending"}, {"state": "pending"},
                              {"state": "failed"}]
        reasoner = Reasoner(AgentDecision(execution_plan=workflow, goal_id="goal"))
        outcome = self.agent(reasoner).process(conversation(), RETENTION, 2)
        self.assertEqual(outcome.reason, "plan_waiting")
        self.assertEqual(reasoner.calls, 1)
        self.assertEqual(self.agent(reasoner).advance_due_plans(lambda _: conversation()), 0)
        self.now += timedelta(seconds=10)
        self.assertEqual(self.agent(reasoner).advance_due_plans(lambda _: conversation()), 1)
        self.assertEqual(reasoner.calls, 1)
        self.now += timedelta(seconds=10)
        self.agent(reasoner).advance_due_plans(lambda _: conversation())
        self.assertEqual(self.store.load("goal").state.execution_plan.core_reentry_reason,
                         "planned_result_unexpected")
        self.assertEqual(self.agent(reasoner).advance_due_plans(lambda _: conversation()), 0)

    def test_restart_resumes_cursor_and_changed_precondition_wakes_core(self):
        workflow = plan(step("coding"), step("ci", completion=(
            PlanCondition("values.state", "done"),), waiting=(
            PlanCondition("values.state", "pending"),)))
        self.outputs["ci"] = [{"state": "pending"}, {"state": "done"}]
        reasoner = Reasoner(AgentDecision(execution_plan=workflow, goal_id="goal"))
        self.agent(reasoner).process(conversation(), RETENTION, 2)
        self.store.close()
        self.store = SQLiteGoalStore(self.path)
        self.now += timedelta(seconds=10)
        self.agent(reasoner).advance_due_plans(lambda _: conversation())
        self.assertEqual(self.store.load("goal").state.execution_plan.status, "completed")
        self.assertEqual(self.calls, ["coding", "ci", "ci"])

        snapshot = self.store.load("goal")
        replacement = replace(workflow, plan_id="plan-2", cursor=0)
        self.store.replace(replace(snapshot.state, execution_plan=replacement),
                           snapshot.retention_until, snapshot.revision)
        self.agent(reasoner).advance_due_plans(lambda _: conversation("person-2"))
        self.assertEqual(self.store.load("goal").state.execution_plan.core_reentry_reason,
                         "plan_precondition_changed")

    def test_plan_cannot_spend_turn_bound_authority(self):
        workflow = plan(step("review"))
        reasoner = Reasoner(AgentDecision(execution_plan=workflow, goal_id="goal"),
                            AgentDecision(response="I need authority.", goal_id="goal"))
        self.agent(reasoner, turn_bound=frozenset({"review"})).process(
            conversation(), RETENTION, 2,
        )
        self.assertEqual(self.calls, [])
        self.assertIsNone(self.store.load("goal").state.execution_plan)

    def test_review_completion_returns_to_core_before_merge(self):
        workflow = plan(step("review"), step("merge"))
        reasoner = Reasoner(AgentDecision(execution_plan=workflow, goal_id="goal"),
                            AgentDecision(response="Review needs judgment.", goal_id="goal"))
        outcome = self.agent(reasoner, review_requires_judgment=True).process(
            conversation(), RETENTION, 3,
        )
        self.assertEqual(outcome.state, CoreState.RESPONDED)
        self.assertEqual(self.calls, ["review"])
        self.assertEqual(reasoner.calls, 2)
        self.assertEqual(reasoner.contexts[1].transient_attempts[0].result.values["state"],
                         "done")

    def test_pending_external_review_is_mechanical_until_published(self):
        workflow = plan(step("review", completion=(
            PlanCondition("values.available", True),), waiting=(
            PlanCondition("failure.code", "review_unavailable"),), wake=True))
        self.outputs["review"] = [
            (CapabilityResultState.FAILED, {"code": "review_unavailable"}),
            {"available": True},
        ]
        reasoner = Reasoner(AgentDecision(execution_plan=workflow, goal_id="goal"))
        self.agent(reasoner, effectful=frozenset({"review"})).process(
            conversation(), RETENTION, 2,
        )
        self.assertEqual(reasoner.calls, 1)
        self.assertEqual(self.store.load("goal").state.execution_plan.status, "waiting")
        self.now += timedelta(seconds=10)
        self.agent(reasoner, effectful=frozenset({"review"})).advance_due_plans(
            lambda _: conversation(),
        )
        self.assertEqual(self.calls, ["review", "review"])
        self.assertEqual(self.store.load("goal").state.execution_plan.status, "needs_core")
        self.assertEqual(reasoner.calls, 1)

    def test_failed_test_and_merge_refusal_stop_before_next_step(self):
        for name in ("ci", "merge"):
            with self.subTest(name=name):
                snapshot = self.store.load("goal")
                self.store.replace(replace(snapshot.state, execution_plan=None),
                                   snapshot.retention_until, snapshot.revision)
                self.calls.clear()
                self.outputs[name] = (CapabilityResultState.FAILED,
                                      {"code": "failed"})
                workflow = replace(plan(step(name), step("cleanup")), plan_id=f"plan-{name}")
                reasoner = Reasoner(AgentDecision(execution_plan=workflow, goal_id="goal"),
                                    AgentDecision(response="I will assess the failure.",
                                                  goal_id="goal"))
                self.agent(reasoner).process(conversation(), RETENTION, 3)
                self.assertEqual(self.calls, [name])
                self.assertEqual(reasoner.calls, 2)

    def test_completed_plan_is_not_offered_again_after_core_response(self):
        workflow = plan(step("coding"))
        reasoner = Reasoner(AgentDecision(execution_plan=workflow, goal_id="goal"),
                            AgentDecision(response="Done.", goal_id="goal"))
        self.agent(reasoner).process(conversation(), RETENTION, 3)

        class Ledger:
            def exists(self, _identifier):
                return False

        source = PlanContinuationSource(self.store, Ledger(), enabled=True)
        self.assertEqual(source.due_opportunities(), ())
        self.assertEqual(self.store.load("goal").state.execution_plan.status, "handled")

    def test_changed_evidence_offers_one_core_occasion(self):
        workflow = plan(step("ci", completion=(PlanCondition("values.state", "passed"),),
                             waiting=(PlanCondition("values.state", "pending"),)))
        self.outputs["ci"] = [{"state": "pending"}, {"state": "failed"}]
        reasoner = Reasoner(AgentDecision(execution_plan=workflow, goal_id="goal"),
                            AgentDecision(response="CI failed; I will repair it.",
                                          goal_id="goal"))
        agent = self.agent(reasoner)
        agent.process(conversation(), RETENTION, 2)
        self.now += timedelta(seconds=10)
        agent.advance_due_plans(lambda _: conversation())

        class Ledger:
            def __init__(self):
                self.seen = set()

            def exists(self, identifier):
                return identifier in self.seen

            def record_created(self, opportunity):
                self.seen.add(opportunity.opportunity_id)
                return True

        source = PlanContinuationSource(self.store, Ledger(), enabled=True)
        offered = source.due_opportunities()
        self.assertEqual(len(offered), 1)
        self.assertTrue(source.claim(offered[0]))
        self.assertEqual(source.due_opportunities(), ())
        outcome = agent.process(conversation(), RETENTION, 2,
                                resume_plan_goal_id="goal")
        self.assertEqual(outcome.response, "CI failed; I will repair it.")
        self.assertEqual(reasoner.calls, 2)
        self.assertEqual(reasoner.contexts[1].transient_attempts[0].result.values["state"],
                         "failed")

    def test_interrupted_dispatch_is_never_replayed(self):
        workflow = plan(step("merge"))
        pending = CapabilityAttempt(
            workflow.steps[0].call, CapabilityAttemptDisposition.PENDING,
            None, reason_code="dispatch_pending",
        )
        snapshot = self.store.load("goal")
        self.store.replace(
            replace(snapshot.state, execution_plan=workflow, attempts=(pending,)),
            snapshot.retention_until, snapshot.revision,
        )
        self.agent(Reasoner()).advance_due_plans(lambda _: conversation())
        state = self.store.load("goal").state
        self.assertEqual(state.execution_plan.core_reentry_reason, "dispatch_interrupted")
        self.assertEqual(state.execution_plan.cursor, 0)
        self.assertEqual(self.calls, [])

    def test_background_completion_reenters_core_once_for_final_response(self):
        workflow = plan(step("ci", completion=(PlanCondition("values.state", "passed"),),
                             waiting=(PlanCondition("values.state", "pending"),)))
        self.outputs["ci"] = [{"state": "pending"}, {"state": "passed"}]
        reasoner = Reasoner(AgentDecision(execution_plan=workflow, goal_id="goal"),
                            AgentDecision(response="Checks passed.", goal_id="goal"))
        agent = self.agent(reasoner)
        agent.process(conversation(), RETENTION, 2)
        self.now += timedelta(seconds=10)
        agent.advance_due_plans(lambda _: conversation())
        outcome = agent.process(conversation(), RETENTION, 2,
                                resume_plan_goal_id="goal")
        self.assertEqual(outcome.state, CoreState.RESPONDED)
        self.assertEqual(outcome.response, "Checks passed.")
        self.assertEqual(reasoner.calls, 2)
        self.assertEqual(self.store.load("goal").state.execution_plan.status, "handled")

    def test_array_conditions_keep_mixed_ci_pending_without_reasoning(self):
        workflow = plan(step("ci", completion=(
            PlanCondition("values.check_runs.*.status", "completed"),
            PlanCondition("values.check_runs.*.conclusion", "success"),
        ), waiting=(
            PlanCondition("values.check_runs.*.status", "completed", "any", True),
        )), step("merge"))
        self.outputs["ci"] = [
            {"check_runs": [
                {"status": "completed", "conclusion": "success"},
                {"status": "in_progress", "conclusion": None},
            ]},
            {"check_runs": [
                {"status": "completed", "conclusion": "success"},
                {"status": "completed", "conclusion": "success"},
            ]},
        ]
        reasoner = Reasoner(AgentDecision(execution_plan=workflow, goal_id="goal"))
        agent = self.agent(reasoner)
        agent.process(conversation(), RETENTION, 2)
        self.assertEqual(reasoner.calls, 1)
        self.assertEqual(self.calls, ["ci"])
        self.now += timedelta(seconds=10)
        agent.advance_due_plans(lambda _: conversation())
        self.assertEqual(self.calls, ["ci", "ci", "merge"])
        self.assertEqual(reasoner.calls, 1)

    def test_mechanical_blocker_also_blocks_a_new_plan(self):
        self.outputs["request_external_review"] = (
            CapabilityResultState.FAILED,
            {"code": "review_pending", "requires_judgement": True},
        )
        reasoner = Reasoner(
            AgentDecision(call=CapabilityCall("request-1", "request_external_review", {}),
                          goal_id="goal"),
            AgentDecision(execution_plan=plan(step("merge")), goal_id="goal"),
        )
        outcome = self.agent(reasoner).process(
            conversation(), RETENTION, 3,
            origin=CognitionOrigin.EXTERNAL_EVENT,
        )
        self.assertEqual(outcome.reason, "review_pending")
        self.assertEqual(self.calls, ["request_external_review"])
        self.assertIsNone(self.store.load("goal").state.execution_plan)

    def test_waiting_cannot_repeat_a_consequential_capability(self):
        workflow = plan(step("merge", completion=(PlanCondition("values.state", "done"),),
                             waiting=(PlanCondition("values.state", "pending"),)))
        reasoner = Reasoner(AgentDecision(execution_plan=workflow, goal_id="goal"),
                            AgentDecision(response="Waiting requires observation.",
                                          goal_id="goal"))
        self.agent(reasoner, effectful=frozenset({"merge"})).process(
            conversation(), RETENTION, 3,
        )
        self.assertEqual(self.calls, [])
        self.assertIsNone(self.store.load("goal").state.execution_plan)

    def test_missing_wildcard_field_cannot_satisfy_all(self):
        workflow = plan(step("ci", completion=(
            PlanCondition("values.check_runs.*.conclusion", "success"),
        )), step("merge"))
        self.outputs["ci"] = {"check_runs": [{"conclusion": "success"}, {}]}
        reasoner = Reasoner(AgentDecision(execution_plan=workflow, goal_id="goal"),
                            AgentDecision(response="Check evidence is incomplete.",
                                          goal_id="goal"))
        self.agent(reasoner).process(conversation(), RETENTION, 3)
        self.assertEqual(self.calls, ["ci"])
        self.assertEqual(self.store.load("goal").state.execution_plan.core_reentry_reason,
                         "planned_result_unexpected")
