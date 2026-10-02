from __future__ import annotations

import ast
import sys
import tempfile
import unittest
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from alx.contracts import (  # noqa: E402
    AgentDecision, CapabilityAttempt, CapabilityAttemptDisposition,
    CapabilityCall, CapabilityDefinition, CapabilityResult, CapabilityResultState,
    CognitionOrigin, ConversationOrigin, ConversationSnapshot, ConversationTurn, ExecutionPlan,
    ExecutionStep, GoalState, Objective, PlanCondition, SideEffect,
    Evidence, GoalStatus, GoalStopReason, StructuredSchema, SuccessCriterion, ValueKind,
)
from alx.contracts import (  # noqa: E402
    Approval, ApprovalLifecycle, ApprovalScope, GoalMutationKind, GoalProposal, WorkItem,
)
from alx.contracts.continuity import CognitionOpportunity  # noqa: E402
from alx.contracts.pull_request_checks import (  # noqa: E402
    CheckRun, CommitStatus, PullRequestChecks,
)
from alx.contracts.review_content import (  # noqa: E402
    REVIEW_FAILED, REVIEW_IN_PROGRESS, ReviewContent, ReviewReadError,
)
from alx.core import CoreAgent, CoreState  # noqa: E402
from alx.core.loop import REASONING_TURN_WINDOW, project_turns_for_reasoning  # noqa: E402
from alx.core.plan_results import (  # noqa: E402
    PlanResultKind, classify_planned_result, plan_invalidation_facts, reduce_plan,
)
from alx.tools.pull_request_checks import (  # noqa: E402
    DEFINITION as PULL_REQUEST_CHECKS_DEFINITION, READ_PULL_REQUEST_CHECKS,
    build_pull_request_checks_executors,
)
from alx.tools.review_content import (  # noqa: E402
    DEFINITION as REVIEW_CONTENT_DEFINITION, READ_EXTERNAL_REVIEW,
    build_review_content_executors,
)
from alx.continuity.plan_source import PlanContinuationSource  # noqa: E402
from alx.conversation import ConversationGateway, SQLiteConversationStore  # noqa: E402
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


# A person who starts background work hears one response-only step about it.
ACK = AgentDecision(response="Started; I will report back.", goal_id="goal")


class Reasoner:
    def __init__(self, *decisions):
        self.decisions = list(decisions)
        self.calls = 0
        self.contexts = []

    def decide(self, context):
        self.calls += 1
        self.contexts.append(context)
        return self.decisions.pop(0)


class PlanHarness(unittest.TestCase):
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
              review_requires_judgment=False, effectful=frozenset(),
              extra_definitions=(), budget_check=None, plan_continuation=True):
        names = ("coding", "review", "ci", "merge", "sync", "cleanup",
                 "other", "request_external_review")
        definitions = tuple(CapabilityDefinition(
            name, name, SCHEMA, SCHEMA,
            SideEffect.EFFECTFUL if name in effectful else SideEffect.NONE,
            ("review_unavailable",) if name == "review" else (),
            requires_core_judgment=(name == "review" and review_requires_judgment),
            repeat_safe_observation=(name == "review"),
            pending_failure_reasons=(
                (("review_unavailable", "review_in_progress"),) if name == "review" else ()
            ),
        ) for name in names) + tuple(extra_definitions)

        def dispatch(call, _state):
            self.calls.append(call.capability_id)
            value = self.outputs.get(call.capability_id, {"state": "done"})
            if isinstance(value, list):
                value = value.pop(0)
            if callable(value):
                # A production-shaped attempt: a real executor's result, or a
                # broker refusal or failure exactly as the broker builds it.
                return value(call)
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
                         turn_bound_capabilities=turn_bound,
                         budget_check=budget_check,
                         plan_continuation=plan_continuation)

    def reset_goal(self):
        snapshot = self.store.load("goal")
        self.store.replace(replace(snapshot.state, execution_plan=None, attempts=(),
                                   status=GoalStatus.ACTIVE, stop_reason=None,
                                   blockers=(), outstanding_work=(), evidence=()),
                           snapshot.retention_until, snapshot.revision)
        self.calls.clear()


class ExecutionPlanTests(PlanHarness):
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
        reasoner = Reasoner(AgentDecision(execution_plan=workflow, goal_id="goal"), ACK)
        outcome = self.agent(reasoner).process(conversation(), RETENTION, 2)
        self.assertEqual(outcome.reason, "plan_waiting")
        # The person who started it hears one response; the wait continues.
        self.assertEqual(outcome.response, ACK.response)
        self.assertEqual(reasoner.calls, 2)
        self.assertEqual(self.agent(reasoner).advance_due_plans(lambda _: conversation()), 0)
        self.now += timedelta(seconds=10)
        self.assertEqual(self.agent(reasoner).advance_due_plans(lambda _: conversation()), 1)
        self.assertEqual(reasoner.calls, 2)
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
                         "new_person_turn")

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

    def test_review_findings_wake_core_even_when_wait_condition_matches(self):
        workflow = plan(step("review", completion=(
            PlanCondition("values.findings", ()),), waiting=(
            PlanCondition("state", "succeeded"),)), step("merge"))
        self.outputs["review"] = {"findings": ["repair required"]}
        reasoner = Reasoner(AgentDecision(execution_plan=workflow, goal_id="goal"),
                            AgentDecision(response="I will assess the finding.",
                                          goal_id="goal"))
        self.agent(reasoner, review_requires_judgment=True).process(
            conversation(), RETENTION, 3,
        )
        self.assertEqual(self.calls, ["review"])
        self.assertEqual(reasoner.calls, 2)
        self.assertEqual(reasoner.contexts[1].transient_attempts[0].result.values["findings"],
                         ("repair required",))

    def test_pending_external_review_is_mechanical_until_published(self):
        workflow = plan(step("review", completion=(
            PlanCondition("values.available", True),), waiting=(
            PlanCondition("failure.reason", "review_in_progress"),), wake=True))
        self.outputs["review"] = [
            (CapabilityResultState.FAILED,
             {"code": "review_unavailable", "reason": "review_in_progress"}),
            {"available": True},
        ]
        reasoner = Reasoner(AgentDecision(execution_plan=workflow, goal_id="goal"), ACK)
        self.agent(reasoner, effectful=frozenset({"review"})).process(
            conversation(), RETENTION, 2,
        )
        self.assertEqual(reasoner.calls, 2)
        self.assertEqual(self.store.load("goal").state.execution_plan.status, "waiting")
        self.now += timedelta(seconds=10)
        self.agent(reasoner, effectful=frozenset({"review"})).advance_due_plans(
            lambda _: conversation(),
        )
        self.assertEqual(self.calls, ["review", "review"])
        self.assertEqual(self.store.load("goal").state.execution_plan.status, "needs_core")
        self.assertEqual(reasoner.calls, 2)

    def test_failed_test_and_merge_refusal_stop_before_next_step(self):
        for name in ("ci", "merge"):
            with self.subTest(name=name):
                self.reset_goal()
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
        agent = self.agent(reasoner)
        outcome = agent.process(conversation(), RETENTION, 3)
        self.assertEqual(self.store.load("goal").state.execution_plan.status, "completed")
        agent.acknowledge_plan_response(outcome.snapshot)

        class Ledger:
            def exists(self, _identifier):
                return False

        source = PlanContinuationSource(self.store, Ledger(), enabled=True)
        self.assertEqual(source.due_opportunities(), ())
        self.assertEqual(self.store.load("goal").state.execution_plan.status, "handled")

    def test_completed_goal_with_undelivered_plan_response_is_reoffered(self):
        workflow = replace(plan(step("coding")), cursor=1, status="completed")
        snapshot = self.store.load("goal")
        completed = replace(
            snapshot.state,
            status=GoalStatus.COMPLETED,
            stop_reason=GoalStopReason.SUCCESS_CRITERIA_MET,
            evidence=(Evidence("done", "verification", supports=("done",),
                              source_references=("attempt:merge",)),),
            execution_plan=workflow,
        )
        self.store.replace(completed, snapshot.retention_until, snapshot.revision)

        class Ledger:
            def exists(self, _identifier):
                return False

        source = PlanContinuationSource(self.store, Ledger(), enabled=True)
        opportunities = source.due_opportunities()
        self.assertEqual(len(opportunities), 1)
        conversations = SQLiteConversationStore(self.path.with_name("conversation.sqlite3"))
        self.addCleanup(conversations.close)
        created = conversations.create("thread", RETENTION)
        conversations.append(conversation().turns[0], RETENTION, created.revision)
        gateway = ConversationGateway(
            self.agent(Reasoner(AgentDecision(response="Finished.", goal_id="goal"))),
            conversations,
        )
        outcome = gateway.receive_cognition_opportunity(
            "thread", opportunities[0], 1, RETENTION,
        )
        self.assertEqual(outcome.response, "Finished.")
        self.assertEqual(conversations.load("thread").turns[-1].content, "Finished.")
        self.assertEqual(self.store.load("goal").state.execution_plan.status, "handled")

    def test_spent_continuation_is_retained_and_reopened_with_new_generation(self):
        class Ledger:
            def __init__(self):
                self.rows = {}
                self.unreconciled = set()

            def exists(self, identifier):
                return identifier in self.rows

            def record_created(self, opportunity):
                self.rows[opportunity.opportunity_id] = {
                    "opportunity_id": opportunity.opportunity_id,
                    "refs": "\x1f".join(opportunity.references),
                }
                return True

            def unfinished(self):
                return tuple(self.rows.values())

            def mark_unreconciled(self, identifier):
                self.unreconciled.add(identifier)

            def release(self, identifier):
                self.rows.pop(identifier, None)

        class Spend:
            def dispatch_started(self, _identifier):
                return True

        workflow = replace(plan(step("coding")), cursor=1, status="completed")
        snapshot = self.store.load("goal")
        self.store.replace(replace(snapshot.state, execution_plan=workflow),
                           snapshot.retention_until, snapshot.revision)
        ledger = Ledger()
        source = PlanContinuationSource(self.store, ledger, enabled=True)
        first = source.due_opportunities()[0]
        source.claim(first)
        self.assertEqual(source.recover(Spend()), ())
        self.assertIn(first.opportunity_id, ledger.unreconciled)
        self.assertIn(first.opportunity_id, ledger.rows)
        recovered_plan = self.store.load("goal").state.execution_plan
        self.assertEqual(recovered_plan.continuation_generation, 1)
        second = source.due_opportunities()
        self.assertEqual(len(second), 1)
        self.assertNotEqual(second[0].opportunity_id, first.opportunity_id)

    def test_intermediate_core_decision_does_not_consume_plan_continuation(self):
        workflow = plan(step("coding"))
        reasoner = Reasoner(AgentDecision(execution_plan=workflow, goal_id="goal"),
                            AgentDecision(goal_id="goal"),
                            AgentDecision(response="Done.", goal_id="goal"))
        agent = self.agent(reasoner)
        outcome = agent.process(conversation(), RETENTION, 2)
        self.assertEqual(outcome.reason, "goal_selection_redundant")
        self.assertEqual(self.store.load("goal").state.execution_plan.status, "completed")

        class Ledger:
            def exists(self, _identifier):
                return False

        source = PlanContinuationSource(self.store, Ledger(), enabled=True)
        self.assertEqual(len(source.due_opportunities()), 1)
        outcome = agent.process(conversation(), RETENTION, 1,
                                resume_plan_goal_id="goal")
        self.assertEqual(outcome.response, "Done.")
        self.assertEqual(self.store.load("goal").state.execution_plan.status, "completed")
        agent.acknowledge_plan_response(outcome.snapshot)
        self.assertEqual(self.store.load("goal").state.execution_plan.status, "handled")

    def test_gateway_handles_plan_only_after_response_is_stored(self):
        workflow = replace(plan(step("coding")), cursor=1, status="completed")
        snapshot = self.store.load("goal")
        self.store.replace(replace(snapshot.state, execution_plan=workflow),
                           snapshot.retention_until, snapshot.revision)
        conversations = SQLiteConversationStore(self.path.with_name("conversation.sqlite3"))
        self.addCleanup(conversations.close)
        gateway = ConversationGateway(
            self.agent(Reasoner(AgentDecision(response="Done.", goal_id="goal"))),
            conversations,
        )
        gateway.receive_conversation_turn(conversation().turns[0], 1, RETENTION)
        self.assertEqual(conversations.load("thread").turns[-1].content, "Done.")
        self.assertEqual(self.store.load("goal").state.execution_plan.status, "handled")

    def test_failed_response_persistence_keeps_plan_continuation(self):
        workflow = replace(plan(step("coding")), cursor=1, status="completed")
        snapshot = self.store.load("goal")
        self.store.replace(replace(snapshot.state, execution_plan=workflow),
                           snapshot.retention_until, snapshot.revision)
        conversations = SQLiteConversationStore(self.path.with_name("conversation.sqlite3"))
        self.addCleanup(conversations.close)
        gateway = ConversationGateway(
            self.agent(Reasoner(AgentDecision(response="Done.", goal_id="goal"))),
            conversations,
        )
        original_append = conversations.append

        def append(turn, retention_until, expected_revision):
            if turn.origin is ConversationOrigin.ALX_RESPONSE:
                raise RuntimeError("response store unavailable")
            return original_append(turn, retention_until, expected_revision)

        with patch.object(conversations, "append", side_effect=append):
            with self.assertRaisesRegex(RuntimeError, "response store unavailable"):
                gateway.receive_conversation_turn(conversation().turns[0], 1, RETENTION)
        self.assertEqual(self.store.load("goal").state.execution_plan.status, "completed")

    def test_changed_evidence_offers_one_core_occasion(self):
        workflow = plan(step("ci", completion=(PlanCondition("values.state", "passed"),),
                             waiting=(PlanCondition("values.state", "pending"),)))
        self.outputs["ci"] = [{"state": "pending"}, {"state": "failed"}]
        reasoner = Reasoner(AgentDecision(execution_plan=workflow, goal_id="goal"), ACK,
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
        self.assertEqual(reasoner.calls, 3)
        self.assertEqual(reasoner.contexts[2].transient_attempts[0].result.values["state"],
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
        reasoner = Reasoner(AgentDecision(execution_plan=workflow, goal_id="goal"), ACK,
                            AgentDecision(response="Checks passed.", goal_id="goal"))
        agent = self.agent(reasoner)
        agent.process(conversation(), RETENTION, 2)
        self.now += timedelta(seconds=10)
        agent.advance_due_plans(lambda _: conversation())
        outcome = agent.process(conversation(), RETENTION, 2,
                                resume_plan_goal_id="goal")
        self.assertEqual(outcome.state, CoreState.RESPONDED)
        self.assertEqual(outcome.response, "Checks passed.")
        self.assertEqual(reasoner.calls, 3)
        agent.acknowledge_plan_response(outcome.snapshot)
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
        reasoner = Reasoner(AgentDecision(execution_plan=workflow, goal_id="goal"), ACK)
        agent = self.agent(reasoner)
        agent.process(conversation(), RETENTION, 2)
        self.assertEqual(reasoner.calls, 2)
        self.assertEqual(self.calls, ["ci"])
        self.now += timedelta(seconds=10)
        agent.advance_due_plans(lambda _: conversation())
        self.assertEqual(self.calls, ["ci", "ci", "merge"])
        self.assertEqual(reasoner.calls, 2)

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

    def test_planned_review_failure_blocks_follow_up_action(self):
        self.outputs["request_external_review"] = (
            CapabilityResultState.FAILED,
            {"code": "review_pending", "requires_judgement": True},
        )
        workflow = plan(step("request_external_review"), step("merge"))
        reasoner = Reasoner(
            AgentDecision(execution_plan=workflow, goal_id="goal"),
            AgentDecision(call=CapabilityCall("merge-after-review", "merge", {}),
                          goal_id="goal"),
        )
        outcome = self.agent(reasoner).process(
            conversation(), RETENTION, 2, origin=CognitionOrigin.EXTERNAL_EVENT,
        )
        self.assertEqual(outcome.reason, "review_pending")
        self.assertEqual(self.calls, ["request_external_review"])
        self.assertEqual(self.store.load("goal").state.execution_plan.status, "needs_core")

    def test_planned_review_blocker_survives_restart(self):
        self.outputs["request_external_review"] = (
            CapabilityResultState.FAILED,
            {"code": "review_pending", "requires_judgement": True},
        )
        workflow = plan(step("request_external_review"), step("merge"))
        self.agent(Reasoner(AgentDecision(execution_plan=workflow, goal_id="goal"))).process(
            conversation(), RETENTION, 1, origin=CognitionOrigin.EXTERNAL_EVENT,
        )
        self.store.close()
        self.store = SQLiteGoalStore(self.path)
        reasoner = Reasoner(AgentDecision(
            call=CapabilityCall("merge-after-restart", "merge", {}), goal_id="goal",
        ))
        outcome = self.agent(reasoner).process(
            conversation(), RETENTION, 1, origin=CognitionOrigin.EXTERNAL_EVENT,
            resume_plan_goal_id="goal",
        )
        self.assertEqual(outcome.reason, "review_pending")
        self.assertEqual(self.calls, ["request_external_review"])

    def test_planned_review_blocker_applies_after_ordinary_goal_selection(self):
        self.outputs["request_external_review"] = (
            CapabilityResultState.FAILED,
            {"code": "review_pending", "requires_judgement": True},
        )
        workflow = plan(step("request_external_review"), step("merge"))
        self.agent(Reasoner(AgentDecision(execution_plan=workflow, goal_id="goal"))).process(
            conversation(), RETENTION, 1, origin=CognitionOrigin.EXTERNAL_EVENT,
        )
        reasoner = Reasoner(
            AgentDecision(goal_id="goal"),
            AgentDecision(call=CapabilityCall("merge-after-selection", "merge", {}),
                          goal_id="goal"),
        )
        outcome = self.agent(reasoner).process(
            conversation(), RETENTION, 2, origin=CognitionOrigin.EXTERNAL_EVENT,
        )
        self.assertEqual(outcome.reason, "review_pending")
        self.assertEqual(self.calls, ["request_external_review"])

    def test_planned_review_blocker_applies_to_action_in_selection_decision(self):
        self.outputs["request_external_review"] = (
            CapabilityResultState.FAILED,
            {"code": "review_pending", "requires_judgement": True},
        )
        workflow = plan(step("request_external_review"), step("merge"))
        self.agent(Reasoner(AgentDecision(execution_plan=workflow, goal_id="goal"))).process(
            conversation(), RETENTION, 1, origin=CognitionOrigin.EXTERNAL_EVENT,
        )
        reasoner = Reasoner(AgentDecision(
            call=CapabilityCall("merge-with-selection", "merge", {}), goal_id="goal",
        ))
        outcome = self.agent(reasoner).process(
            conversation(), RETENTION, 1, origin=CognitionOrigin.EXTERNAL_EVENT,
        )
        self.assertEqual(outcome.reason, "review_pending")
        self.assertEqual(self.calls, ["request_external_review"])

    def test_review_blocker_survives_crash_before_plan_checkpoint(self):
        workflow = plan(step("request_external_review"), step("merge"))
        call = workflow.steps[0].call
        failed = CapabilityResult(
            call.call_id, call.capability_id, CapabilityResultState.FAILED, {},
            {"code": "review_pending", "requires_judgement": True},
        )
        attempt = CapabilityAttempt(call, CapabilityAttemptDisposition.EXECUTED,
                                    True, failed)
        snapshot = self.store.load("goal")
        self.store.replace(
            replace(snapshot.state, execution_plan=workflow, attempts=(attempt,)),
            snapshot.retention_until, snapshot.revision,
        )
        reasoner = Reasoner(AgentDecision(
            call=CapabilityCall("merge-after-crash", "merge", {}), goal_id="goal",
        ))
        outcome = self.agent(reasoner).process(
            conversation(), RETENTION, 1, origin=CognitionOrigin.EXTERNAL_EVENT,
            resume_plan_goal_id="goal",
        )
        self.assertEqual(outcome.reason, "review_pending")
        self.assertEqual(self.calls, [])
        self.assertEqual(self.store.load("goal").state.execution_plan.core_reentry_reason,
                         "plan_result_uncheckpointed")

    def test_waiting_failure_recorded_before_crash_is_not_polled_again(self):
        workflow = plan(step("ci", completion=(
            PlanCondition("values.state", "passed"),), waiting=(
            PlanCondition("values.state", "pending"),)))
        first_call = workflow.steps[0].call
        retry_call = replace(first_call, call_id="ci-poll-2")
        pending = CapabilityAttempt(
            first_call, CapabilityAttemptDisposition.EXECUTED, True,
            CapabilityResult(first_call.call_id, "ci", CapabilityResultState.SUCCEEDED,
                             {"state": "pending"}),
        )
        failed = CapabilityAttempt(
            retry_call, CapabilityAttemptDisposition.EXECUTED, True,
            CapabilityResult(retry_call.call_id, "ci", CapabilityResultState.SUCCEEDED,
                             {"state": "failed"}),
        )
        waiting = replace(workflow, status="waiting", next_due_at=NOW,
                          last_result_call_id=first_call.call_id)
        snapshot = self.store.load("goal")
        self.store.replace(
            replace(snapshot.state, execution_plan=waiting,
                    attempts=(pending, failed)),
            snapshot.retention_until, snapshot.revision,
        )
        self.store.close()
        self.store = SQLiteGoalStore(self.path)
        self.agent(Reasoner()).advance_due_plans(lambda _: conversation())
        stored = self.store.load("goal").state.execution_plan
        self.assertEqual(stored.status, "needs_core")
        self.assertEqual(stored.core_reentry_reason, "plan_result_uncheckpointed")
        self.assertEqual(self.calls, [])

    def test_crash_gap_blocker_applies_to_ordinary_goal_selection(self):
        workflow = plan(step("request_external_review"), step("merge"))
        call = workflow.steps[0].call
        failed = CapabilityResult(
            call.call_id, call.capability_id, CapabilityResultState.FAILED, {},
            {"code": "review_pending", "requires_judgement": True},
        )
        attempt = CapabilityAttempt(call, CapabilityAttemptDisposition.EXECUTED,
                                    True, failed)
        snapshot = self.store.load("goal")
        self.store.replace(
            replace(snapshot.state, execution_plan=workflow, attempts=(attempt,)),
            snapshot.retention_until, snapshot.revision,
        )
        reasoner = Reasoner(AgentDecision(
            call=CapabilityCall("merge-after-crash-selection", "merge", {}),
            goal_id="goal",
        ))
        outcome = self.agent(reasoner).process(
            conversation(), RETENTION, 1, origin=CognitionOrigin.EXTERNAL_EVENT,
        )
        self.assertEqual(outcome.reason, "review_pending")
        self.assertEqual(self.calls, [])
        self.assertEqual(self.store.load("goal").state.execution_plan.status, "needs_core")

    def test_blocker_survives_switch_to_another_goal(self):
        self.store.create(replace(goal(), goal_id="goal-b",
                                  execution_plan=plan(step("other"))),
                          "thread", RETENTION)
        self.outputs["request_external_review"] = (
            CapabilityResultState.FAILED,
            {"code": "review_pending", "requires_judgement": True},
        )
        reasoner = Reasoner(
            AgentDecision(call=CapabilityCall("review-a", "request_external_review", {}),
                          goal_id="goal"),
            AgentDecision(call=CapabilityCall("merge-b", "merge", {}),
                          goal_id="goal-b"),
        )
        outcome = self.agent(reasoner).process(
            conversation(), RETENTION, 2, origin=CognitionOrigin.EXTERNAL_EVENT,
        )
        self.assertEqual(outcome.reason, "review_pending")
        self.assertEqual(self.calls, ["request_external_review"])
        self.assertEqual(self.store.load("goal-b").state.execution_plan.status, "ready")

    def test_ordinary_selection_carries_available_plan_review_evidence(self):
        workflow = plan(step("review"))
        self.outputs["review"] = {"findings": ["repair required"]}
        reasoner = Reasoner(
            AgentDecision(execution_plan=workflow, goal_id="goal"),
            AgentDecision(goal_id="goal"),
            AgentDecision(response="I will assess the review.", goal_id="goal"),
        )
        agent = self.agent(reasoner, review_requires_judgment=True)
        agent.process(conversation(), RETENTION, 1,
                      origin=CognitionOrigin.EXTERNAL_EVENT)
        agent.process(conversation(), RETENTION, 2,
                      origin=CognitionOrigin.EXTERNAL_EVENT)
        self.assertEqual(reasoner.contexts[2].transient_attempts[0].result.values["findings"],
                         ("repair required",))

    def test_reinstalled_model_plan_id_gets_new_continuation_identity(self):
        class Ledger:
            def __init__(self):
                self.seen = set()

            def exists(self, identifier):
                return identifier in self.seen

            def record_created(self, opportunity):
                self.seen.add(opportunity.opportunity_id)
                return True

        ledger = Ledger()
        source = PlanContinuationSource(self.store, ledger, enabled=True)
        reasoner = Reasoner(
            AgentDecision(execution_plan=plan(step("coding")), goal_id="goal"),
            AgentDecision(execution_plan=plan(step("other")), goal_id="goal"),
        )
        agent = self.agent(reasoner)
        agent.process(conversation(), RETENTION, 1)
        first = source.due_opportunities()
        self.assertEqual(len(first), 1)
        self.assertTrue(source.claim(first[0]))
        agent.process(conversation(), RETENTION, 1)
        second = source.due_opportunities()
        self.assertEqual(len(second), 1)
        self.assertNotEqual(second[0].opportunity_id, first[0].opportunity_id)
        self.assertEqual(self.calls, ["coding", "other"])

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


HEAD = "a" * 40


def check_run(name, status, conclusion):
    return CheckRun(name, status, conclusion, "2026-10-01T00:00:00Z",
                    None if status != "completed" else "2026-10-01T00:05:00Z",
                    f"https://github.com/checks/{name}", "github-actions",
                    "GitHub Actions", None, None)


def checks_result(check_runs=(), commit_statuses=()):
    """The real check-read executor over one provider answer."""
    def attempt(call):
        executor = build_pull_request_checks_executors(
            lambda _request: PullRequestChecks(95, HEAD, check_runs, commit_statuses),
            lambda: call.call_id,
        )[READ_PULL_REQUEST_CHECKS]
        return CapabilityAttempt(call, CapabilityAttemptDisposition.EXECUTED, True,
                                 executor(call.arguments))
    return attempt


def review_result(answer):
    """The real review-read executor; `answer` is content or an exception."""
    def provider(_request):
        if isinstance(answer, Exception):
            raise answer
        return answer

    def attempt(call):
        executor = build_review_content_executors(
            provider, lambda: call.call_id)[READ_EXTERNAL_REVIEW]
        return CapabilityAttempt(call, CapabilityAttemptDisposition.EXECUTED, True,
                                 executor(call.arguments))
    return attempt


def unavailable_review(reason):
    return ReviewContent(95, HEAD, "reviewer", False, unavailable_reason=reason)


def published_review():
    return ReviewContent(95, HEAD, "reviewer", True, summary="One defect in the reducer.",
                         submitted_at=NOW, retrieved_at=NOW)


def refused(call):
    return CapabilityAttempt(call, CapabilityAttemptDisposition.REJECTED, False,
                             reason_code="approval_required")


def broker_failure(code, invoked):
    def attempt(call):
        return CapabilityAttempt(
            call, CapabilityAttemptDisposition.BROKER_FAILURE, invoked,
            CapabilityResult(call.call_id, call.capability_id,
                             CapabilityResultState.FAILED, failure={"code": code}),
            code,
        )
    return attempt


CHECK_ARGS = {"pull_request_number": 95, "head_sha": HEAD}


def check_step(*, completion, waiting):
    return ExecutionStep(CapabilityCall("call-checks", READ_PULL_REQUEST_CHECKS, CHECK_ARGS),
                         completion, waiting, 10)


def review_step(*, completion=(PlanCondition("values.available", True),),
                waiting=(PlanCondition("failure.reason", "review_in_progress"),)):
    return ExecutionStep(CapabilityCall("call-review", READ_EXTERNAL_REVIEW, CHECK_ARGS),
                         completion, waiting, 10 if waiting else 0)


RUNS_DONE = (PlanCondition("values.check_runs.*.status", "completed"),
             PlanCondition("values.check_runs.*.conclusion", "success"))
RUNS_PENDING = (PlanCondition("values.check_runs.*.status", "completed", "any", True),)
STATUSES_DONE = (PlanCondition("values.commit_statuses.*.state", "success"),)
STATUSES_PENDING = (PlanCondition("values.commit_statuses.*.state", "pending", "any"),)


class PlanResultClassifierTests(unittest.TestCase):
    """The precedence, exercised directly on the one classifier."""

    def classify(self, attempt, step_=None, definition=None, **options):
        step_ = step_ or step("ci", completion=(PlanCondition("state", "succeeded"),),
                              waiting=(PlanCondition("state", "succeeded", negate=True),))
        return classify_planned_result(step_, attempt, definition, **options)

    def test_refusal_can_neither_advance_nor_wait(self):
        call = CapabilityCall("call-ci", "ci", {})
        # A refusal carries no result, so a negated waiting condition sees a
        # missing state as "not succeeded". It still never waits.
        outcome = self.classify(refused(call))
        self.assertIs(outcome.kind, PlanResultKind.WAKE_CORE)
        self.assertEqual(outcome.facts[0], "planned_call_refused")

    def test_broker_failure_can_neither_advance_nor_wait(self):
        call = CapabilityCall("call-ci", "ci", {})
        waits_on_code = step("ci", completion=(PlanCondition("state", "succeeded"),),
                             waiting=(PlanCondition("failure.code", "executor_error"),))
        for invoked, fact in ((False, "planned_dispatch_failed"), (True, "dispatch_uncertain")):
            with self.subTest(invoked=invoked):
                outcome = self.classify(broker_failure("executor_error", invoked)(call),
                                        waits_on_code)
                self.assertIs(outcome.kind, PlanResultKind.WAKE_CORE)
                self.assertEqual(outcome.facts[0], fact)

    def test_judgement_outranks_completion_and_waiting(self):
        call = CapabilityCall("call-ci", "ci", {})
        judged = CapabilityDefinition("ci", "ci", SCHEMA, SCHEMA, SideEffect.NONE,
                                      requires_core_judgment=True)
        succeeded = CapabilityAttempt(call, CapabilityAttemptDisposition.EXECUTED, True,
                                      CapabilityResult("call-ci", "ci",
                                                       CapabilityResultState.SUCCEEDED,
                                                       {"state": "passed"}))
        completes = step("ci", completion=(PlanCondition("values.state", "passed"),))
        self.assertIs(self.classify(succeeded, completes, judged).kind,
                      PlanResultKind.WAKE_CORE)
        flagged = CapabilityAttempt(call, CapabilityAttemptDisposition.EXECUTED, True,
                                    CapabilityResult("call-ci", "ci",
                                                     CapabilityResultState.FAILED, {},
                                                     {"code": "pending",
                                                      "requires_judgement": True}))
        waits = step("ci", completion=(PlanCondition("state", "succeeded"),),
                     waiting=(PlanCondition("failure.code", "pending"),))
        outcome = self.classify(flagged, waits)
        self.assertIs(outcome.kind, PlanResultKind.WAKE_CORE)
        self.assertIn("planned_evidence_requires_judgement", outcome.facts)

    def test_partial_missing_and_conflicting_results_wake_core(self):
        call = CapabilityCall("call-ci", "ci", {})
        both = step("ci", completion=(PlanCondition("values.state", "passed"),),
                    waiting=(PlanCondition("values.state", "pending", negate=True),))
        cases = {
            "planned_result_partial": CapabilityAttempt(
                call, CapabilityAttemptDisposition.EXECUTED, True,
                CapabilityResult("call-ci", "ci", CapabilityResultState.PARTIAL,
                                 {"state": "passed"})),
            # A declared field absent from the evidence satisfies nothing.
            "planned_result_unexpected": CapabilityAttempt(
                call, CapabilityAttemptDisposition.EXECUTED, True,
                CapabilityResult("call-ci", "ci", CapabilityResultState.SUCCEEDED,
                                 {"status": "passed"})),
            "planned_result_conflicting": CapabilityAttempt(
                call, CapabilityAttemptDisposition.EXECUTED, True,
                CapabilityResult("call-ci", "ci", CapabilityResultState.SUCCEEDED,
                                 {"state": "passed"})),
        }
        for fact, attempt in cases.items():
            with self.subTest(fact=fact):
                outcome = self.classify(attempt, both)
                self.assertIs(outcome.kind, PlanResultKind.WAKE_CORE)
                self.assertEqual(outcome.facts, (fact,))

    def test_succeeded_envelope_with_failed_check_is_not_plan_success(self):
        call = CapabilityCall("call-checks", READ_PULL_REQUEST_CHECKS, CHECK_ARGS)
        attempt = checks_result((check_run("unit", "completed", "failure"),))(call)
        self.assertIs(attempt.result.state, CapabilityResultState.SUCCEEDED)
        outcome = self.classify(attempt, check_step(completion=RUNS_DONE, waiting=()),
                                CHECKS_DEFINITION)
        self.assertIs(outcome.kind, PlanResultKind.WAKE_CORE)

    def test_invalidation_outranks_a_successful_result(self):
        call = CapabilityCall("call-ci", "ci", {})
        succeeded = CapabilityAttempt(call, CapabilityAttemptDisposition.EXECUTED, True,
                                      CapabilityResult("call-ci", "ci",
                                                       CapabilityResultState.SUCCEEDED, {}))
        outcome = self.classify(succeeded, step("ci"),
                                invalidation=("plan_precondition_changed",))
        self.assertEqual(outcome.facts[0], "plan_precondition_changed")
        self.assertIs(outcome.kind, PlanResultKind.WAKE_CORE)

    def test_cancelled_goal_and_withdrawn_approval_invalidate_the_plan(self):
        workflow = plan(step("ci"))
        cancelled = replace(goal(), status=GoalStatus.CANCELLED,
                            stop_reason=GoalStopReason.CANCELLED)
        self.assertIn("goal_inactive",
                      plan_invalidation_facts(workflow, cancelled, "person-1", NOW))
        approved = ExecutionStep(CapabilityCall("call-merge", "merge", {}, "approval-1"),
                                 (PlanCondition("state", "succeeded"),))
        for lifecycle, expires in ((ApprovalLifecycle.WITHDRAWN, None),
                                   (ApprovalLifecycle.GRANTED, NOW)):
            with self.subTest(lifecycle=lifecycle):
                state = replace(goal(), approvals=(
                    Approval("approval-1", ApprovalScope("merge", {}), lifecycle, expires),))
                self.assertEqual(
                    plan_invalidation_facts(plan(approved), state, "person-1", NOW,
                                            next_step=approved),
                    ("plan_approval_invalid",))


CHECKS_DEFINITION = PULL_REQUEST_CHECKS_DEFINITION


class PlanResultInvariantTests(PlanHarness):
    """Every invariant the PR #95 audit found missing, through the real Core."""

    def production_agent(self, reasoner, **options):
        return self.agent(reasoner, extra_definitions=(
            CHECKS_DEFINITION, REVIEW_CONTENT_DEFINITION,
        ), **options)

    def install(self, workflow, *later_decisions, budget=1, **options):
        reasoner = Reasoner(AgentDecision(execution_plan=workflow, goal_id="goal"),
                            *later_decisions)
        agent = self.production_agent(reasoner, **options)
        outcome = agent.process(conversation(), RETENTION, budget)
        return agent, reasoner, outcome

    def plan_state(self):
        return self.store.load("goal").state.execution_plan

    def test_rejected_and_broker_failure_neither_advance_nor_wait(self):
        cases = {
            "planned_call_refused": refused,
            "planned_dispatch_failed": broker_failure("implementation_missing", False),
            "dispatch_uncertain": broker_failure("executor_error", True),
        }
        for fact, output in cases.items():
            with self.subTest(fact=fact):
                self.reset_goal()
                self.outputs["ci"] = output
                workflow = replace(plan(
                    step("ci", completion=(PlanCondition("state", "succeeded"),),
                         waiting=(PlanCondition("state", "succeeded", negate=True),)),
                    step("merge")), plan_id=f"plan-{fact}")
                self.install(workflow)
                stored = self.plan_state()
                self.assertEqual(stored.status, "needs_core")
                self.assertEqual(stored.core_reentry_reason, fact)
                self.assertIsNone(stored.next_due_at)
                self.assertEqual(stored.cursor, 0)
                self.assertEqual(self.calls, ["ci"])

    def test_failed_check_run_beside_pending_one_wakes_core_immediately(self):
        self.outputs[READ_PULL_REQUEST_CHECKS] = checks_result((
            check_run("unit", "completed", "failure"),
            check_run("lint", "in_progress", None),
        ))
        workflow = plan(check_step(completion=RUNS_DONE, waiting=RUNS_PENDING), step("merge"))
        self.install(workflow)
        stored = self.plan_state()
        self.assertEqual(stored.status, "needs_core")
        self.assertEqual(stored.core_reentry_reason, "planned_check_failed")
        self.assertIsNone(stored.next_due_at)
        self.assertEqual(self.calls, [READ_PULL_REQUEST_CHECKS])

    def test_failed_commit_status_beside_pending_one_wakes_core_immediately(self):
        for failed in ("failure", "error"):
            with self.subTest(state=failed):
                self.reset_goal()
                self.outputs[READ_PULL_REQUEST_CHECKS] = checks_result(commit_statuses=(
                    CommitStatus("ci/unit", failed, "Unit tests", None),
                    CommitStatus("ci/lint", "pending", "Lint", None),
                ))
                workflow = replace(plan(
                    check_step(completion=STATUSES_DONE, waiting=STATUSES_PENDING),
                    step("merge")), plan_id=f"plan-{failed}")
                self.install(workflow)
                stored = self.plan_state()
                self.assertEqual(stored.core_reentry_reason, "planned_check_failed")
                self.assertEqual(self.calls, [READ_PULL_REQUEST_CHECKS])

    def test_pending_status_the_plan_did_not_declare_wakes_core(self):
        # Waiting covers only what the plan named. A commit status it did not
        # declare pending is settled, and an unsettled one is ambiguous.
        self.outputs[READ_PULL_REQUEST_CHECKS] = checks_result(
            (check_run("lint", "in_progress", None),),
            (CommitStatus("ci/unit", "pending", "Unit tests", None),),
        )
        workflow = plan(check_step(completion=(*RUNS_DONE, *STATUSES_DONE),
                                   waiting=RUNS_PENDING), step("merge"))
        self.install(workflow)
        self.assertEqual(self.plan_state().status, "needs_core")
        self.assertEqual(self.calls, [READ_PULL_REQUEST_CHECKS])

    def test_failed_commit_status_beside_pending_check_run_wakes_core(self):
        self.outputs[READ_PULL_REQUEST_CHECKS] = checks_result(
            (check_run("lint", "queued", None),),
            (CommitStatus("ci/unit", "failure", "Unit tests", None),),
        )
        workflow = plan(check_step(completion=(*RUNS_DONE, *STATUSES_DONE),
                                   waiting=RUNS_PENDING), step("merge"))
        self.install(workflow)
        self.assertEqual(self.plan_state().core_reentry_reason, "planned_check_failed")
        self.assertEqual(self.calls, [READ_PULL_REQUEST_CHECKS])

    def test_only_pending_checks_wait_then_success_advances(self):
        self.outputs[READ_PULL_REQUEST_CHECKS] = [
            checks_result((check_run("unit", "completed", "success"),
                           check_run("lint", "in_progress", None)),
                          (CommitStatus("ci/x", "pending", None, None),)),
            checks_result((check_run("unit", "completed", "success"),
                           check_run("lint", "completed", "success")),
                          (CommitStatus("ci/x", "success", None, None),)),
        ]
        workflow = plan(check_step(completion=(*RUNS_DONE, *STATUSES_DONE),
                                   waiting=(*RUNS_PENDING, *STATUSES_PENDING)),
                        step("merge"))
        agent, reasoner, _ = self.install(workflow, ACK)
        self.assertEqual(self.plan_state().status, "waiting")
        self.now += timedelta(seconds=10)
        agent.advance_due_plans(lambda _: conversation())
        self.assertEqual(self.calls, [READ_PULL_REQUEST_CHECKS, READ_PULL_REQUEST_CHECKS,
                                      "merge"])
        self.assertEqual(self.plan_state().status, "completed")
        self.assertEqual(reasoner.calls, 2)

    def test_production_review_unavailable_distinguishes_its_reasons(self):
        cases = (
            ("in_progress", unavailable_review(REVIEW_IN_PROGRESS), "waiting", None),
            ("failed", unavailable_review(REVIEW_FAILED), "needs_core",
             "planned_result_failed"),
            ("transport", ReviewReadError("review_unavailable"), "needs_core",
             "planned_result_failed"),
            ("unclassified", RuntimeError("socket closed"), "needs_core",
             "planned_result_failed"),
        )
        for name, answer, status, reason in cases:
            with self.subTest(case=name):
                self.reset_goal()
                self.outputs[READ_EXTERNAL_REVIEW] = review_result(answer)
                workflow = replace(plan(review_step(), step("merge")), plan_id=f"plan-{name}")
                self.install(workflow)
                stored = self.plan_state()
                self.assertEqual(stored.status, status)
                self.assertEqual(stored.core_reentry_reason, reason)
                self.assertEqual(self.calls, [READ_EXTERNAL_REVIEW])
                if status == "needs_core":
                    self.assertIn("planned_evidence_requires_judgement",
                                  stored.core_reentry_facts)
                    self.assertEqual(stored.mechanical_blocker, "review_unavailable")
                else:
                    self.assertIsNone(stored.mechanical_blocker)

    def test_review_in_progress_with_undeclared_detail_wakes_core(self):
        def detailed(call):
            return CapabilityAttempt(call, CapabilityAttemptDisposition.EXECUTED, True,
                                     CapabilityResult(call.call_id, READ_EXTERNAL_REVIEW,
                                                      CapabilityResultState.FAILED,
                                                      failure={"code": "review_unavailable",
                                                               "reason": REVIEW_IN_PROGRESS,
                                                               "requires_judgement": True,
                                                               "round": "superseded"}))
        self.outputs[READ_EXTERNAL_REVIEW] = detailed
        self.install(plan(review_step(), step("merge")))
        self.assertEqual(self.plan_state().status, "needs_core")

    def test_requires_judgement_outranks_matching_wait_and_completion(self):
        self.outputs[READ_EXTERNAL_REVIEW] = review_result(published_review())
        workflow = plan(review_step(waiting=(PlanCondition("state", "succeeded"),)),
                        step("merge"))
        self.install(workflow)
        stored = self.plan_state()
        self.assertEqual(stored.status, "needs_core")
        self.assertIn("planned_evidence_requires_judgement", stored.core_reentry_facts)
        self.assertEqual(self.calls, [READ_EXTERNAL_REVIEW])

    def test_crash_after_result_storage_never_redispatches_effectful_step(self):
        workflow = plan(step("merge"), step("cleanup"))
        call = workflow.steps[0].call
        stored = CapabilityAttempt(call, CapabilityAttemptDisposition.EXECUTED, True,
                                   CapabilityResult(call.call_id, "merge",
                                                    CapabilityResultState.SUCCEEDED,
                                                    {"state": "done"}))
        snapshot = self.store.load("goal")
        self.store.replace(replace(snapshot.state, execution_plan=workflow,
                                   attempts=(stored,)),
                           snapshot.retention_until, snapshot.revision)
        self.store.close()
        self.store = SQLiteGoalStore(self.path)
        agent = self.agent(Reasoner(AgentDecision(response="Merged; verifying.",
                                                  goal_id="goal")),
                           effectful=frozenset({"merge"}))
        agent.advance_due_plans(lambda _: conversation())
        agent.advance_due_plans(lambda _: conversation())
        outcome = agent.process(conversation(), RETENTION, 1, resume_plan_goal_id="goal")
        self.assertEqual(self.calls, [])
        result = self.plan_state()
        self.assertEqual(result.cursor, 0)
        self.assertEqual(result.core_reentry_reason, "plan_result_uncheckpointed")
        self.assertEqual(result.last_result_call_id, call.call_id)
        self.assertEqual(outcome.response, "Merged; verifying.")

    def test_interrupted_wait_and_changed_precondition_wake_once_with_both(self):
        waiting = replace(
            plan(step("ci", completion=(PlanCondition("values.state", "passed"),),
                      waiting=(PlanCondition("values.state", "pending"),))),
            context_preconditions={"head": "a" * 40}, status="waiting",
            next_due_at=NOW + timedelta(seconds=10), last_result_call_id="call-ci",
        )
        first = CapabilityAttempt(
            waiting.steps[0].call, CapabilityAttemptDisposition.EXECUTED, True,
            CapabilityResult("call-ci", "ci", CapabilityResultState.SUCCEEDED,
                             {"state": "pending"}))
        poll = CapabilityAttempt(CapabilityCall("ci-poll-2", "ci", {}),
                                 CapabilityAttemptDisposition.PENDING, None,
                                 reason_code="dispatch_pending")
        snapshot = self.store.load("goal")
        self.store.replace(replace(snapshot.state, context={"head": "b" * 40},
                                   execution_plan=waiting, attempts=(first, poll)),
                           snapshot.retention_until, snapshot.revision)
        self.store.close()
        self.store = SQLiteGoalStore(self.path)
        self.agent(Reasoner()).advance_due_plans(lambda _: conversation())
        stored = self.plan_state()
        self.assertEqual(stored.status, "needs_core")
        self.assertEqual(stored.core_reentry_reason, "plan_precondition_changed")
        self.assertIn("dispatch_interrupted", stored.core_reentry_facts)
        self.assertEqual(self.calls, [])

        class Ledger:
            def exists(self, _identifier):
                return False

        offered = PlanContinuationSource(self.store, Ledger(), enabled=True).due_opportunities()
        self.assertEqual(len(offered), 1)

    def test_invalidated_wait_never_resumes_dispatch(self):
        def waiting_plan(**changes):
            workflow = plan(
                step("ci", completion=(PlanCondition("values.state", "passed"),),
                     waiting=(PlanCondition("values.state", "pending"),)),
                ExecutionStep(CapabilityCall("call-merge", "merge", {}, "approval-1"),
                              (PlanCondition("state", "succeeded"),)),
            )
            return replace(workflow, status="waiting", next_due_at=NOW,
                           last_result_call_id="call-ci", **changes)

        prior = CapabilityAttempt(
            CapabilityCall("call-ci", "ci", {}), CapabilityAttemptDisposition.EXECUTED,
            True, CapabilityResult("call-ci", "ci", CapabilityResultState.SUCCEEDED,
                                   {"state": "pending"}))
        granted = Approval("approval-1", ApprovalScope("merge", {}),
                           ApprovalLifecycle.GRANTED, NOW + timedelta(seconds=5))
        cases = {
            "new_person_turn": ({}, (granted,), "person-2", ()),
            "goal_state_changed": ({}, (granted,), "person-1", ()),
            "plan_approval_invalid:expired": ({}, (granted,), "person-1", ("ci",)),
            "plan_approval_invalid:withdrawn": (
                {}, (replace(granted, lifecycle=ApprovalLifecycle.WITHDRAWN),),
                "person-1", ("ci",)),
        }
        for name, (changes, approvals, turn, expected_calls) in cases.items():
            with self.subTest(case=name):
                self.calls.clear()
                self.now = NOW + timedelta(seconds=10)
                self.outputs["ci"] = {"state": "passed"}
                self.reset_goal()
                snapshot = self.store.load("goal")
                self.store.replace(
                    replace(snapshot.state, execution_plan=waiting_plan(**changes),
                            attempts=(prior,), approvals=approvals),
                    snapshot.retention_until, snapshot.revision,
                )
                agent = self.agent(Reasoner(AgentDecision(
                    goal_id="goal", goal_proposal=GoalProposal(
                        GoalMutationKind.CANCEL, objective_summary=None),
                )), effectful=frozenset({"merge"}))
                if name == "goal_state_changed":
                    agent.process(conversation(), RETENTION, 1,
                                  origin=CognitionOrigin.EXTERNAL_EVENT)
                agent.advance_due_plans(lambda _, turn=turn: conversation(turn))
                stored = self.plan_state()
                self.assertNotIn("merge", self.calls)
                self.assertEqual(tuple(self.calls), expected_calls)
                self.assertEqual(stored.status, "needs_core")
                self.assertEqual(stored.core_reentry_reason, name.split(":")[0])

    def test_judgement_evidence_after_restart_is_reobserved_not_metadata(self):
        self.outputs[READ_EXTERNAL_REVIEW] = review_result(published_review())
        self.install(plan(review_step(), step("merge")))
        self.assertEqual(self.plan_state().status, "needs_core")
        stored = self.store.load("goal").state.attempts[-1].result
        self.assertNotIn("summary", stored.values)
        self.store.close()
        self.store = SQLiteGoalStore(self.path)
        reasoner = Reasoner(AgentDecision(response="One defect to repair.", goal_id="goal"))
        outcome = self.production_agent(reasoner).process(
            conversation(), RETENTION, 1, origin=CognitionOrigin.WORK_COMPLETED,
            resume_plan_goal_id="goal",
        )
        self.assertEqual(outcome.response, "One defect to repair.")
        evidence = reasoner.contexts[0].transient_attempts
        self.assertEqual(len(evidence), 1)
        self.assertEqual(evidence[0].result.values["summary"], "One defect in the reducer.")
        self.assertEqual(self.calls, [READ_EXTERNAL_REVIEW, READ_EXTERNAL_REVIEW])
        self.assertNotIn("merge", self.calls)

    def test_unrepeatable_judgement_evidence_lost_on_restart_blocks_follow_up(self):
        judged = CapabilityDefinition("assess", "assess", SCHEMA, SCHEMA,
                                      SideEffect.EFFECTFUL, requires_core_judgment=True)
        self.outputs["assess"] = lambda call: CapabilityAttempt(
            call, CapabilityAttemptDisposition.EXECUTED, True,
            CapabilityResult(call.call_id, "assess", CapabilityResultState.SUCCEEDED,
                             {"verdict": "unclear", "detail": "full text"},
                             durable_values={"verdict": "unclear"}))
        reasoner = Reasoner(AgentDecision(execution_plan=plan(step("assess"), step("merge")),
                                          goal_id="goal"))
        self.agent(reasoner, extra_definitions=(judged,)).process(conversation(), RETENTION, 1)
        self.store.close()
        self.store = SQLiteGoalStore(self.path)
        reasoner = Reasoner(AgentDecision(call=CapabilityCall("merge-now", "merge", {}),
                                          goal_id="goal"))
        outcome = self.agent(reasoner, extra_definitions=(judged,)).process(
            conversation(), RETENTION, 1, origin=CognitionOrigin.WORK_COMPLETED,
            resume_plan_goal_id="goal",
        )
        self.assertEqual(outcome.reason, "judgment_evidence_unavailable")
        self.assertEqual(self.calls, ["assess"])
        self.assertIn("judgment_evidence_unavailable", self.plan_state().core_reentry_facts)


class PlanResultSinglePathTests(unittest.TestCase):
    """Law 0: one classifier, one reducer, and no competing interpreter."""

    LOOP = Path(__file__).resolve().parents[1] / "src" / "alx" / "core" / "loop.py"

    def callers(self, name):
        tree = ast.parse(self.LOOP.read_text())
        found = set()
        for function in ast.walk(tree):
            if isinstance(function, ast.FunctionDef):
                for node in ast.walk(function):
                    if (isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
                            and node.func.id == name):
                        found.add(function.name)
        return found

    def callers_of_method(self, name):
        tree = ast.parse(self.LOOP.read_text())
        found = set()
        for function in ast.walk(tree):
            if isinstance(function, ast.FunctionDef):
                for node in ast.walk(function):
                    if (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
                            and node.func.attr == name):
                        found.add(function.name)
        return found

    def test_results_are_classified_and_reduced_in_one_place(self):
        self.assertEqual(self.callers("classify_planned_result"), {"_classify_and_apply"})
        # Fresh, recovered and resumed results, and a restart's re-read.
        self.assertEqual(self.callers_of_method("_classify_and_apply"),
                         {"_reconcile_plan", "_plan_evidence"})
        self.assertEqual(self.callers("reduce_plan"),
                         {"_apply_plan_result", "_reduce_goal_proposal"})
        # The blocker rule is read directly only for a call she made herself;
        # every planned result gets its blocker from the classifier.
        self.assertEqual(self.callers("judgment_blocker"), {"process"})

    def test_superseded_interpreters_are_deleted(self):
        source = self.LOOP.read_text()
        for name in ("_advance_execution_plan", "_mechanical_blocker_from_attempt",
                     "_plan_condition_matches", "_plan_conditions_match"):
            self.assertNotIn(name, source)
        # Nothing outside the reducer writes a wake, wait, or cursor move.
        for fragment in ('status="needs_core"', 'status="waiting"',
                         "core_reentry_reason=", "cursor=plan.cursor"):
            self.assertNotIn(fragment, source)


class OpenPlanScanTests(PlanHarness):
    """Finding 1: the due path reads only goals whose plan is still open."""

    def test_store_returns_only_open_plans_in_storage_order(self):
        def add(goal_id, status=GoalStatus.ACTIVE, plan_status=None, **extra):
            workflow = None
            if plan_status is not None:
                workflow = replace(plan(step("ci")), plan_id=f"plan-{goal_id}",
                                   status=plan_status,
                                   cursor=1 if plan_status == "completed" else 0,
                                   next_due_at=NOW if plan_status == "waiting" else None)
            self.store.create(replace(goal(), goal_id=goal_id, status=status,
                                      execution_plan=workflow, **extra),
                              "thread", RETENTION)

        add("no-plan")
        add("cancelled", GoalStatus.CANCELLED, stop_reason=GoalStopReason.CANCELLED)
        add("handled", plan_status="handled")
        add("waiting", plan_status="waiting")
        add("cancelled-handled", GoalStatus.CANCELLED, "handled",
            stop_reason=GoalStopReason.CANCELLED)
        add("needs-core", GoalStatus.BLOCKED, "needs_core",
            stop_reason=GoalStopReason.GENUINELY_BLOCKED,
            blockers=(WorkItem("review", "Review needs judgment"),))
        add("ready", plan_status="ready")
        add("completed", plan_status="completed")
        self.assertEqual(self.store.list_open_plan_goal_ids(),
                         ("waiting", "needs-core", "ready", "completed"))

    def test_due_path_never_decodes_the_full_goal_history(self):
        snapshot = self.store.load("goal")
        self.store.replace(replace(snapshot.state, execution_plan=plan(step("ci"))),
                           snapshot.retention_until, snapshot.revision)

        class Ledger:
            def exists(self, _identifier):
                return False

        with patch.object(self.store, "list_goals",
                          side_effect=AssertionError("full history scanned")):
            self.agent(Reasoner()).advance_due_plans(lambda _: conversation())
            offered = PlanContinuationSource(self.store, Ledger(),
                                             enabled=True).due_opportunities()
        self.assertEqual(self.calls, ["ci"])
        self.assertEqual(len(offered), 1)


class PlanFailureIsolationTests(PlanHarness):
    """Finding 2: one goal's failure does not stop another's reconciliation."""

    def test_failing_goal_is_logged_and_later_goal_still_reconciles(self):
        snapshot = self.store.load("goal")
        self.store.replace(replace(snapshot.state, execution_plan=plan(step("coding"))),
                           snapshot.retention_until, snapshot.revision)
        self.store.create(replace(goal(), goal_id="goal-b",
                                  execution_plan=replace(plan(step("ci")), plan_id="plan-b")),
                          "thread", RETENTION)
        loads = []

        def load(_conversation_id):
            loads.append(_conversation_id)
            if len(loads) == 1:
                raise LookupError("conversation unreadable")
            return conversation()

        with self.assertLogs("alx.core.loop", level="WARNING") as logs:
            advanced = self.agent(Reasoner()).advance_due_plans(load)
        self.assertEqual(advanced, 1)
        self.assertEqual(self.calls, ["ci"])
        self.assertEqual(self.store.load("goal-b").state.execution_plan.status, "completed")
        # The failed goal is neither advanced nor recorded as reconciled.
        failed = self.store.load("goal").state.execution_plan
        self.assertEqual((failed.status, failed.cursor), ("ready", 0))
        self.assertTrue(any("goal" in line and "LookupError" in line
                            for line in logs.output))


class PlannedDispatchConversationTests(PlanHarness):
    """Finding 3: a planned call runs under its goal's own conversation."""

    def setUp(self):
        super().setUp()
        # The runtime's binding, as live voice keeps it: only the budget
        # check names the conversation that executors then read.
        self.current = ["unrelated-earlier-thread"]
        self.seen = []
        self.budget_refused = False

    def budget_check(self, conversation_id):
        self.current[0] = conversation_id
        if self.budget_refused:
            raise RuntimeError("execution ceiling reached")

    def watching_agent(self, reasoner=None):
        def record(call):
            self.seen.append((call.capability_id, self.current[0]))
            return CapabilityAttempt(call, CapabilityAttemptDisposition.EXECUTED, True,
                                     CapabilityResult(call.call_id, call.capability_id,
                                                      CapabilityResultState.SUCCEEDED,
                                                      {"state": "done"}))
        self.outputs["ci"] = record
        self.outputs["coding"] = record
        return self.agent(reasoner or Reasoner(), budget_check=self.budget_check)

    def install(self, *steps):
        snapshot = self.store.load("goal")
        self.store.replace(replace(snapshot.state, execution_plan=plan(*steps)),
                           snapshot.retention_until, snapshot.revision)

    def test_due_tick_dispatch_binds_the_goals_conversation(self):
        self.install(step("coding"), step("ci"))
        self.watching_agent().advance_due_plans(lambda _: conversation())
        self.assertEqual(self.seen, [("coding", "thread"), ("ci", "thread")])

    def test_resumed_dispatch_binds_the_goals_conversation(self):
        self.install(step("ci"))
        self.watching_agent(Reasoner(AgentDecision(response="Done.", goal_id="goal"))).process(
            conversation(), RETENTION, 1, origin=CognitionOrigin.WORK_COMPLETED,
            resume_plan_goal_id="goal",
        )
        self.assertEqual(self.seen, [("ci", "thread")])

    def test_exceeded_budget_wakes_core_without_dispatching(self):
        self.install(step("ci"), step("merge"))
        self.budget_refused = True
        self.watching_agent().advance_due_plans(lambda _: conversation())
        stored = self.store.load("goal").state
        self.assertEqual(self.seen, [])
        self.assertEqual(self.calls, [])
        self.assertEqual(stored.attempts, ())
        self.assertEqual(stored.execution_plan.status, "needs_core")
        self.assertEqual(stored.execution_plan.core_reentry_reason, "plan_budget_exceeded")
        self.assertEqual(stored.execution_plan.cursor, 0)


class NonActiveJudgmentEvidenceTests(PlanHarness):
    """Finding 4: a goal that is not ACTIVE never receives a new dispatch."""

    def test_restart_never_reobserves_for_a_non_active_goal(self):
        judged = CapabilityDefinition("assess", "assess", SCHEMA, SCHEMA,
                                      SideEffect.NONE, requires_core_judgment=True)
        call = CapabilityCall("call-assess", "assess", {})
        stored = CapabilityAttempt(call, CapabilityAttemptDisposition.EXECUTED, True,
                                   CapabilityResult(call.call_id, "assess",
                                                    CapabilityResultState.SUCCEEDED,
                                                    {"verdict": "unclear"}))
        woken = replace(
            plan(ExecutionStep(call, (PlanCondition("state", "succeeded"),)), step("merge")),
            status="needs_core", last_result_call_id=call.call_id,
            core_reentry_reason="planned_evidence_requires_judgement",
            core_reentry_facts=("planned_evidence_requires_judgement",),
        )
        evidence = (Evidence("done", "verification", supports=("done",),
                             source_references=("attempt:call-assess",)),)
        work = (WorkItem("assess", "Judge the assessment"),)
        cases = (
            (GoalStatus.AWAITING_INPUT, GoalStopReason.REQUIRED_INPUT, (),
             {"outstanding_work": work}),
            (GoalStatus.BLOCKED, GoalStopReason.GENUINELY_BLOCKED, (), {"blockers": work}),
            (GoalStatus.COMPLETED, GoalStopReason.SUCCESS_CRITERIA_MET, evidence, {}),
        )
        for status, reason, proof, parked in cases:
            with self.subTest(status=status):
                self.reset_goal()
                snapshot = self.store.load("goal")
                self.store.replace(
                    replace(snapshot.state, status=status, stop_reason=reason,
                            evidence=proof, attempts=(stored,), execution_plan=woken,
                            **parked),
                    snapshot.retention_until, snapshot.revision,
                )
                self.store.close()
                self.store = SQLiteGoalStore(self.path)
                reasoner = Reasoner(AgentDecision(response="I need to read it again.",
                                                  goal_id="goal"))
                self.agent(reasoner, extra_definitions=(judged,)).process(
                    conversation(), RETENTION, 1, origin=CognitionOrigin.WORK_COMPLETED,
                    resume_plan_goal_id="goal",
                )
                state = self.store.load("goal").state
                self.assertEqual(self.calls, [])
                self.assertEqual(state.attempts, (stored,))
                self.assertFalse(any(item.disposition is CapabilityAttemptDisposition.PENDING
                                     for item in state.attempts))
                self.assertIn("judgment_evidence_unavailable",
                              state.execution_plan.core_reentry_facts)
                self.assertEqual(reasoner.contexts[0].transient_attempts, ())


class AdversarialFollowUpTests(PlanHarness):
    """Gaps found by local review of the four fixes."""

    def test_undecodable_goal_does_not_stop_other_plans_or_occasions(self):
        import sqlite3
        self.store.create(replace(goal(), goal_id="goal-b",
                                  execution_plan=replace(plan(step("ci")), plan_id="plan-b")),
                          "thread", RETENTION)
        self.store.create(replace(goal(), goal_id="goal-c", execution_plan=replace(
            plan(step("coding")), plan_id="plan-c", cursor=1, status="completed")),
            "thread", RETENTION)
        snapshot = self.store.load("goal")
        self.store.replace(replace(snapshot.state, execution_plan=plan(step("other"))),
                           snapshot.retention_until, snapshot.revision)
        # The plan still reads as open in SQL but no longer decodes.
        raw = sqlite3.connect(self.path)
        raw.execute("UPDATE goals SET state_json = json_set(state_json, "
                    "'$.execution_plan.cursor', 99) WHERE goal_id = 'goal'")
        raw.commit()
        raw.close()

        class Ledger:
            def exists(self, _identifier):
                return False

        with self.assertLogs(level="WARNING"):
            self.agent(Reasoner()).advance_due_plans(lambda _: conversation())
            offered = PlanContinuationSource(self.store, Ledger(),
                                             enabled=True).due_opportunities()
        self.assertEqual(self.calls, ["ci"])
        self.assertEqual({item.references[0] for item in offered},
                         {"execution_plan:goal-b", "execution_plan:goal-c"})

    def test_selection_reobservation_does_not_rebind_the_turns_dispatch(self):
        current = ["thread"]
        seen = []

        def budget_check(conversation_id):
            current[0] = conversation_id

        judged = CapabilityDefinition("assess", "assess", SCHEMA, SCHEMA,
                                      SideEffect.NONE, requires_core_judgment=True)

        def record(call):
            seen.append((call.capability_id, current[0]))
            return CapabilityAttempt(call, CapabilityAttemptDisposition.EXECUTED, True,
                                     CapabilityResult(call.call_id, call.capability_id,
                                                      CapabilityResultState.SUCCEEDED,
                                                      {"state": "done"}))

        self.outputs["assess"] = record
        self.outputs["ci"] = record
        call = CapabilityCall("call-assess", "assess", {})
        stored = CapabilityAttempt(call, CapabilityAttemptDisposition.EXECUTED, True,
                                   CapabilityResult(call.call_id, "assess",
                                                    CapabilityResultState.SUCCEEDED,
                                                    {"verdict": "unclear"}))
        woken = replace(
            plan(ExecutionStep(call, (PlanCondition("state", "succeeded"),))),
            status="needs_core", last_result_call_id=call.call_id,
            core_reentry_reason="planned_evidence_requires_judgement",
            core_reentry_facts=("planned_evidence_requires_judgement",),
        )
        self.store.create(replace(goal(), goal_id="elsewhere", attempts=(stored,),
                                  execution_plan=woken), "other-thread", RETENTION)
        reasoner = Reasoner(AgentDecision(call=CapabilityCall("turn-ci", "ci", {}),
                                          goal_id="elsewhere"))
        agent = self.agent(reasoner, extra_definitions=(judged,), budget_check=budget_check)
        agent.process(conversation(), RETENTION, 1, origin=CognitionOrigin.EXTERNAL_EVENT)
        self.assertEqual(seen, [("assess", "other-thread"), ("ci", "thread")])


class StartupRecoveryIsolationTests(PlanHarness):
    """CodeRabbit b039f45 #1: one stale record cannot stop AL/X starting."""

    class Ledger:
        def __init__(self, *goal_ids):
            self.rows = {}
            for goal_id in goal_ids:
                identifier = f"execution_plan:{goal_id}:plan-{goal_id}:1"
                self.rows[identifier] = {
                    "opportunity_id": identifier,
                    "refs": "\x1f".join((
                        f"execution_plan:{goal_id}", f"execution_plan_id:plan-{goal_id}",
                        "execution_plan_cursor:1", "execution_plan_generation:0",
                    )),
                }
            self.unreconciled = []
            self.released = []

        def unfinished(self):
            return tuple(self.rows.values())

        def mark_unreconciled(self, identifier):
            self.unreconciled.append(identifier)

        def release(self, identifier):
            self.released.append(identifier)

    class Spend:
        def dispatch_started(self, _identifier):
            return True

    def completed_goal(self, goal_id):
        self.store.create(replace(goal(), goal_id=goal_id, execution_plan=replace(
            plan(step("coding")), plan_id=f"plan-{goal_id}", cursor=1, status="completed")),
            "thread", RETENTION)

    def test_missing_and_undecodable_goals_do_not_stop_later_recovery(self):
        import sqlite3
        self.completed_goal("corrupt")
        self.completed_goal("valid")
        raw = sqlite3.connect(self.path)
        raw.execute("UPDATE goals SET state_json = json_set(state_json, "
                    "'$.execution_plan.cursor', 99) WHERE goal_id = 'corrupt'")
        raw.commit()
        raw.close()
        ledger = self.Ledger("missing", "corrupt", "valid")
        source = PlanContinuationSource(self.store, ledger, enabled=True)
        with self.assertLogs("alx.continuity.plan_source", level="WARNING") as logs:
            source.recover(self.Spend())
        self.assertEqual(sorted(ledger.unreconciled), sorted(ledger.rows))
        self.assertEqual(ledger.released, [])
        self.assertEqual(
            self.store.load("valid").state.execution_plan.continuation_generation, 1)
        for goal_id, error in (("missing", "GoalNotFound"), ("corrupt", "ValueError")):
            self.assertTrue(any(f"execution_plan:{goal_id}:" in line and f"goal {goal_id}" in line
                                and error in line for line in logs.output), logs.output)

    def test_ledger_failure_on_one_record_still_recovers_the_next(self):
        self.completed_goal("valid")
        ledger = self.Ledger("broken", "valid")
        original = ledger.mark_unreconciled
        failed = []

        def mark(identifier):
            if "broken" in identifier and not failed:
                failed.append(identifier)
                raise RuntimeError("ledger busy")
            original(identifier)

        ledger.mark_unreconciled = mark
        with self.assertLogs("alx.continuity.plan_source", level="WARNING"):
            PlanContinuationSource(self.store, ledger, enabled=True).recover(self.Spend())
        # Recovery continued past the failure. The failed record stays
        # unfinished for the next startup to retain; it is never released
        # for replay.
        self.assertEqual(ledger.unreconciled, ["execution_plan:valid:plan-valid:1"])
        self.assertEqual(failed, ["execution_plan:broken:plan-broken:1"])
        self.assertEqual(ledger.released, [])


class ParkingWithExecutablePlanTests(PlanHarness):
    """CodeRabbit b039f45 #2: a parked goal never keeps an executable plan."""

    def waiting(self, **state_changes):
        workflow = replace(
            plan(step("ci", completion=(PlanCondition("values.state", "passed"),),
                      waiting=(PlanCondition("values.state", "pending"),)), step("merge")),
            status="waiting", next_due_at=NOW + timedelta(seconds=60),
            last_result_call_id="call-ci",
        )
        prior = CapabilityAttempt(
            workflow.steps[0].call, CapabilityAttemptDisposition.EXECUTED, True,
            CapabilityResult("call-ci", "ci", CapabilityResultState.SUCCEEDED,
                             {"state": "pending"}))
        snapshot = self.store.load("goal")
        self.store.replace(replace(snapshot.state, execution_plan=workflow, attempts=(prior,),
                                   **state_changes),
                           snapshot.retention_until, snapshot.revision)
        return workflow

    def assert_parked(self, status, work_field):
        state = self.store.load("goal").state
        self.assertIs(state.status, status)
        self.assertEqual(state.execution_plan.status, "needs_core")
        self.assertEqual(state.execution_plan.core_reentry_reason, "goal_parked")
        self.assertEqual(state.execution_plan.cursor, 0)
        self.assertTrue(getattr(state, work_field))
        self.assertNotIn("merge", self.calls)
        return state

    def test_autonomous_call_less_end_parks_awaiting_input_and_keeps_response(self):
        self.waiting(outstanding_work=(WorkItem("ci", "Wait for checks"),))
        reasoner = Reasoner(AgentDecision(response="Checks still running.", goal_id="goal"),
                            AgentDecision(response="Checks still running.", goal_id="goal"))
        outcome = self.agent(reasoner).process(conversation(), RETENTION, 3,
                                               origin=CognitionOrigin.EXTERNAL_EVENT)
        self.assertEqual(outcome.state, CoreState.RESPONDED)
        self.assertEqual(outcome.response, "Checks still running.")
        self.assert_parked(GoalStatus.AWAITING_INPUT, "outstanding_work")

    def test_blocked_parking_wakes_the_plan(self):
        self.waiting(blockers=(WorkItem("ci", "Checks need a decision"),))
        reasoner = Reasoner(AgentDecision(response="Blocked on checks.", goal_id="goal"),
                            AgentDecision(response="Blocked on checks.", goal_id="goal"))
        outcome = self.agent(reasoner).process(conversation(), RETENTION, 3,
                                               origin=CognitionOrigin.EXTERNAL_EVENT)
        self.assertEqual(outcome.response, "Blocked on checks.")
        self.assert_parked(GoalStatus.BLOCKED, "blockers")

    def test_mechanical_blocker_parks_another_goal_with_a_waiting_plan(self):
        self.waiting(outstanding_work=(WorkItem("ci", "Wait for checks"),))
        self.store.create(replace(goal(), goal_id="review-goal"), "thread", RETENTION)
        self.outputs["request_external_review"] = (
            CapabilityResultState.FAILED,
            {"code": "review_pending", "requires_judgement": True},
        )
        reasoner = Reasoner(
            AgentDecision(call=CapabilityCall("review-a", "request_external_review", {}),
                          goal_id="review-goal"),
            AgentDecision(call=CapabilityCall("ci-direct", "ci", {}), goal_id="goal"),
        )
        outcome = self.agent(reasoner).process(conversation(), RETENTION, 3,
                                               origin=CognitionOrigin.EXTERNAL_EVENT)
        self.assertEqual(outcome.reason, "review_pending")
        self.assertNotIn("ci", self.calls)
        self.assert_parked(GoalStatus.AWAITING_INPUT, "outstanding_work")

    def test_terminal_blocker_response_parks_without_losing_it(self):
        self.waiting(outstanding_work=(WorkItem("ci", "Wait for checks"),))
        self.outputs["request_external_review"] = (
            CapabilityResultState.FAILED,
            {"code": "review_pending", "requires_judgement": True},
        )
        reasoner = Reasoner(
            AgentDecision(call=CapabilityCall("review-p", "request_external_review", {}),
                          goal_id="goal"),
            AgentDecision(response="The review is still pending.", goal_id="goal"),
        )
        outcome = self.agent(reasoner).process(conversation(), RETENTION, 3)
        self.assertEqual(outcome.state, CoreState.RESPONDED)
        self.assertEqual(outcome.response, "The review is still pending.")
        self.assert_parked(GoalStatus.AWAITING_INPUT, "outstanding_work")

    def test_gateway_stores_the_response_from_a_parking_turn(self):
        self.waiting(outstanding_work=(WorkItem("ci", "Wait for checks"),))
        conversations = SQLiteConversationStore(self.path.with_name("conversation.sqlite3"))
        self.addCleanup(conversations.close)
        created = conversations.create("thread", RETENTION)
        conversations.append(conversation().turns[0], RETENTION, created.revision)
        gateway = ConversationGateway(self.agent(Reasoner(
            AgentDecision(response="Checks still running.", goal_id="goal"),
            AgentDecision(response="Checks still running.", goal_id="goal"),
        )), conversations)
        opportunity = CognitionOpportunity("mail:1", CognitionOrigin.EXTERNAL_EVENT, NOW,
                                           "thread")
        gateway.receive_cognition_opportunity("thread", opportunity, 3, RETENTION)
        self.assertEqual(conversations.load("thread").turns[-1].content,
                         "Checks still running.")
        state = self.store.load("goal").state
        self.assertIs(state.status, GoalStatus.AWAITING_INPUT)
        self.assertNotIn(state.execution_plan.status, {"ready", "waiting"})


class RepeatedPlanRefusalTests(PlanHarness):
    """An unchanged plan refusal never buys another workflow reasoning step.

    A person still hears exactly one response-only step about it; an
    autonomous turn ends silently.
    """

    TURN_BOUND = frozenset({"review"})

    def invalid(self, plan_id="plan-x"):
        # A step needing a fresh person-turn approval cannot be planned.
        return replace(plan(step("review")), plan_id=plan_id)

    def refusing_agent(self, reasoner, **options):
        return self.agent(reasoner, turn_bound=self.TURN_BOUND, **options)

    def workflow_steps(self, reasoner):
        return [item for item in reasoner.contexts if item.response_only_reason is None]

    def test_identical_refusal_buys_no_workflow_step_but_the_person_is_answered(self):
        reasoner = Reasoner(*(AgentDecision(execution_plan=self.invalid(), goal_id="goal")
                              for _ in range(2)),
                            AgentDecision(response="That needs your approval first.",
                                          goal_id="goal"),
                            *(AgentDecision(execution_plan=self.invalid(), goal_id="goal")
                              for _ in range(3)))
        outcome = self.refusing_agent(reasoner).process(conversation(), RETENTION, 6)
        self.assertEqual(len(self.workflow_steps(reasoner)), 2)
        self.assertEqual(reasoner.calls, 3)
        self.assertEqual(reasoner.contexts[2].response_only_reason, "plan_precondition_invalid")
        self.assertEqual(outcome.state, CoreState.RESPONDED)
        self.assertEqual(outcome.response, "That needs your approval first.")
        self.assertEqual(outcome.reason, "plan_precondition_invalid")
        first = reasoner.contexts[1].refused_calls
        self.assertEqual([item["reason"] for item in first], ["plan_precondition_invalid"])
        self.assertTrue(first[0]["subject"].startswith("plan-x:"))
        self.assertEqual(self.calls, [])
        self.assertIsNone(self.store.load("goal").state.execution_plan)

    def test_response_only_step_cannot_dispatch_or_change_anything(self):
        before = self.store.load("goal")
        attempts = (
            AgentDecision(call=CapabilityCall("ci-x", "ci", {}), goal_id="goal"),
            AgentDecision(execution_plan=plan(step("coding")), goal_id="goal"),
            AgentDecision(response="Changed.", goal_id="goal", goal_proposal=GoalProposal(
                GoalMutationKind.UPDATE, objective_summary="Something else")),
        )
        for index, attempt in enumerate(attempts):
            with self.subTest(attempt=index):
                reasoner = Reasoner(AgentDecision(execution_plan=self.invalid(), goal_id="goal"),
                                    AgentDecision(execution_plan=self.invalid(), goal_id="goal"),
                                    attempt)
                outcome = self.refusing_agent(reasoner).process(conversation(), RETENTION, 6)
                self.assertEqual(outcome.state, CoreState.CHECKPOINTED)
                self.assertEqual(reasoner.calls, 3)
                self.assertEqual(self.calls, [])
                after = self.store.load("goal")
                self.assertEqual(after.state.objective, before.state.objective)
                self.assertIsNone(after.state.execution_plan)

    def test_autonomous_turn_stays_silent_on_a_repeated_refusal(self):
        reasoner = Reasoner(*(AgentDecision(execution_plan=self.invalid(), goal_id="goal")
                              for _ in range(4)))
        outcome = self.refusing_agent(reasoner).process(
            conversation(), RETENTION, 4, origin=CognitionOrigin.EXTERNAL_EVENT)
        self.assertEqual((reasoner.calls, outcome.state, outcome.reason),
                         (2, CoreState.CHECKPOINTED, "plan_precondition_invalid"))

    def test_every_plan_refusal_reason_is_suppressed_on_repeat(self):
        unsafe = replace(plan(step("merge", completion=(PlanCondition("values.state", "done"),),
                                   waiting=(PlanCondition("values.state", "pending"),))),
                         plan_id="plan-unsafe")
        cases = {
            "plan_precondition_invalid": (self.invalid(), {"turn_bound": self.TURN_BOUND}),
            "plan_wait_unsafe": (unsafe, {"effectful": frozenset({"merge"})}),
            "plan_continuation_unavailable": (plan(step("coding")),
                                              {"plan_continuation": False}),
        }
        for reason, (workflow, options) in cases.items():
            with self.subTest(reason=reason):
                self.reset_goal()
                reasoner = Reasoner(*(AgentDecision(execution_plan=workflow, goal_id="goal")
                                      for _ in range(4)))
                outcome = self.agent(reasoner, **options).process(
                    conversation(), RETENTION, 4, origin=CognitionOrigin.EXTERNAL_EVENT)
                self.assertEqual((reasoner.calls, outcome.reason), (2, reason))

    def test_inactive_goal_refusal_is_suppressed_on_repeat(self):
        snapshot = self.store.load("goal")
        self.store.replace(replace(snapshot.state, status=GoalStatus.AWAITING_INPUT,
                                   stop_reason=GoalStopReason.REQUIRED_INPUT,
                                   outstanding_work=(WorkItem("x", "Need input"),)),
                           snapshot.retention_until, snapshot.revision)
        reasoner = Reasoner(*(AgentDecision(execution_plan=plan(step("coding")), goal_id="goal")
                              for _ in range(4)))
        outcome = self.agent(reasoner).process(conversation(), RETENTION, 4,
                                               origin=CognitionOrigin.EXTERNAL_EVENT)
        self.assertEqual((reasoner.calls, outcome.reason), (2, "plan_requires_active_goal"))

    def test_rejected_goal_mutation_refusal_is_suppressed_on_repeat(self):
        rejected = GoalProposal(GoalMutationKind.REQUEST_COMPLETION)
        reasoner = Reasoner(*(AgentDecision(execution_plan=plan(step("coding")), goal_id="goal",
                                            goal_proposal=rejected) for _ in range(4)))
        outcome = self.agent(reasoner).process(conversation(), RETENTION, 4)
        # The proposal's own refusal is checked first and stops the repeat;
        # the plan refusal it carried is recorded with a subject beside it.
        self.assertEqual(outcome.reason, "goal_proposal_invalid")
        self.assertEqual(reasoner.calls, 2)
        self.assertEqual(self.calls, [])
        plan_refusals = [item for item in reasoner.contexts[1].refused_calls
                         if item["reason"] == "plan_goal_mutation_rejected"]
        self.assertEqual(len(plan_refusals), 1)
        self.assertTrue(plan_refusals[0]["subject"].startswith("plan-1:"))

    def test_different_plan_is_not_suppressed(self):
        reasoner = Reasoner(AgentDecision(execution_plan=self.invalid("plan-a"), goal_id="goal"),
                            AgentDecision(execution_plan=self.invalid("plan-b"), goal_id="goal"),
                            AgentDecision(response="It needs your approval.", goal_id="goal"))
        outcome = self.refusing_agent(reasoner).process(conversation(), RETENTION, 4)
        self.assertEqual(len(self.workflow_steps(reasoner)), 3)
        self.assertEqual(outcome.response, "It needs your approval.")
        self.assertEqual([item["reason"] for item in reasoner.contexts[2].refused_calls],
                         ["plan_precondition_invalid", "plan_precondition_invalid"])

    def test_same_plan_is_reconsidered_after_the_goal_genuinely_changed(self):
        snapshot = self.store.load("goal")
        self.store.replace(replace(snapshot.state, status=GoalStatus.AWAITING_INPUT,
                                   stop_reason=GoalStopReason.REQUIRED_INPUT,
                                   outstanding_work=(WorkItem("x", "Need input"),)),
                           snapshot.retention_until, snapshot.revision)
        same = plan(step("coding"))
        reasoner = Reasoner(
            AgentDecision(execution_plan=same, goal_id="goal"),
            AgentDecision(execution_plan=same, goal_id="goal", goal_proposal=GoalProposal(
                GoalMutationKind.UPDATE, outstanding_work=())),
            AgentDecision(response="Done.", goal_id="goal"),
        )
        self.agent(reasoner).process(conversation(), RETENTION, 4,
                                     origin=CognitionOrigin.EXTERNAL_EVENT)
        self.assertEqual(self.calls, ["coding"])

    def test_same_refusal_after_changed_goal_state_gets_another_step(self):
        reasoner = Reasoner(
            AgentDecision(execution_plan=self.invalid(), goal_id="goal"),
            AgentDecision(execution_plan=self.invalid(), goal_id="goal",
                          goal_proposal=GoalProposal(GoalMutationKind.UPDATE,
                                                     context={"head": "b" * 40})),
            AgentDecision(response="It still needs your approval.", goal_id="goal"),
        )
        outcome = self.refusing_agent(reasoner).process(conversation(), RETENTION, 4)
        self.assertEqual(len(self.workflow_steps(reasoner)), 3)
        self.assertEqual(outcome.response, "It still needs your approval.")
        subjects = [item["subject"] for item in reasoner.contexts[2].refused_calls
                    if item["reason"] == "plan_precondition_invalid"]
        self.assertEqual(len(subjects), 2)
        self.assertNotEqual(subjects[0], subjects[1])

class Crash(BaseException):
    """The process stopping: nothing in the gateway may catch it."""


class ResponseDurabilityTests(PlanHarness):
    """A stored response never depends on plan bookkeeping, and is never repeated."""

    def setUp(self):
        super().setUp()
        snapshot = self.store.load("goal")
        self.store.replace(replace(snapshot.state, execution_plan=replace(
            plan(step("coding")), cursor=1, status="completed",
            core_reentry_reason="plan_completed", core_reentry_facts=("plan_completed",))),
            snapshot.retention_until, snapshot.revision)
        self.conversations = SQLiteConversationStore(self.path.with_name("conversation.sqlite3"))
        self.addCleanup(self.conversations.close)
        created = self.conversations.create("thread", RETENTION)
        self.conversations.append(conversation().turns[0], RETENTION, created.revision)

    class Ledger:
        def exists(self, _identifier):
            return False

        def record_created(self, _opportunity):
            return True

    def offered(self):
        return PlanContinuationSource(self.store, self.Ledger(), enabled=True).due_opportunities()

    def gateway(self, *decisions):
        self.reasoner = Reasoner(*decisions)
        self.core = self.agent(self.reasoner)
        return ConversationGateway(self.core, self.conversations)

    def deliver(self, gateway):
        return gateway.receive_cognition_opportunity("thread", self.offered()[0], 1, RETENTION)

    def responses(self):
        return [item for item in self.conversations.load("thread").turns
                if item.origin is ConversationOrigin.ALX_RESPONSE]

    def restart(self):
        self.store.close()
        self.store = SQLiteGoalStore(self.path)
        return self.gateway()

    def test_acknowledgement_failure_keeps_the_stored_response(self):
        gateway = self.gateway(AgentDecision(response="Finished.", goal_id="goal"))
        with patch.object(self.core, "acknowledge_plan_response",
                          side_effect=RuntimeError("goal store busy")):
            with self.assertLogs("alx.conversation.gateway", level="WARNING"):
                outcome = self.deliver(gateway)
        self.assertEqual(outcome.response, "Finished.")
        self.assertEqual([item.content for item in self.responses()], ["Finished."])
        # Bookkeeping is recovered from the stored turn, without a second answer.
        self.restart().advance_due_plans()
        self.assertEqual(self.store.load("goal").state.execution_plan.status, "handled")
        self.assertEqual(self.offered(), ())
        self.assertEqual(len(self.responses()), 1)

    def test_response_store_failure_is_never_treated_as_delivered(self):
        gateway = self.gateway(AgentDecision(response="Finished.", goal_id="goal"))
        original = self.conversations.append

        def append(turn, retention_until, expected_revision):
            if turn.origin is ConversationOrigin.ALX_RESPONSE:
                raise RuntimeError("conversation store unavailable")
            return original(turn, retention_until, expected_revision)

        with patch.object(self.conversations, "append", side_effect=append):
            with self.assertRaisesRegex(RuntimeError, "conversation store unavailable"):
                self.deliver(gateway)
        self.assertEqual(self.responses(), [])
        self.restart().advance_due_plans()
        self.assertEqual(self.store.load("goal").state.execution_plan.status, "completed")
        self.assertEqual(len(self.offered()), 1)

    def test_crash_before_storage_answers_exactly_once_after_restart(self):
        gateway = self.gateway(AgentDecision(response="Finished.", goal_id="goal"))
        with patch.object(self.conversations, "append", side_effect=Crash()):
            with self.assertRaises(Crash):
                self.deliver(gateway)
        restarted = self.restart()
        restarted.advance_due_plans()
        self.assertEqual(len(self.offered()), 1)
        gateway = self.gateway(AgentDecision(response="Finished.", goal_id="goal"))
        self.deliver(gateway)
        self.assertEqual(len(self.responses()), 1)
        self.assertEqual(self.store.load("goal").state.execution_plan.status, "handled")

    def test_crash_between_storage_and_acknowledgement_never_answers_twice(self):
        gateway = self.gateway(AgentDecision(response="Finished.", goal_id="goal"))
        opportunity = self.offered()[0]
        with patch.object(self.core, "acknowledge_plan_response", side_effect=Crash()):
            with self.assertRaises(Crash):
                gateway.receive_cognition_opportunity("thread", opportunity, 1, RETENTION)
        self.assertEqual(len(self.responses()), 1)
        # Even offered again before any tick reconciles, she is not asked twice.
        self.store.close()
        self.store = SQLiteGoalStore(self.path)
        resumed = self.gateway(AgentDecision(response="Finished again.", goal_id="goal"))
        outcome = resumed.receive_cognition_opportunity("thread", opportunity, 1, RETENTION)
        self.assertEqual(outcome.reason, "plan_inactive")
        self.assertEqual(self.reasoner.calls, 0)
        self.assertEqual([item.content for item in self.responses()], ["Finished."])
        self.assertEqual(self.store.load("goal").state.execution_plan.status, "handled")

    def test_restart_after_acknowledgement_offers_nothing(self):
        self.deliver(self.gateway(AgentDecision(response="Finished.", goal_id="goal")))
        self.restart().advance_due_plans()
        self.assertEqual(self.offered(), ())
        self.assertEqual(len(self.responses()), 1)

    def test_person_turn_after_an_unacknowledged_answer_is_still_stored(self):
        gateway = self.gateway(AgentDecision(response="Finished.", goal_id="goal"))
        with patch.object(self.core, "acknowledge_plan_response", side_effect=Crash()):
            with self.assertRaises(Crash):
                self.deliver(gateway)
        self.store.close()
        self.store = SQLiteGoalStore(self.path)
        gateway = self.gateway(AgentDecision(response="Here is more.", goal_id="goal"))
        follow_up = ConversationTurn("thread", "person-2", ConversationOrigin.TYPED,
                                     "And then?", NOW, "friedl")
        gateway.receive_conversation_turn(follow_up, 1, RETENTION)
        self.assertEqual([item.content for item in self.responses()],
                         ["Finished.", "Here is more."])


class JudgmentRereadClassificationTests(PlanHarness):
    """The restart re-read of judgment evidence is a planned result like any other."""

    def production_agent(self, reasoner):
        return self.agent(reasoner, extra_definitions=(REVIEW_CONTENT_DEFINITION,))

    def woken_by_review(self):
        self.outputs[READ_EXTERNAL_REVIEW] = review_result(published_review())
        self.production_agent(Reasoner(AgentDecision(
            execution_plan=plan(review_step(), step("merge")), goal_id="goal",
        ))).process(conversation(), RETENTION, 1)
        before = self.store.load("goal").state.execution_plan
        self.assertEqual(before.status, "needs_core")
        self.store.close()
        self.store = SQLiteGoalStore(self.path)
        return before

    def resume(self, *decisions):
        reasoner = Reasoner(*decisions)
        outcome = self.production_agent(reasoner).process(
            conversation(), RETENTION, len(decisions) or 1,
            origin=CognitionOrigin.WORK_COMPLETED, resume_plan_goal_id="goal",
        )
        return reasoner, outcome, self.store.load("goal").state.execution_plan

    def test_reread_passes_through_the_classifier_and_wakes_core(self):
        before = self.woken_by_review()
        with patch("alx.core.loop.classify_planned_result",
                   wraps=classify_planned_result) as classifier:
            reasoner, outcome, after = self.resume(
                AgentDecision(response="One defect to repair.", goal_id="goal"))
        rereads = [item for item in classifier.call_args_list
                   if item.kwargs.get("judgment_wake")]
        self.assertEqual(len(rereads), 1)
        self.assertEqual(rereads[0].kwargs["judgment_wake"], before.core_reentry_facts)
        self.assertEqual(after.status, "needs_core")
        self.assertEqual(after.cursor, before.cursor)
        self.assertIn("planned_evidence_requires_judgement", after.core_reentry_facts)
        self.assertIn("judgment_evidence_reobserved", after.core_reentry_facts)
        self.assertNotEqual(after.last_result_call_id, before.last_result_call_id)
        self.assertEqual(reasoner.contexts[0].transient_attempts[0].result.values["summary"],
                         "One defect in the reducer.")
        self.assertEqual(outcome.response, "One defect to repair.")

    def test_reread_that_would_wait_or_fail_holds_follow_up_action(self):
        cases = {
            "in_progress": (review_result(unavailable_review(REVIEW_IN_PROGRESS)),
                            "judgment_evidence_unavailable"),
            "failed": (review_result(unavailable_review(REVIEW_FAILED)), "review_unavailable"),
            "transport": (review_result(ReviewReadError("review_unavailable")),
                          "review_unavailable"),
            "refused": (refused, "judgment_evidence_unavailable"),
            "broker_failure": (broker_failure("executor_error", True),
                               "judgment_evidence_unavailable"),
        }
        for name, (reread, blocker) in cases.items():
            with self.subTest(case=name):
                self.reset_goal()
                self.calls.clear()
                before = self.woken_by_review()
                self.outputs[READ_EXTERNAL_REVIEW] = reread
                self.calls.clear()
                _, outcome, after = self.resume(AgentDecision(
                    call=CapabilityCall(f"merge-{name}", "merge", {}), goal_id="goal"))
                self.assertEqual(after.status, "needs_core")
                self.assertEqual(after.cursor, before.cursor)
                self.assertIsNone(after.next_due_at)
                self.assertEqual(after.mechanical_blocker, blocker)
                self.assertEqual(outcome.reason, blocker)
                self.assertNotIn("merge", self.calls)

    def test_unrepeatable_evidence_is_reduced_not_written_directly(self):
        judged = CapabilityDefinition("assess", "assess", SCHEMA, SCHEMA,
                                      SideEffect.EFFECTFUL, requires_core_judgment=True)
        self.outputs["assess"] = lambda call: CapabilityAttempt(
            call, CapabilityAttemptDisposition.EXECUTED, True,
            CapabilityResult(call.call_id, "assess", CapabilityResultState.SUCCEEDED,
                             {"verdict": "unclear"}, durable_values={}))
        self.agent(Reasoner(AgentDecision(execution_plan=plan(step("assess")), goal_id="goal")),
                   extra_definitions=(judged,)).process(conversation(), RETENTION, 1)
        self.store.close()
        self.store = SQLiteGoalStore(self.path)
        with patch("alx.core.loop.reduce_plan", wraps=reduce_plan) as reducer:
            self.agent(Reasoner(AgentDecision(response="I cannot judge it.", goal_id="goal")),
                       extra_definitions=(judged,)).process(
                conversation(), RETENTION, 1, origin=CognitionOrigin.WORK_COMPLETED,
                resume_plan_goal_id="goal",
            )
        self.assertTrue(any("judgment_evidence_unavailable" in item.args[1].facts
                            for item in reducer.call_args_list))
        self.assertEqual(self.calls, ["assess"])


class ReviewFollowUpTests(PlanHarness):
    """Gaps found by local review of the re-read and response-durability fixes."""

    def test_reread_whose_dispatch_raises_is_classified_as_interrupted(self):
        self.outputs[READ_EXTERNAL_REVIEW] = review_result(published_review())
        agent = self.agent(Reasoner(AgentDecision(
            execution_plan=plan(review_step(), step("merge")), goal_id="goal")),
            extra_definitions=(REVIEW_CONTENT_DEFINITION,))
        agent.process(conversation(), RETENTION, 1)
        self.store.close()
        self.store = SQLiteGoalStore(self.path)

        def explode(_call):
            raise RuntimeError("transport dropped")

        self.outputs[READ_EXTERNAL_REVIEW] = explode
        reasoner = Reasoner(AgentDecision(call=CapabilityCall("merge-x", "merge", {}),
                                          goal_id="goal"))
        outcome = self.agent(reasoner, extra_definitions=(REVIEW_CONTENT_DEFINITION,)).process(
            conversation(), RETENTION, 1, origin=CognitionOrigin.WORK_COMPLETED,
            resume_plan_goal_id="goal",
        )
        stored = self.store.load("goal").state.execution_plan
        self.assertIn("dispatch_interrupted", stored.core_reentry_facts)
        self.assertIn("judgment_evidence_unavailable", stored.core_reentry_facts)
        self.assertEqual(stored.mechanical_blocker, "judgment_evidence_unavailable")
        self.assertEqual(outcome.reason, "judgment_evidence_unavailable")
        self.assertNotIn("merge", self.calls)

    def test_due_tick_loads_a_waiting_continuations_thread_once(self):
        snapshot = self.store.load("goal")
        self.store.replace(replace(snapshot.state, execution_plan=replace(
            plan(step("coding")), cursor=1, status="completed")),
            snapshot.retention_until, snapshot.revision)
        loads = []
        agent = self.agent(Reasoner())
        for _ in range(3):
            agent.advance_due_plans(lambda cid: loads.append(cid) or conversation())
        self.assertEqual(loads, ["thread"])



class InterruptedRereadTests(PlanHarness):
    """CodeRabbit 0b81a6c: an interrupted re-read never leaves an open dispatch."""

    def woken_then_reread_raises(self, **state_changes):
        self.outputs[READ_EXTERNAL_REVIEW] = review_result(published_review())
        self.agent(Reasoner(AgentDecision(
            execution_plan=plan(review_step(), step("merge")), goal_id="goal",
        )), extra_definitions=(REVIEW_CONTENT_DEFINITION,)).process(conversation(), RETENTION, 1)
        if state_changes:
            snapshot = self.store.load("goal")
            self.store.replace(replace(snapshot.state, **state_changes),
                               snapshot.retention_until, snapshot.revision)
        self.store.close()
        self.store = SQLiteGoalStore(self.path)

        def explode(_call):
            raise RuntimeError("transport dropped")

        self.outputs[READ_EXTERNAL_REVIEW] = explode

    def resume(self, *decisions):
        with patch("alx.core.loop.classify_planned_result",
                   wraps=classify_planned_result) as classifier:
            self.reasoner = Reasoner(*decisions)
            outcome = self.agent(self.reasoner, extra_definitions=(REVIEW_CONTENT_DEFINITION,)).process(
                conversation(), RETENTION, len(decisions), origin=CognitionOrigin.WORK_COMPLETED,
                resume_plan_goal_id="goal",
            )
        self.rereads = [item for item in classifier.call_args_list
                        if item.kwargs.get("judgment_wake")]
        return outcome

    def assert_closed_and_classified(self):
        state = self.store.load("goal").state
        self.assertFalse(any(item.disposition is CapabilityAttemptDisposition.PENDING
                             for item in state.attempts))
        closed = [item for item in state.attempts
                  if item.reason_code == "dispatch_interrupted"]
        self.assertEqual(len(closed), 1)
        self.assertIs(closed[0].disposition, CapabilityAttemptDisposition.BROKER_FAILURE)
        # The classifier read the closed attempt, once, inside the wake.
        self.assertEqual(len(self.rereads), 1)
        self.assertIs(self.rereads[0].args[1].disposition,
                      CapabilityAttemptDisposition.BROKER_FAILURE)
        plan_state = state.execution_plan
        self.assertIn("dispatch_interrupted", plan_state.core_reentry_facts)
        self.assertIn("judgment_evidence_unavailable", plan_state.core_reentry_facts)
        self.assertEqual(plan_state.mechanical_blocker, "judgment_evidence_unavailable")
        self.assertEqual(plan_state.last_result_call_id, closed[0].call.call_id)
        # Her evidence is the closed result, not an open dispatch.
        self.assertIs(self.reasoner.contexts[0].transient_attempts[0].disposition,
                      CapabilityAttemptDisposition.BROKER_FAILURE)
        return state

    def test_interrupted_reread_is_closed_and_classified_before_she_responds(self):
        self.woken_then_reread_raises()
        outcome = self.resume(AgentDecision(response="I could not read it.", goal_id="goal"))
        self.assertEqual(outcome.response, "I could not read it.")
        self.assert_closed_and_classified()

    def test_direct_call_after_interrupted_reread_is_refused_without_a_second_dispatch(self):
        # CodeRabbit's second-PENDING crash: not reachable, because the blocker
        # refuses the call before any dispatch is checkpointed.
        self.woken_then_reread_raises()
        outcome = self.resume(AgentDecision(call=CapabilityCall("merge-now", "merge", {}),
                                            goal_id="goal"))
        self.assertEqual(outcome.reason, "judgment_evidence_unavailable")
        self.assertNotIn("merge", self.calls)
        state = self.assert_closed_and_classified()
        self.assertFalse(any(item.call is not None and item.call.call_id == "merge-now"
                             for item in state.attempts))

    def test_parking_after_interrupted_reread_keeps_outstanding_work(self):
        work = (WorkItem("judge", "Judge the review"),)
        cases = {
            "refused_direct_call": (AgentDecision(call=CapabilityCall("merge-p", "merge", {}),
                                                  goal_id="goal"),),
            "call_less_end": (AgentDecision(response="I could not read it.", goal_id="goal"),
                              AgentDecision(response="I could not read it.", goal_id="goal")),
        }
        for name, decisions in cases.items():
            with self.subTest(case=name):
                self.reset_goal()
                self.calls.clear()
                self.woken_then_reread_raises(outstanding_work=work)
                outcome = self.resume(*decisions)
                state = self.assert_closed_and_classified()
                self.assertIs(state.status, GoalStatus.AWAITING_INPUT)
                self.assertEqual(state.outstanding_work, work)
                self.assertNotIn("merge", self.calls)
                if name == "call_less_end":
                    self.assertEqual(outcome.response, "I could not read it.")

    def test_gateway_stores_the_response_after_an_interrupted_reread(self):
        self.woken_then_reread_raises(outstanding_work=(WorkItem("judge", "Judge the review"),))
        conversations = SQLiteConversationStore(self.path.with_name("conversation.sqlite3"))
        self.addCleanup(conversations.close)
        created = conversations.create("thread", RETENTION)
        conversations.append(conversation().turns[0], RETENTION, created.revision)

        class Ledger:
            def exists(self, _identifier):
                return False

        opportunity = PlanContinuationSource(self.store, Ledger(), enabled=True).due_opportunities()[0]
        gateway = ConversationGateway(self.agent(Reasoner(
            AgentDecision(response="I could not read it.", goal_id="goal"),
            AgentDecision(response="I could not read it.", goal_id="goal"),
        ), extra_definitions=(REVIEW_CONTENT_DEFINITION,)), conversations)
        gateway.receive_cognition_opportunity("thread", opportunity, 2, RETENTION)
        self.assertEqual(conversations.load("thread").turns[-1].content, "I could not read it.")
        state = self.store.load("goal").state
        self.assertFalse(any(item.disposition is CapabilityAttemptDisposition.PENDING
                             for item in state.attempts))
        self.assertIs(state.status, GoalStatus.AWAITING_INPUT)


class PlanContinuationAvailabilityTests(PlanHarness):
    """CodeRabbit 85c4b6c #1: no plan runs where it can never return to her."""

    def test_without_continuation_no_plan_is_installed_and_she_answers(self):
        reasoner = Reasoner(
            AgentDecision(execution_plan=plan(step("coding"), step("merge")), goal_id="goal"),
            AgentDecision(response="I cannot run that in the background here.", goal_id="goal"),
        )
        outcome = self.agent(reasoner, plan_continuation=False).process(
            conversation(), RETENTION, 3)
        self.assertEqual(outcome.response, "I cannot run that in the background here.")
        self.assertIsNone(self.store.load("goal").state.execution_plan)
        self.assertEqual(self.calls, [])
        refusals = reasoner.contexts[1].refused_calls
        self.assertEqual([item["reason"] for item in refusals], ["plan_continuation_unavailable"])

    def test_without_continuation_ticks_never_dispatch_an_existing_plan(self):
        snapshot = self.store.load("goal")
        self.store.replace(replace(snapshot.state, execution_plan=plan(step("merge"))),
                           snapshot.retention_until, snapshot.revision)
        agent = self.agent(Reasoner(), plan_continuation=False, effectful=frozenset({"merge"}))
        for _ in range(3):
            agent.advance_due_plans(lambda _: conversation())
        self.assertEqual(self.calls, [])
        self.assertEqual(self.store.load("goal").state.execution_plan.status, "ready")

    def test_without_continuation_ordinary_calls_are_unaffected(self):
        reasoner = Reasoner(AgentDecision(call=CapabilityCall("ci-1", "ci", {}), goal_id="goal"),
                            AgentDecision(response="Checks read.", goal_id="goal"))
        outcome = self.agent(reasoner, plan_continuation=False).process(
            conversation(), RETENTION, 3)
        self.assertEqual(self.calls, ["ci"])
        self.assertEqual(outcome.response, "Checks read.")

    def test_with_continuation_the_plan_installs_and_runs(self):
        reasoner = Reasoner(AgentDecision(execution_plan=plan(step("coding")), goal_id="goal"),
                            AgentDecision(response="Done.", goal_id="goal"))
        self.agent(reasoner, plan_continuation=True).process(conversation(), RETENTION, 3)
        self.assertEqual(self.calls, ["coding"])

    def test_runtime_binds_installation_and_continuation_to_one_condition(self):
        source = (Path(__file__).resolve().parents[1] / "src" / "alx" / "bootstrap"
                  / "live_voice.py").read_text()
        self.assertIn("plan_continuation = providers.autonomous is not None", source)
        self.assertIn("plan_continuation=plan_continuation,", source)
        self.assertIn("goal_store, opportunity_ledger, enabled=plan_continuation,", source)


class PersonTurnPlanAcknowledgementTests(PlanHarness):
    """CodeRabbit 85c4b6c #2: a person who starts work always hears one response."""

    def waiting_plan(self):
        self.outputs["ci"] = {"state": "pending"}
        return plan(step("ci", completion=(PlanCondition("values.state", "passed"),),
                         waiting=(PlanCondition("values.state", "pending"),)), step("merge"))

    def test_person_turn_gets_exactly_one_response_and_the_wait_continues(self):
        reasoner = Reasoner(AgentDecision(execution_plan=self.waiting_plan(), goal_id="goal"),
                            ACK, AgentDecision(response="A second answer.", goal_id="goal"))
        outcome = self.agent(reasoner).process(conversation(), RETENTION, 3)
        self.assertEqual(outcome.state, CoreState.RESPONDED)
        self.assertEqual(outcome.response, ACK.response)
        self.assertEqual(outcome.reason, "plan_waiting")
        self.assertEqual(reasoner.calls, 2)
        self.assertEqual(reasoner.contexts[1].response_only_reason, "plan_waiting")
        stored = self.store.load("goal").state
        self.assertEqual((stored.execution_plan.status, stored.execution_plan.cursor),
                         ("waiting", 0))
        self.assertIs(stored.status, GoalStatus.ACTIVE)
        self.assertEqual(self.calls, ["ci"])

    def test_response_only_pass_cannot_dispatch_or_change_the_plan(self):
        reasoner = Reasoner(
            AgentDecision(execution_plan=self.waiting_plan(), goal_id="goal"),
            AgentDecision(call=CapabilityCall("merge-now", "merge", {}), goal_id="goal"),
        )
        outcome = self.agent(reasoner).process(conversation(), RETENTION, 3)
        self.assertEqual(outcome.state, CoreState.CHECKPOINTED)
        self.assertEqual(reasoner.calls, 2)
        self.assertEqual(self.calls, ["ci"])
        stored = self.store.load("goal").state.execution_plan
        self.assertEqual((stored.status, stored.cursor), ("waiting", 0))

    def test_autonomous_turn_stays_silent(self):
        reasoner = Reasoner(AgentDecision(execution_plan=self.waiting_plan(), goal_id="goal"))
        outcome = self.agent(reasoner).process(conversation(), RETENTION, 3,
                                               origin=CognitionOrigin.EXTERNAL_EVENT)
        self.assertEqual(outcome.state, CoreState.CHECKPOINTED)
        self.assertEqual(outcome.reason, "plan_waiting")
        self.assertEqual(reasoner.calls, 1)

    def test_person_turn_hears_a_response_when_the_first_dispatch_fails(self):
        def explode(_call):
            raise RuntimeError("executor crashed")

        self.outputs["coding"] = explode
        reasoner = Reasoner(AgentDecision(execution_plan=plan(step("coding")), goal_id="goal"),
                            AgentDecision(response="The job did not start.", goal_id="goal"))
        outcome = self.agent(reasoner).process(conversation(), RETENTION, 3)
        self.assertEqual(outcome.response, "The job did not start.")
        self.assertEqual(outcome.reason, "plan_inactive")
        self.assertEqual(reasoner.calls, 2)

    def test_person_turn_blocked_by_a_plan_blocker_hears_a_response(self):
        self.outputs["request_external_review"] = (
            CapabilityResultState.FAILED,
            {"code": "review_pending", "requires_judgement": True},
        )
        self.agent(Reasoner(AgentDecision(
            execution_plan=plan(step("request_external_review"), step("merge")), goal_id="goal",
        ))).process(conversation(), RETENTION, 1, origin=CognitionOrigin.EXTERNAL_EVENT)
        reasoner = Reasoner(
            AgentDecision(call=CapabilityCall("merge-p", "merge", {}), goal_id="goal"),
            AgentDecision(response="The review is still pending.", goal_id="goal"),
        )
        outcome = self.agent(reasoner).process(conversation(), RETENTION, 3)
        self.assertEqual(outcome.state, CoreState.RESPONDED)
        self.assertEqual(outcome.response, "The review is still pending.")
        self.assertEqual(outcome.reason, "review_pending")
        self.assertNotIn("merge", self.calls)


class RuntimeOwnedSourceTurnTests(PlanHarness):
    """CodeRabbit 85c4b6c #4: the turn a plan answers is bound by the runtime."""

    def long_thread(self):
        turns = [ConversationTurn("thread", "person-1", ConversationOrigin.TYPED,
                                  "Please do the work", NOW, "friedl"),
                 ConversationTurn("thread", "person-2", ConversationOrigin.TYPED,
                                  "And merge it when ready", NOW, "friedl")]
        turns += [ConversationTurn("thread", f"alx-{index}", ConversationOrigin.ALX_RESPONSE,
                                   f"Update {index}", NOW)
                  for index in range(REASONING_TURN_WINDOW + 3)]
        return ConversationSnapshot("thread", tuple(turns), len(turns), RETENTION)

    def test_plan_installs_after_more_than_a_window_of_her_turns(self):
        thread = self.long_thread()
        # The latest person turn is outside what reasoning was shown.
        projected = project_turns_for_reasoning(thread.turns, self.store.load("goal").state)
        self.assertNotIn("person-2", [item.turn_id for item in projected])
        proposed = replace(plan(step("coding")), source_turn_id=None)
        reasoner = Reasoner(AgentDecision(execution_plan=proposed, goal_id="goal"),
                            AgentDecision(response="Done.", goal_id="goal"))
        self.agent(reasoner).process(thread, RETENTION, 3)
        self.assertEqual(self.calls, ["coding"])
        self.assertEqual(self.store.load("goal").state.execution_plan.source_turn_id, "person-2")

    def test_model_cannot_choose_the_source_turn(self):
        forged = replace(plan(step("coding")), source_turn_id="someone-else")
        reasoner = Reasoner(AgentDecision(execution_plan=forged, goal_id="goal"),
                            AgentDecision(response="Done.", goal_id="goal"))
        self.agent(reasoner).process(conversation(), RETENTION, 3)
        self.assertEqual(self.store.load("goal").state.execution_plan.source_turn_id, "person-1")

    def test_a_later_person_turn_still_invalidates_the_stamped_plan(self):
        self.outputs["ci"] = {"state": "pending"}
        workflow = plan(step("ci", completion=(PlanCondition("values.state", "passed"),),
                             waiting=(PlanCondition("values.state", "pending"),)))
        agent = self.agent(Reasoner(AgentDecision(execution_plan=workflow, goal_id="goal"), ACK))
        agent.process(conversation(), RETENTION, 3)
        self.now += timedelta(seconds=10)
        agent.advance_due_plans(lambda _: conversation("person-9"))
        self.assertEqual(self.store.load("goal").state.execution_plan.core_reentry_reason,
                         "new_person_turn")

    def test_acknowledgement_does_not_park_background_work(self):
        snapshot = self.store.load("goal")
        self.store.replace(replace(snapshot.state,
                                   outstanding_work=(WorkItem("merge", "Merge when green"),)),
                           snapshot.retention_until, snapshot.revision)
        self.outputs["ci"] = {"state": "pending"}
        workflow = plan(step("ci", completion=(PlanCondition("values.state", "passed"),),
                             waiting=(PlanCondition("values.state", "pending"),)))
        outcome = self.agent(Reasoner(AgentDecision(execution_plan=workflow, goal_id="goal"),
                                      ACK)).process(conversation(), RETENTION, 3)
        self.assertEqual(outcome.response, ACK.response)
        state = self.store.load("goal").state
        self.assertIs(state.status, GoalStatus.ACTIVE)
        self.assertEqual(state.execution_plan.status, "waiting")



class RuntimeOwnedObjectiveTests(PlanHarness):
    """The objective a plan serves is the goal's record, never the model's copy."""

    def test_model_cannot_alter_the_objective_or_its_source(self):
        forged = replace(plan(step("coding")), objective_source="turn:forged",
                         objective_summary="Something else entirely")
        reasoner = Reasoner(AgentDecision(execution_plan=forged, goal_id="goal"),
                            AgentDecision(response="Done.", goal_id="goal"))
        self.agent(reasoner).process(conversation(), RETENTION, 3)
        installed = self.store.load("goal").state.execution_plan
        self.assertEqual((installed.objective_source, installed.objective_summary),
                         ("turn:person-1", "Do the work"))
        self.assertEqual(self.calls, ["coding"])

    def test_imperfect_restatement_cannot_cause_a_refusal(self):
        for summary in (None, "Do the work.", "do the work", "Do the"):
            with self.subTest(summary=summary):
                self.reset_goal()
                proposal = replace(plan(step("coding")), objective_summary=summary,
                                   objective_source=None)
                reasoner = Reasoner(AgentDecision(execution_plan=proposal, goal_id="goal"),
                                    AgentDecision(response="Done.", goal_id="goal"))
                self.agent(reasoner).process(conversation(), RETENTION, 3)
                self.assertEqual(self.calls, ["coding"])
                self.assertEqual(reasoner.contexts[1].refused_calls, ())

    def test_a_real_objective_change_still_invalidates_the_plan(self):
        self.outputs["ci"] = {"state": "pending"}
        workflow = plan(step("ci", completion=(PlanCondition("values.state", "passed"),),
                             waiting=(PlanCondition("values.state", "pending"),)))
        agent = self.agent(Reasoner(AgentDecision(execution_plan=workflow, goal_id="goal"),
                                    ACK))
        agent.process(conversation(), RETENTION, 3)
        snapshot = self.store.load("goal")
        self.store.replace(replace(snapshot.state,
                                   objective=Objective("turn:person-1", "Different work")),
                           snapshot.retention_until, snapshot.revision)
        self.now += timedelta(seconds=10)
        agent.advance_due_plans(lambda _: conversation())
        stored = self.store.load("goal").state.execution_plan
        self.assertEqual(stored.core_reentry_reason, "plan_precondition_changed")
        self.assertEqual(self.calls, ["ci"])

    def test_a_stored_plan_must_be_bound_to_its_goals_objective(self):
        unbound = replace(plan(step("coding")), objective_source=None, objective_summary=None)
        with self.assertRaisesRegex(ValueError, "bound to its objective"):
            replace(goal(), execution_plan=unbound)
