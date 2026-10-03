"""D-036 execution plans: the simplified lifecycle and its acceptance invariants.

RUNNING / WAITING are the executor's; NEEDS_CORE is AL/X's; COMPLETED and
CANCELLED are terminal. A plan wakes her only through its own attention, and
only her explicit resolution of that exact attention moves it on.
"""

from __future__ import annotations

import ast
import asyncio
import sys
import tempfile
import threading
import unittest
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from alx.contracts import (  # noqa: E402
    AgentDecision, Approval, ApprovalLifecycle, ApprovalScope, CapabilityAttempt,
    CapabilityAttemptDisposition, CapabilityCall, CapabilityDefinition, CapabilityResult,
    CapabilityResultState, CognitionOrigin, ConversationOrigin, ConversationSnapshot,
    ConversationTurn, ExecutionOutcome, ExecutionPlan, ExecutionStep, GoalMutationKind,
    GoalProposal, GoalState, GoalStatus, GoalStopReason, Objective, PlanAttention,
    PlanCondition, PlanDispatch, PlanOperation, PlanStatus, PlanUpdate, SideEffect,
    StructuredSchema, SuccessCriterion, ValueKind, WorkItem,
)
from alx.contracts.pull_request_checks import (  # noqa: E402
    CheckRun, CommitStatus, PullRequestChecks,
)
from alx.contracts.review_content import (  # noqa: E402
    REVIEW_FAILED, REVIEW_IN_PROGRESS, ReviewContent,
)
from alx.continuity.plan_source import (  # noqa: E402
    MAX_PAID_PLAN_OFFERS, PlanAttentionSource, PlanWorkers,
)
from alx.conversation import ConversationGateway, SQLiteConversationStore  # noqa: E402
from alx.core import CoreAgent, CoreState  # noqa: E402
from alx.core.plan_results import (  # noqa: E402
    PlanResultKind, classify_planned_result, condition_matches, json_equal,
    plan_invalidation_facts,
)
from alx.goals import SQLiteGoalStore  # noqa: E402
from alx.tools.pull_request_checks import (  # noqa: E402
    DEFINITION as CHECKS_DEFINITION, build_pull_request_checks_executors,
)
from alx.tools.review_content import (  # noqa: E402
    DEFINITION as REVIEW_DEFINITION, build_review_content_executors,
)

NOW = datetime(2026, 10, 1, tzinfo=UTC)
RETENTION = NOW + timedelta(days=30)
SCHEMA = StructuredSchema(ValueKind.OBJECT)
HEAD = "a" * 40
SRC = Path(__file__).resolve().parents[1] / "src" / "alx"

# Effectful work, repeat-safe observations, and plain reads.
EFFECTFUL = ("coding", "merge", "cleanup")
OBSERVATIONS = {"ci": SideEffect.NONE, "review": SideEffect.EFFECTFUL}
DEFINITIONS = (
    *(CapabilityDefinition(name, name, SCHEMA, SCHEMA, SideEffect.EFFECTFUL)
      for name in EFFECTFUL),
    *(CapabilityDefinition(name, name, SCHEMA, SCHEMA, effect, plan_observation=True)
      for name, effect in OBSERVATIONS.items()),
    CapabilityDefinition("other", "other", SCHEMA, SCHEMA, SideEffect.NONE),
)


def conversation(*person_turns: str) -> ConversationSnapshot:
    turns = tuple(
        ConversationTurn("thread", turn_id, ConversationOrigin.TYPED, "Please do the work",
                         NOW, "friedl")
        for turn_id in (person_turns or ("person-1",))
    )
    return ConversationSnapshot("thread", turns, 1, RETENTION)


def new_goal(goal_id: str = "goal", **changes) -> GoalState:
    return replace(GoalState(goal_id, Objective("turn:person-1", "Do the work"),
                             (SuccessCriterion("done", "verified"),)), **changes)


def step(name: str, *, wait: int = 0, bound: int = 0, wake: bool = False,
         completion=(), approval_id=None) -> ExecutionStep:
    return ExecutionStep(CapabilityCall(f"call-{name}", name, {}, approval_id),
                         tuple(completion), wait, bound or (600 if wait else 0), wake)


def plan(*steps: ExecutionStep, **changes) -> ExecutionPlan:
    return replace(ExecutionPlan("plan", None, None, None, tuple(steps)), **changes)


def install(workflow: ExecutionPlan, response="Started.") -> AgentDecision:
    return AgentDecision(response=response, goal_id="goal",
                         plan_update=PlanUpdate(PlanOperation.INSTALL, workflow))


def resolve(operation: PlanOperation, response="Understood.") -> AgentDecision:
    return AgentDecision(response=response, goal_id="goal",
                         plan_update=PlanUpdate(operation))


SELECT = AgentDecision(goal_id="goal")


def outcome(state=CapabilityResultState.SUCCEEDED, result_outcome=None, values=None,
            failure=None):
    """What a fake capability returns, as its result would say it."""
    return state, result_outcome, values or {}, failure


SUCCESS = outcome()
PENDING = outcome(result_outcome=ExecutionOutcome.PENDING)
JUDGE = outcome(result_outcome=ExecutionOutcome.AMBIGUOUS, values={"findings": ["fix"]})
FAILED = outcome(CapabilityResultState.FAILED, failure={"code": "failed"})


class Reasoner:
    def __init__(self, *decisions):
        self.decisions = list(decisions)
        self.contexts = []

    @property
    def calls(self):
        return len(self.contexts)

    def decide(self, context):
        self.contexts.append(context)
        return self.decisions.pop(0)


class PlanHarness(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.path = Path(directory.name) / "goals.sqlite3"
        self.now = NOW
        self.store = self.open_store()
        self.store.create(new_goal(), "thread", RETENTION)
        self.calls: list[CapabilityCall] = []
        self.outputs: dict[str, object] = {}
        self.budget_stopped = False

    def open_store(self):
        store = SQLiteGoalStore(self.path, clock=lambda: self.now)
        self.addCleanup(store.close)
        return store

    def restart(self):
        """A new process: durable state only, no live worker, no cache."""
        self.store.close()
        self.store = self.open_store()

    def dispatch(self, call, _state):
        self.calls.append(call)
        value = self.outputs.get(call.capability_id, SUCCESS)
        if isinstance(value, list):
            value = value.pop(0)
        if callable(value):
            return value(call)
        state, result_outcome, values, failure = value
        return CapabilityAttempt(call, CapabilityAttemptDisposition.EXECUTED, True,
                                 CapabilityResult(call.call_id, call.capability_id, state,
                                                  values, failure, outcome=result_outcome))

    def budget(self, _conversation_id):
        if self.budget_stopped:
            raise RuntimeError("budget exceeded")

    def agent(self, reasoner=None, **options):
        options.setdefault("plan_continuation", True)
        return CoreAgent(self.store, reasoner or Reasoner(), self.dispatch,
                         options.pop("definitions", DEFINITIONS),
                         clock=lambda: self.now,
                         budget_check=options.pop("budget_check", self.budget), **options)

    @staticmethod
    def same_process(agent, reasoner):
        """The one Core of a running process, deciding with another reasoner."""
        agent._reasoner = reasoner
        return agent

    def work(self, agent):
        """Every due step, run to the next wait or attention, as workers would."""
        jobs = list(agent.advance_due_plans())
        while jobs:
            job = agent.begin_planned_dispatch(jobs.pop(0))
            if job is not None:
                jobs.extend(agent.finish_planned_dispatch(job, agent.run_planned_dispatch(job)))

    def person(self, agent, *turns, budget=4):
        return agent.process(conversation(*turns), RETENTION, budget)

    def occasion(self, agent, budget=4):
        return agent.process(conversation(), RETENTION, budget,
                             origin=CognitionOrigin.WORK_COMPLETED,
                             resume_plan_goal_id="goal")

    def state(self, goal_id="goal"):
        return self.store.load(goal_id).state

    def plan_of(self, goal_id="goal"):
        return self.state(goal_id).execution_plan

    def names(self):
        return [call.capability_id for call in self.calls]

    def later(self, seconds):
        self.now += timedelta(seconds=seconds)

    def set_goal(self, goal_id="goal", **changes):
        snapshot = self.store.load(goal_id)
        return self.store.replace(replace(snapshot.state, **changes),
                                  snapshot.retention_until, snapshot.revision)

    def installed(self, *steps, agent=None, **changes):
        """Install a plan through a real person turn and return the agent."""
        agent = agent or self.agent(Reasoner(install(plan(*steps, **changes))))
        outcome_ = self.person(agent)
        self.assertEqual(outcome_.state, CoreState.RESPONDED, outcome_.reason)
        return agent


class LifecycleTests(PlanHarness):
    """Acceptance invariants 1 and 2: Core only for judgment, and a wake never ends a plan."""

    def test_core_is_called_only_for_judgment_and_completion(self):
        reasoner = Reasoner(
            install(plan(step("coding"), step("ci", wait=10), step("review", wait=10),
                         step("merge"), step("cleanup"))),
            resolve(PlanOperation.ACCEPT, "The finding is acceptable; merging."),
            resolve(PlanOperation.FINISH, "Merged and cleaned up."),
        )
        agent = self.agent(reasoner)
        self.outputs = {"ci": [PENDING, PENDING, SUCCESS], "review": [PENDING, JUDGE]}
        self.person(agent)
        self.work(agent)
        for _ in range(3):
            self.later(10)
            self.work(agent)
        attention = self.plan_of().attention
        self.assertEqual(attention.reason, "planned_evidence_requires_judgement")
        self.assertEqual(reasoner.calls, 1)
        # The judgment wake shows her the full result it was raised for.
        self.occasion(agent)
        evidence = reasoner.contexts[1].transient_attempts
        self.assertEqual(evidence[-1].result.values["findings"], ("fix",))
        self.work(agent)
        self.assertEqual(self.plan_of().attention.reason, "plan_steps_done")
        self.occasion(agent)
        self.assertEqual(reasoner.calls, 3)
        self.assertEqual(self.plan_of().status, PlanStatus.COMPLETED)
        self.assertEqual(self.names(), ["coding", "ci", "ci", "ci", "review", "review",
                                        "merge", "cleanup"])

    def test_a_wake_answered_without_resolution_keeps_its_exact_attention(self):
        reasoner = Reasoner(install(plan(step("coding"), step("merge"))),
                            AgentDecision(response="Looking into the failure.", goal_id="goal"),
                            resolve(PlanOperation.RESUME))
        agent = self.agent(reasoner)
        self.outputs["coding"] = [FAILED, SUCCESS]
        self.person(agent)
        self.work(agent)
        before = self.plan_of()
        self.assertEqual((before.status, before.attention.reason),
                         (PlanStatus.NEEDS_CORE, "planned_result_failed"))
        self.occasion(agent)
        after = self.plan_of()
        self.assertEqual((after.status, after.attention_seq, after.cursor),
                         (PlanStatus.NEEDS_CORE, before.attention_seq, before.cursor))
        # The same plan continues once she resumes it: the step is retried.
        self.occasion(agent)
        self.work(agent)
        self.assertEqual(self.names(), ["coding", "coding", "merge"])
        self.assertEqual(self.plan_of().attention.reason, "plan_steps_done")

    def test_a_plan_installed_by_a_person_runs_after_the_turn_not_inside_it(self):
        agent = self.installed(step("coding"))
        self.assertEqual(self.calls, [])
        self.assertEqual(self.plan_of().status, PlanStatus.RUNNING)
        self.work(agent)
        self.assertEqual(self.names(), ["coding"])

    def test_accept_runs_on_past_a_judged_step_and_resume_runs_it_again(self):
        for operation, expected in ((PlanOperation.ACCEPT, ["review", "merge"]),
                                    (PlanOperation.RESUME, ["review", "review"])):
            with self.subTest(operation=operation.value):
                self.set_goal(execution_plan=None, attempts=())
                self.calls.clear()
                reasoner = Reasoner(install(plan(step("review", wait=10), step("merge"))),
                                    resolve(operation))
                agent = self.agent(reasoner)
                self.outputs["review"] = [JUDGE, JUDGE]
                self.person(agent)
                self.work(agent)
                self.occasion(agent)
                self.work(agent)
                self.assertEqual(self.names(), expected)

    def test_accept_cannot_run_past_the_last_step(self):
        reasoner = Reasoner(install(plan(step("review", wait=10))),
                            resolve(PlanOperation.ACCEPT),
                            resolve(PlanOperation.FINISH, "Done."))
        agent = self.agent(reasoner)
        self.outputs["review"] = JUDGE
        self.person(agent)
        self.work(agent)
        self.occasion(agent)
        self.assertEqual(reasoner.contexts[2].refused_calls[0]["reason"],
                         "plan_has_no_remaining_steps")
        self.assertEqual(self.plan_of().status, PlanStatus.COMPLETED)

    def test_checkpoint_step_advances_and_then_wakes_her_with_its_result(self):
        agent = self.installed(step("coding", wake=True), step("merge"))
        self.outputs["coding"] = outcome(values={"branch": "feature"})
        self.work(agent)
        current = self.plan_of()
        self.assertEqual((current.cursor, current.attention.reason), (1, "plan_checkpoint"))
        self.assertEqual(self.names(), ["coding"])


class UnrelatedActivityTests(PlanHarness):
    """Acceptance invariant 3: ordinary Core activity has no effect on a running plan."""

    def test_ordinary_activity_on_the_goal_leaves_a_waiting_plan_untouched(self):
        agent = self.installed(step("ci", wait=10), step("merge"))
        self.outputs["ci"] = [PENDING, outcome(values={"other": True}), SUCCESS]
        self.work(agent)
        before = self.plan_of()
        self.assertEqual(before.status, PlanStatus.WAITING)
        reasoner = Reasoner(
            # The same capability, called directly with other arguments.
            AgentDecision(call=CapabilityCall("direct-ci", "ci", {"pr": 7}), goal_id="goal"),
            # A goal update that touches nothing the plan declared.
            AgentDecision(response="Noted.", goal_id="goal", goal_proposal=GoalProposal(
                GoalMutationKind.UPDATE,
                outstanding_work=(WorkItem("merge", "merge after CI"),))),
        )
        self.person(self.agent(reasoner), "person-1", "person-2")
        after = self.plan_of()
        self.assertEqual(
            (after.status, after.cursor, after.next_due_at, after.attention_seq),
            (before.status, before.cursor, before.next_due_at, before.attention_seq))
        self.later(10)
        self.work(agent)
        self.assertEqual(self.names(), ["ci", "ci", "ci", "merge"])
        self.assertEqual(self.plan_of().attention.reason, "plan_steps_done")

    def test_a_turn_ending_with_outstanding_work_does_not_park_a_running_plan(self):
        self.set_goal(outstanding_work=(WorkItem("merge", "merge after CI"),))
        self.installed(step("ci", wait=10))
        self.assertIs(self.state().status, GoalStatus.ACTIVE)
        self.assertEqual(self.plan_of().status, PlanStatus.RUNNING)


class PreconditionTests(PlanHarness):
    """Acceptance invariant 4: only a declared precondition change wakes her."""

    def test_changed_context_precondition_wakes_once_at_the_next_boundary(self):
        self.set_goal(context={"head": HEAD})
        agent = self.installed(step("ci", wait=10), step("merge"),
                               context_preconditions={"head": HEAD})
        self.outputs["ci"] = [PENDING, SUCCESS]
        self.work(agent)
        self.set_goal(context={"head": "b" * 40})
        self.later(10)
        self.work(agent)
        self.work(agent)
        current = self.plan_of()
        self.assertEqual((current.attention.reason, current.attention_seq),
                         ("plan_precondition_changed", 1))
        self.assertEqual(self.names(), ["ci"])

    def test_withdrawn_approval_wakes_her_before_the_step_runs(self):
        scope = ApprovalScope("merge", {})
        self.set_goal(approvals=(Approval("ok", scope, ApprovalLifecycle.GRANTED),))
        agent = self.installed(step("coding"), step("merge", approval_id="ok"))
        self.outputs["coding"] = lambda call: (
            self.set_goal(approvals=(Approval("ok", scope, ApprovalLifecycle.WITHDRAWN),))
            and None) or self.dispatch_success(call)
        self.work(agent)
        self.assertEqual(self.plan_of().attention.reason, "plan_approval_invalid")
        self.assertEqual(self.names(), ["coding"])

    def dispatch_success(self, call):
        return CapabilityAttempt(call, CapabilityAttemptDisposition.EXECUTED, True,
                                 CapabilityResult(call.call_id, call.capability_id,
                                                  CapabilityResultState.SUCCEEDED))

    def test_invalidation_facts_ignore_person_turns_and_unrelated_fields(self):
        workflow = plan(step("ci"), objective_source="turn:person-1",
                        objective_summary="Do the work", source_turn_id="person-1")
        state = new_goal(context={"unrelated": 1},
                         outstanding_work=(WorkItem("x", "x"),))
        self.assertEqual(plan_invalidation_facts(workflow, state, NOW), ())
        self.assertEqual(
            plan_invalidation_facts(workflow, replace(state, status=GoalStatus.AWAITING_INPUT,
                                                      stop_reason=GoalStopReason.REQUIRED_INPUT,
                                                      outstanding_work=(WorkItem("x", "x"),)),
                                    NOW), ("goal_inactive",))


class ResultIdentityTests(PlanHarness):
    """Acceptance invariant 5: a result moves the plan only by its in-flight identity."""

    def cancel(self, agent):
        self.person(self.same_process(
            agent, Reasoner(SELECT, resolve(PlanOperation.CANCEL, "Stopped."))))
        self.assertEqual(self.plan_of().status, PlanStatus.CANCELLED)

    def replace_plan(self, agent):
        self.person(self.same_process(
            agent, Reasoner(SELECT, install(plan(step("cleanup")), "Replaced."))))

    def test_cancel_before_the_dispatch_boundary_means_the_call_never_happens(self):
        for stop in (self.cancel, self.replace_plan):
            with self.subTest(stop=stop.__name__):
                self.set_goal(execution_plan=None, attempts=())
                self.calls.clear()
                agent = self.installed(step("coding"), step("merge"))
                (job,) = agent.advance_due_plans()
                stop(agent)
                self.assertIsNone(agent.begin_planned_dispatch(job))
                self.assertNotIn("coding", self.names())
                (closed,) = [item for item in self.state().attempts
                             if item.call.call_id == job.call.call_id]
                self.assertEqual((closed.reason_code, closed.implementation_invoked),
                                 ("plan_dispatch_withdrawn", False))

    def test_cancel_after_the_boundary_records_the_result_and_runs_nothing_more(self):
        agent = self.installed(step("coding"), step("merge"))
        (job,) = agent.advance_due_plans()
        job = agent.begin_planned_dispatch(job)
        self.assertTrue(self.plan_of().inflight.started)
        self.cancel(agent)
        self.assertEqual(agent.finish_planned_dispatch(job, agent.run_planned_dispatch(job)), ())
        (recorded,) = [item for item in self.state().attempts
                       if item.call.call_id == job.call.call_id]
        self.assertEqual(recorded.disposition, CapabilityAttemptDisposition.EXECUTED)
        self.assertEqual(self.plan_of().status, PlanStatus.CANCELLED)
        self.work(agent)
        self.assertEqual(self.names(), ["coding"])

    def test_a_started_dispatch_is_never_begun_twice_or_relabelled(self):
        agent = self.installed(step("coding"))
        (job,) = agent.advance_due_plans()
        self.assertIsNotNone(agent.begin_planned_dispatch(job))
        self.assertIsNone(agent.begin_planned_dispatch(job))
        (pending,) = [item for item in self.state().attempts
                      if item.call.call_id == job.call.call_id]
        self.assertIs(pending.disposition, CapabilityAttemptDisposition.PENDING)

    def test_late_result_of_a_replaced_plan_moves_nothing(self):
        agent = self.installed(step("coding"))
        (job,) = agent.advance_due_plans()
        job = agent.begin_planned_dispatch(job)
        self.replace_plan(agent)
        replacement = self.plan_of()
        agent.finish_planned_dispatch(job, agent.run_planned_dispatch(job))
        self.assertEqual(self.plan_of(), replacement)

    def test_an_approval_withdrawn_before_the_boundary_stops_the_call(self):
        scope = ApprovalScope("merge", {})
        self.set_goal(approvals=(Approval("ok", scope, ApprovalLifecycle.GRANTED),))
        agent = self.installed(step("merge", approval_id="ok"))
        (job,) = agent.advance_due_plans()
        self.assertIs(next(item for item in self.state().approvals).lifecycle,
                      ApprovalLifecycle.CLAIMED)
        self.set_goal(approvals=(Approval("ok", scope, ApprovalLifecycle.WITHDRAWN),))
        self.assertIsNone(agent.begin_planned_dispatch(job))
        self.assertEqual(self.calls, [])
        self.assertEqual(self.plan_of().attention.reason, "plan_approval_invalid")

    def test_a_claimed_approval_reaches_the_gate_as_the_grant_it_is(self):
        scope = ApprovalScope("merge", {})
        self.set_goal(approvals=(Approval("ok", scope, ApprovalLifecycle.GRANTED),))
        agent = self.installed(step("merge", approval_id="ok"))
        (job,) = agent.advance_due_plans()
        started = agent.begin_planned_dispatch(job)
        (approval,) = started.authority.approvals
        self.assertIs(approval.lifecycle, ApprovalLifecycle.GRANTED)
        agent.finish_planned_dispatch(started, agent.run_planned_dispatch(started))
        self.assertIs(self.state().approvals[0].lifecycle, ApprovalLifecycle.CONSUMED)

    def test_a_direct_call_on_the_goal_is_refused_while_a_step_is_in_flight(self):
        agent = self.installed(step("coding"))
        (job,) = agent.advance_due_plans()
        reasoner = Reasoner(
            SELECT,
            AgentDecision(call=CapabilityCall("direct", "cleanup", {}), goal_id="goal"),
            AgentDecision(response="It is still running.", goal_id="goal"),
        )
        self.person(self.same_process(agent, reasoner))
        self.assertEqual(reasoner.contexts[2].refused_calls[0]["reason"], "plan_step_in_flight")
        job = agent.begin_planned_dispatch(job)
        agent.finish_planned_dispatch(job, agent.run_planned_dispatch(job))
        self.assertEqual(self.names(), ["coding"])
        self.assertEqual(self.plan_of().attention.reason, "plan_steps_done")

    def test_every_dispatch_has_its_own_identity_written_before_the_call(self):
        agent = self.installed(step("ci", wait=10))
        self.outputs["ci"] = [PENDING, PENDING]
        (job,) = agent.advance_due_plans()
        self.assertEqual(self.plan_of().inflight, PlanDispatch(0, job.call.call_id))
        agent.finish_planned_dispatch(job, agent.run_planned_dispatch(job))
        self.later(10)
        (second,) = agent.advance_due_plans()
        self.assertNotEqual(job.call.call_id, second.call.call_id)


class FailureContractTests(PlanHarness):
    """Acceptance invariant 6: outcome comes from the capability, never condition syntax."""

    def check_reader(self, *runs, statuses=()):
        def read(_request):
            return PullRequestChecks(95, HEAD, tuple(runs), tuple(statuses))
        executor = build_pull_request_checks_executors(read, lambda: "read-1")
        return executor["read_pull_request_checks"]({"pull_request_number": 95,
                                                     "head_sha": HEAD})

    @staticmethod
    def run_(status, conclusion=None, steps=None):
        return CheckRun("check", status, conclusion, None, None, None, "github-actions",
                        "Actions", None, None, steps)

    def test_checks_report_their_settlement(self):
        cases = {
            "failed beside pending": ((self.run_("completed", "failure"),
                                       self.run_("in_progress")), (), ExecutionOutcome.FAILURE),
            "failed step of a running job": (
                (self.run_("in_progress", None, (("build", "success"), ("test", "failure"))),),
                (), ExecutionOutcome.FAILURE),
            "failed status beside pending run": (
                (self.run_("queued"),), (CommitStatus("ci", "failure", None, None),),
                ExecutionOutcome.FAILURE),
            "only pending": ((self.run_("queued"),), (), ExecutionOutcome.PENDING),
            "nothing registered yet": ((), (), ExecutionOutcome.PENDING),
            "all passing by GitHub's rule": (
                (self.run_("completed", "success"), self.run_("completed", "skipped")),
                (CommitStatus("ci", "success", None, None),), ExecutionOutcome.SUCCESS),
            "outside the vocabulary": ((self.run_("completed", "action_required"),), (),
                                       ExecutionOutcome.AMBIGUOUS),
        }
        for name, (runs, statuses, expected) in cases.items():
            with self.subTest(case=name):
                self.assertEqual(self.check_reader(*runs, statuses=statuses).outcome, expected)

    def test_transient_check_read_failure_is_temporarily_unavailable(self):
        from alx.contracts.pull_request_checks import CheckReadError

        def read(_request):
            raise CheckReadError("rate_limited")
        result = build_pull_request_checks_executors(read, lambda: "r")[
            "read_pull_request_checks"]({"pull_request_number": 95, "head_sha": HEAD})
        self.assertEqual(result.outcome, ExecutionOutcome.TEMPORARILY_UNAVAILABLE)

    def test_review_reports_pending_only_while_the_reviewer_is_working(self):
        def reader(content):
            return build_review_content_executors(lambda _request: content, lambda: "r")[
                "read_external_review"]({"pull_request_number": 95, "head_sha": HEAD})
        working = ReviewContent(95, HEAD, "reviewer", False,
                                unavailable_reason=REVIEW_IN_PROGRESS)
        failed = ReviewContent(95, HEAD, "reviewer", False, unavailable_reason=REVIEW_FAILED)
        published = ReviewContent(95, HEAD, "reviewer", True, summary="One finding.",
                                  submitted_at=NOW, retrieved_at=NOW)
        self.assertEqual(reader(working).outcome, ExecutionOutcome.PENDING)
        self.assertEqual(classify_planned_result(
            step("review", wait=10), self.attempt(reader(failed)), REVIEW_DEFINITION).kind,
            PlanResultKind.WAKE_CORE)
        self.assertEqual(reader(published).outcome, ExecutionOutcome.AMBIGUOUS)

    @staticmethod
    def attempt(result):
        call = CapabilityCall(result.call_id, result.capability_id, {})
        return CapabilityAttempt(call, CapabilityAttemptDisposition.EXECUTED, True, result)

    def test_failed_check_beside_a_pending_one_wakes_her_at_once(self):
        definitions = (*DEFINITIONS, CHECKS_DEFINITION)
        agent = self.installed(
            ExecutionStep(CapabilityCall("read", "read_pull_request_checks",
                                         {"pull_request_number": 95, "head_sha": HEAD}),
                          (), 60, 3600),
            agent=self.agent(Reasoner(install(plan(ExecutionStep(
                CapabilityCall("read", "read_pull_request_checks",
                               {"pull_request_number": 95, "head_sha": HEAD}),
                (), 60, 3600)))), definitions=definitions),
        )
        result = self.check_reader(self.run_("completed", "failure"), self.run_("queued"))
        self.outputs["read_pull_request_checks"] = lambda call: CapabilityAttempt(
            call, CapabilityAttemptDisposition.EXECUTED, True,
            replace(result, call_id=call.call_id))
        self.work(agent)
        self.assertEqual(self.plan_of().attention.reason, "planned_result_failed")


class ClassifierTests(unittest.TestCase):
    def attempt(self, state=CapabilityResultState.SUCCEEDED, result_outcome=None,
                disposition=CapabilityAttemptDisposition.EXECUTED, values=None, failure=None,
                capability="ci"):
        call = CapabilityCall("c", capability, {})
        if disposition is CapabilityAttemptDisposition.REJECTED:
            return CapabilityAttempt(call, disposition, False, reason_code="refused")
        return CapabilityAttempt(call, disposition, True, CapabilityResult(
            "c", capability, state, values or {}, failure, outcome=result_outcome))

    def kind(self, attempt, definition_name="ci", **step_options):
        definition = next(item for item in DEFINITIONS
                          if item.capability_id == definition_name)
        return classify_planned_result(step(definition_name, **step_options), attempt,
                                       definition).kind

    def test_precedence(self):
        WAKE, WAIT, ADVANCE = (PlanResultKind.WAKE_CORE, PlanResultKind.WAIT,
                               PlanResultKind.ADVANCE)
        cases = {
            "refusal": (self.attempt(disposition=CapabilityAttemptDisposition.REJECTED),
                        "ci", {"wait": 10}, WAKE),
            "failure": (self.attempt(CapabilityResultState.FAILED, failure={"code": "x"}),
                        "ci", {"wait": 10}, WAKE),
            "judgment": (self.attempt(result_outcome=ExecutionOutcome.AMBIGUOUS),
                         "ci", {"wait": 10}, WAKE),
            "partial": (self.attempt(CapabilityResultState.PARTIAL, values={"a": 1}),
                        "ci", {}, WAKE),
            "pending observation": (self.attempt(result_outcome=ExecutionOutcome.PENDING),
                                    "ci", {"wait": 10}, WAIT),
            "unavailable observation": (
                self.attempt(CapabilityResultState.FAILED, ExecutionOutcome.TEMPORARILY_UNAVAILABLE,
                             failure={"code": "rate_limited"}), "ci", {"wait": 10}, WAIT),
            "pending from a step that may not wait": (
                self.attempt(result_outcome=ExecutionOutcome.PENDING), "ci", {}, WAKE),
            "pending from a capability that is not an observation": (
                self.attempt(result_outcome=ExecutionOutcome.PENDING, capability="coding"),
                "coding", {}, WAKE),
            "success": (self.attempt(), "ci", {}, ADVANCE),
            "success failing its completion condition": (
                self.attempt(values={"merged": False}), "ci",
                {"completion": (PlanCondition("values.merged", True),)}, WAKE),
        }
        for name, (attempt, definition, options, expected) in cases.items():
            with self.subTest(case=name):
                self.assertEqual(self.kind(attempt, definition, **options), expected)

    def test_a_wait_past_its_bound_wakes_her(self):
        attempt = self.attempt(result_outcome=ExecutionOutcome.PENDING)
        definition = next(item for item in DEFINITIONS if item.capability_id == "ci")
        classified = classify_planned_result(step("ci", wait=10), attempt, definition,
                                             wait_expired=True)
        self.assertEqual(classified.facts, ("plan_wait_exceeded",))

    def test_interruption_repeats_only_a_waiting_observation(self):
        interrupted = CapabilityAttempt(
            CapabilityCall("c", "ci", {}), CapabilityAttemptDisposition.BROKER_FAILURE, True,
            CapabilityResult("c", "ci", CapabilityResultState.FAILED,
                             failure={"code": "dispatch_interrupted"}), "dispatch_interrupted")
        self.assertEqual(self.kind(interrupted, "ci", wait=10), PlanResultKind.WAIT)
        effectful = replace(interrupted, call=CapabilityCall("c", "coding", {}),
                            result=replace(interrupted.result, capability_id="coding"))
        self.assertEqual(self.kind(effectful, "coding"), PlanResultKind.WAKE_CORE)

    def test_success_with_failure_looking_values_is_not_guessed_to_be_failure(self):
        attempt = self.attempt(values={"status": "failed", "conclusion": "failure"})
        self.assertEqual(self.kind(attempt, "ci"), PlanResultKind.ADVANCE)


class RestartTests(PlanHarness):
    """Acceptance invariant 7: restart resumes mechanically where safe."""

    def test_interrupted_effectful_step_wakes_her_and_is_never_replayed(self):
        agent = self.installed(step("coding"), step("merge"))
        (job,) = agent.advance_due_plans()
        self.assertIsNotNone(agent.begin_planned_dispatch(job))
        self.restart()
        self.work(self.agent())
        self.assertEqual(self.plan_of().attention.reason, "dispatch_interrupted")
        self.assertEqual(self.calls, [])

    def test_a_checkpoint_never_started_is_simply_run_after_restart(self):
        agent = self.installed(step("coding"), step("merge"))
        agent.advance_due_plans()
        self.restart()
        self.work(self.agent())
        self.assertEqual(self.names(), ["coding", "merge"])
        self.assertEqual(self.plan_of().attention.reason, "plan_steps_done")
        dropped = [item for item in self.state().attempts
                   if item.reason_code == "dispatch_not_started"]
        self.assertEqual(len(dropped), 1)
        self.assertFalse(dropped[0].implementation_invoked)

    def test_interrupted_observation_is_observed_again_without_her(self):
        agent = self.installed(step("ci", wait=10))
        (job,) = agent.advance_due_plans()
        agent.begin_planned_dispatch(job)
        self.restart()
        agent = self.agent()
        self.work(agent)
        self.assertEqual(self.plan_of().status, PlanStatus.WAITING)
        self.later(10)
        self.work(agent)
        self.assertEqual(self.names(), ["ci"])
        self.assertEqual(self.plan_of().attention.reason, "plan_steps_done")

    def test_waiting_stays_waiting_and_needs_core_keeps_its_attention(self):
        agent = self.installed(step("ci", wait=10), step("coding"))
        self.outputs["ci"] = [PENDING, SUCCESS]
        self.outputs["coding"] = FAILED
        self.work(agent)
        waiting = self.plan_of()
        self.restart()
        self.work(self.agent())
        self.assertEqual(self.plan_of(), waiting)
        self.later(10)
        self.work(self.agent())
        needing = self.plan_of()
        self.restart()
        self.work(self.agent())
        self.assertEqual(self.plan_of(), needing)
        self.assertEqual(needing.attention.reason, "planned_result_failed")

    def test_recorded_but_unreduced_result_is_reduced_not_redispatched(self):
        agent = self.installed(step("coding"), step("merge"))
        (job,) = agent.advance_due_plans()
        job = agent.begin_planned_dispatch(job)
        attempt = agent.run_planned_dispatch(job)
        # The process stops after the result is recorded, before the plan moves.
        snapshot = self.store.load("goal")
        self.store.replace(replace(snapshot.state, attempts=(*snapshot.state.attempts[:-1],
                                                             attempt)),
                           snapshot.retention_until, snapshot.revision)
        self.restart()
        self.work(self.agent())
        self.assertEqual(self.names(), ["coding", "merge"])
        self.assertEqual(self.plan_of().attention.reason, "plan_steps_done")

    def test_judgment_evidence_lost_on_restart_is_observed_again_for_her(self):
        agent = self.installed(step("review", wait=10))
        self.outputs["review"] = [JUDGE, JUDGE]
        self.work(agent)
        self.restart()
        reasoner = Reasoner(AgentDecision(response="Reading it.", goal_id="goal"))
        agent = self.agent(reasoner)
        # Not offered over metadata alone: the read must come first.
        self.assertFalse(agent.plan_evidence_ready("goal"))
        # Read on a worker, by the runner, never inside a Core turn.
        self.work(agent)
        self.assertTrue(agent.plan_evidence_ready("goal"))
        self.assertEqual(self.names(), ["review", "review"])
        self.occasion(agent)
        self.assertEqual(self.names(), ["review", "review"])
        evidence = reasoner.contexts[0].transient_attempts
        self.assertEqual(evidence[0].result.values["findings"], ("fix",))
        # Evidence for her, not a plan result: the plan has not moved.
        self.assertEqual(self.plan_of().attention.reason, "planned_evidence_requires_judgement")


class ReplacedPlanRestartTests(PlanHarness):
    def test_a_stopped_step_of_a_replaced_plan_never_blocks_its_replacement(self):
        agent = self.installed(step("coding"))
        agent.advance_due_plans()
        self.person(self.same_process(
            agent, Reasoner(SELECT, install(plan(step("cleanup")), "Replaced."))))
        self.restart()
        self.work(self.agent())
        self.assertEqual(self.names(), ["cleanup"])
        stopped = next(item for item in self.state().attempts
                       if item.call.capability_id == "coding")
        self.assertEqual(stopped.reason_code, "dispatch_interrupted")


class RestartEvidenceTests(PlanHarness):
    def test_effectful_evidence_after_restart_is_its_durable_record_never_a_rerun(self):
        agent = self.installed(step("coding"))
        self.outputs["coding"] = FAILED
        self.work(agent)
        self.restart()
        reasoner = Reasoner(AgentDecision(response="It failed.", goal_id="goal"))
        self.occasion(self.agent(reasoner))
        (evidence,) = reasoner.contexts[0].transient_attempts
        self.assertEqual(evidence.result.failure, {"code": "failed"})
        self.assertEqual(self.names(), ["coding"])


class AttentionTests(PlanHarness):
    """Acceptance invariants 8 and 9: exhaustion blocks and surfaces; refusals cost nothing."""

    def needing_core(self):
        agent = self.installed(step("coding"))
        self.outputs["coding"] = FAILED
        self.work(agent)
        return agent

    def source(self, notices=None, spend=None):
        return PlanAttentionSource(
            self.store, Ledger(), enabled=True, clock=lambda: self.now, spend=spend,
            notify=(lambda conversation_id, values: notices.append((conversation_id, values)))
            if notices is not None else None,
        )

    def test_exhausted_offers_block_and_notify_once_without_reasoning(self):
        self.needing_core()
        notices = []
        spend = Spend()
        source = self.source(notices, spend)
        for _ in range(MAX_PAID_PLAN_OFFERS):
            (offer,) = source.due_opportunities()
            self.assertTrue(source.claim(offer))
            spend.reached.add(offer.opportunity_id)   # the provider was reached
            source.mark_honoured(offer)
            self.later(PLAN_BACKOFF_CAP)
            source.settle()
        attention = self.plan_of().attention
        self.assertTrue(attention.blocked)
        self.assertEqual(attention.paid_offers, MAX_PAID_PLAN_OFFERS)
        self.assertEqual(source.due_opportunities(), ())
        source.settle()
        self.assertEqual([values["state"] for _thread, values in notices], ["blocked"])
        self.assertEqual(notices[0][0], "thread")
        # A restart cannot extend the cap, and shows the block again.
        self.restart()
        restarted = []
        self.source(restarted, spend).settle()
        self.assertEqual([values["state"] for _thread, values in restarted], ["blocked"])
        self.assertEqual(self.source().due_opportunities(), ())

    def test_blocked_attention_stays_visible_to_every_core_turn_until_resolved(self):
        self.needing_core()
        self.set_goal(execution_plan=replace(self.plan_of(), attention=replace(
            self.plan_of().attention, offers=3, paid_offers=3, blocked=True)))
        for index in range(12):
            self.store.create(new_goal(f"newer-{index}"), "other-thread", RETENTION)
        reasoner = Reasoner(SELECT, resolve(PlanOperation.RESUME, "Trying again."))
        self.person(self.agent(reasoner))
        summary = next(item for item in reasoner.contexts[0].unfinished_goals
                       if item.goal_id == "goal")
        self.assertTrue(summary.plan_attention_blocked)
        self.assertEqual(self.plan_of().status, PlanStatus.RUNNING)
        notices = []
        source = self.source(notices)
        source._notified[("goal", self.plan_of().plan_id, 1)] = "thread"
        source.settle()
        self.assertEqual(notices[0][1]["state"], "resolved")

    def test_an_offer_that_never_reached_a_provider_costs_nothing_and_backs_off(self):
        self.needing_core()
        source = self.source()
        (offer,) = source.due_opportunities()
        self.assertTrue(source.claim(offer))
        source.release(offer)
        attention = self.plan_of().attention
        self.assertEqual((attention.offers, attention.paid_offers), (1, 0))
        self.assertEqual(source.due_opportunities(), ())
        self.later(PLAN_BACKOFF_CAP)
        self.assertEqual(len(source.due_opportunities()), 1)

    def test_a_stale_offer_cannot_be_claimed(self):
        self.needing_core()
        source = self.source()
        (offer,) = source.due_opportunities()
        self.assertTrue(source.claim(offer))
        self.assertFalse(source.claim(offer))

    def test_an_attention_on_a_goal_waiting_for_friedl_is_not_offered(self):
        self.needing_core()
        self.set_goal(status=GoalStatus.AWAITING_INPUT, stop_reason=GoalStopReason.REQUIRED_INPUT,
                      outstanding_work=(WorkItem("answer", "answer"),))
        self.assertEqual(self.source().due_opportunities(), ())

    def test_an_offer_turn_for_a_resolved_attention_reasons_not_at_all(self):
        self.needing_core()
        self.set_goal(execution_plan=replace(self.plan_of(), status=PlanStatus.CANCELLED,
                                             attention=None))
        reasoner = Reasoner()
        self.assertEqual(self.occasion(self.agent(reasoner)).reason, "plan_attention_resolved")
        self.assertEqual(reasoner.calls, 0)


# The longest backoff an offer can carry: five minutes doubled four times.
PLAN_BACKOFF_CAP = 300 * 16 + 1


class Spend:
    """The spend ledger's durable record of which occasions reached a provider."""

    def __init__(self):
        self.reached = set()

    def dispatch_started(self, opportunity_id):
        return opportunity_id in self.reached


class Ledger:
    """The occasion ledger's audit row, recorded and nothing more."""

    def __init__(self):
        self.rows = set()

    def record_created(self, opportunity):
        self.rows.add(opportunity.opportunity_id)
        return True

    def release(self, opportunity_id):
        self.rows.discard(opportunity_id)


class AvailabilityTests(unittest.IsolatedAsyncioTestCase, PlanHarness):
    """Acceptance invariant 10: a long step never holds the Core or other plans."""

    def setUp(self):
        PlanHarness.setUp(self)

    async def test_a_long_step_runs_off_the_lock_while_other_plans_advance(self):
        self.store.create(new_goal("second"), "thread", RETENTION)
        release = threading.Event()
        started = threading.Event()

        def long_coding(call):
            started.set()
            release.wait(10)
            return CapabilityAttempt(call, CapabilityAttemptDisposition.EXECUTED, True,
                                     CapabilityResult(call.call_id, call.capability_id,
                                                      CapabilityResultState.SUCCEEDED))
        self.outputs["coding"] = long_coding
        agent = self.installed(step("coding"))
        snapshot = self.store.load("second")
        self.store.replace(replace(snapshot.state, execution_plan=plan(
            step("cleanup"), plan_id="second-plan", objective_source="turn:person-1",
            objective_summary="Do the work")), snapshot.retention_until, snapshot.revision)
        lock = asyncio.Lock()
        workers = PlanWorkers(agent, lock)
        await workers.advance()
        await asyncio.to_thread(started.wait, 5)
        # The Core-turn lock is free while the coding step runs.
        await asyncio.wait_for(lock.acquire(), 1)
        lock.release()
        for _ in range(200):
            if self.plan_of("second").status is PlanStatus.NEEDS_CORE:
                break
            await asyncio.sleep(0.01)
        self.assertEqual(self.plan_of("second").attention.reason, "plan_steps_done")
        self.assertEqual(self.plan_of().status, PlanStatus.RUNNING)
        release.set()
        await workers.drain()
        self.assertEqual(self.plan_of().attention.reason, "plan_steps_done")


class ForegroundCodingTests(unittest.IsolatedAsyncioTestCase, PlanHarness):
    """A person turn never waits behind coding: it runs only as a plan step.

    Production, 2026-10-03: Core dispatched run_coding_task directly inside a
    person turn, held the Core-turn lock for the whole job, and an unrelated
    question waited minutes behind it.
    """

    def setUp(self):
        PlanHarness.setUp(self)

    async def test_direct_coding_is_refused_and_planned_coding_leaves_turns_free(self):
        coding = CapabilityDefinition("run_coding_task", "code", SCHEMA, SCHEMA,
                                      SideEffect.EFFECTFUL)
        direct = AgentDecision(goal_id="goal", call=CapabilityCall(
            "call-direct", "run_coding_task", {}))
        chat = AgentDecision(response="Quiet so far.")
        reasoner = Reasoner(direct, install(plan(step("run_coding_task"), step("merge"))),
                            chat)
        agent = self.agent(reasoner, definitions=(*DEFINITIONS, coding))
        lock = asyncio.Lock()
        workers = PlanWorkers(agent, lock)
        started, release = threading.Event(), threading.Event()

        def blocked_coding(call):
            started.set()
            release.wait(10)
            return CapabilityAttempt(call, CapabilityAttemptDisposition.EXECUTED, True,
                                     CapabilityResult(call.call_id, call.capability_id,
                                                      CapabilityResultState.SUCCEEDED))
        self.outputs["run_coding_task"] = blocked_coding
        conversations = SQLiteConversationStore(Path(self.path).with_name("turns.sqlite3"))
        self.addCleanup(conversations.close)
        gateway = ConversationGateway(agent, conversations, clock=lambda: self.now)

        async def person_turn(turn_id):
            # As the live session's run_turn: one Core turn under the shared lock.
            turn = ConversationTurn("thread", turn_id, ConversationOrigin.TYPED,
                                    "Words", self.now, "friedl")
            async with lock:
                return await asyncio.to_thread(
                    gateway.receive_conversation_turn, turn, 4, RETENTION)

        first = await person_turn("person-1")
        self.assertEqual(first.state, CoreState.RESPONDED, first.reason)
        # Refused before dispatch, returned to her once, and corrected by her.
        self.assertEqual(self.calls, [])
        self.assertEqual(reasoner.contexts[1].refused_calls, ({
            "call_id": "call-direct", "capability_id": "run_coding_task",
            "reason": "coding_requires_execution_plan", "subject": "run_coding_task",
        },))
        self.assertEqual(self.plan_of().status, PlanStatus.RUNNING)

        await workers.advance()
        self.assertTrue(await asyncio.to_thread(started.wait, 5))
        self.assertFalse(lock.locked())
        before = self.store.load("goal")
        second = await asyncio.wait_for(person_turn("person-2"), 5)
        # Answered while coding is still held: the step never owned the lock.
        self.assertFalse(release.is_set())
        self.assertEqual((second.state, second.response),
                         (CoreState.RESPONDED, "Quiet so far."))
        self.assertEqual(self.store.load("goal"), before)

        release.set()
        await workers.drain()
        self.assertEqual(self.names(), ["run_coding_task", "merge"])
        self.assertNotIn("call-direct", [call.call_id for call in self.calls])
        recorded = [item for item in self.state().attempts
                    if item.call.capability_id == "run_coding_task"]
        self.assertEqual([(item.call.call_id, item.disposition) for item in recorded],
                         [(self.calls[0].call_id, CapabilityAttemptDisposition.EXECUTED)])
        self.assertEqual(self.plan_of().attention.reason, "plan_steps_done")
        # One reasoning call per new piece of evidence, none while it ran.
        self.assertEqual(reasoner.calls, 3)


class DueTickTests(unittest.IsolatedAsyncioTestCase):
    async def test_the_tick_advances_plans_before_offering_occasions(self):
        from alx.continuity.due_source import DueCognitionSource

        order = []

        class Source:
            def due_opportunities(self):
                order.append("offer")
                return ()

        async def advance():
            order.append("plans")
        tick = DueCognitionSource(Source(), object(), asyncio.Lock(), 30,
                                  advance_plans=advance)
        await tick.tick()
        self.assertEqual(order, ["plans", "offer"])


class AcceptSafetyTests(PlanHarness):
    """`accept` moves past a step only when it happened and only judgment remains."""

    def accept_after(self, *steps, outputs, setup=None):
        reasoner = Reasoner(install(plan(*steps)), resolve(PlanOperation.ACCEPT),
                            AgentDecision(response="Understood.", goal_id="goal"))
        agent = self.agent(reasoner)
        self.outputs.update(outputs)
        self.person(agent)
        if setup is None:
            self.work(agent)
        else:
            agent = setup(agent)
        before = self.plan_of()
        self.occasion(agent)
        return reasoner, before

    def assert_refused(self, reasoner, before, reason):
        self.assertEqual(reasoner.contexts[2].refused_calls[0]["reason"],
                         "plan_step_not_acceptable")
        after = self.plan_of()
        self.assertEqual((after.status, after.cursor, after.attention.reason),
                         (PlanStatus.NEEDS_CORE, before.cursor, reason))

    def test_judged_external_review_accepts_and_runs_on(self):
        reasoner, _before = self.accept_after(
            step("review", wait=10), step("merge"), outputs={"review": JUDGE})
        self.work(self.agent())
        self.assertEqual(self.names(), ["review", "merge"])

    def test_a_failed_review_cannot_be_accepted(self):
        failed_review = outcome(CapabilityResultState.FAILED, failure={
            "code": "review_unavailable", "reason": REVIEW_FAILED,
            "requires_judgement": True})
        reasoner, before = self.accept_after(
            step("review", wait=10), step("merge"), outputs={"review": failed_review})
        self.assert_refused(reasoner, before, "planned_result_failed")

    def test_a_refused_step_cannot_be_accepted(self):
        def refused(call):
            return CapabilityAttempt(call, CapabilityAttemptDisposition.REJECTED, False,
                                     reason_code="authority_refused")
        reasoner, before = self.accept_after(
            step("review", wait=10), step("merge"), outputs={"review": refused})
        self.assert_refused(reasoner, before, "planned_call_refused")

    def test_a_merge_that_did_not_happen_cannot_be_accepted_into_cleanup(self):
        for name, result in (
            ("refused for judgment", outcome(CapabilityResultState.FAILED, failure={
                "code": "merge_unavailable", "requires_judgement": True})),
            ("succeeded but ambiguous", JUDGE),
        ):
            with self.subTest(case=name):
                self.set_goal(execution_plan=None, attempts=())
                self.calls.clear()
                reasoner, before = self.accept_after(
                    step("merge"), step("cleanup"), outputs={"merge": result})
                self.assertEqual(reasoner.contexts[2].refused_calls[0]["reason"],
                                 "plan_step_not_acceptable")
                self.work(self.agent())
                self.assertNotIn("cleanup", self.names())

    def test_an_interrupted_effectful_step_cannot_be_accepted(self):
        def crash(agent):
            (job,) = agent.advance_due_plans()
            agent.begin_planned_dispatch(job)
            self.restart()
            fresh = self.agent(Reasoner(resolve(PlanOperation.ACCEPT),
                                        AgentDecision(response="Checking.", goal_id="goal")))
            self.work(fresh)
            return fresh
        reasoner = None
        self.installed(step("coding"), step("merge"))
        before_agent = crash(self.agent())
        before = self.plan_of()
        self.occasion(before_agent)
        refused = before_agent._reasoner.contexts[1].refused_calls[0]["reason"]
        self.assertEqual(refused, "plan_step_not_acceptable")
        self.assertEqual(self.plan_of().attention.reason, "dispatch_interrupted")
        self.assertEqual(self.plan_of().cursor, before.cursor)

    def test_a_checkpoint_cannot_be_accepted_past_the_next_unrun_step(self):
        reasoner, before = self.accept_after(
            step("ci", wake=True), step("merge"), step("cleanup"), outputs={})
        self.assert_refused(reasoner, before, "plan_checkpoint")
        self.assertEqual(self.names(), ["ci"])

    def test_a_stale_attention_cannot_be_accepted(self):
        agent = self.installed(step("review", wait=10), step("merge"))
        self.outputs["review"] = JUDGE
        self.work(agent)
        current = self.plan_of()
        snapshot = self.store.load("goal")
        refusal = agent._plan_update_refusal(
            PlanUpdate(PlanOperation.ACCEPT), snapshot,
            (current.plan_id, current.attention_seq - 1), None, None)
        self.assertEqual(refusal, "plan_attention_not_current")
        refusal = agent._plan_update_refusal(
            PlanUpdate(PlanOperation.ACCEPT), snapshot, ("another-plan", 1), None, None)
        self.assertEqual(refusal, "plan_not_current")


class ContextIsolationTests(PlanHarness):
    """A running planned step keeps its own context whatever a Core turn binds."""

    def test_an_unrelated_turn_cannot_change_a_running_steps_context(self):
        import contextvars

        conversation_var = contextvars.ContextVar("conversation", default="")
        in_step = threading.Event()
        turn_done = threading.Event()
        seen = {}

        def planned(call):
            seen["before"] = conversation_var.get()
            in_step.set()
            turn_done.wait(5)
            seen["after"] = conversation_var.get()
            return CapabilityAttempt(call, CapabilityAttemptDisposition.EXECUTED, True,
                                     CapabilityResult(call.call_id, call.capability_id,
                                                      CapabilityResultState.SUCCEEDED))
        self.outputs["coding"] = planned
        agent = self.agent(Reasoner(install(plan(step("coding")))),
                           bind_dispatch=conversation_var.set,
                           budget_check=conversation_var.set)
        self.person(agent)
        (job,) = agent.advance_due_plans()
        job = agent.begin_planned_dispatch(job)
        self.assertEqual(job.conversation_id, "thread")
        context = contextvars.copy_context()
        worker = threading.Thread(target=lambda: context.run(
            lambda: seen.setdefault("attempt", agent.run_planned_dispatch(job))))
        worker.start()
        self.assertTrue(in_step.wait(5))
        # An unrelated Core turn binds its own conversation meanwhile.
        other = ConversationSnapshot("other-thread", (ConversationTurn(
            "other-thread", "other-1", ConversationOrigin.TYPED, "Hello", NOW, "friedl"),),
            1, RETENTION)
        self.same_process(agent, Reasoner(AgentDecision(response="Hello."))).process(
            other, RETENTION, 2)
        turn_done.set()
        worker.join(5)
        self.assertEqual((seen["before"], seen["after"]), ("thread", "thread"))
        agent.finish_planned_dispatch(job, seen["attempt"])
        self.assertEqual(self.plan_of().attention.reason, "plan_steps_done")

    def test_a_planned_bill_step_never_takes_the_running_occasions_budget(self):
        import ast as syntax

        source = (SRC / "bootstrap" / "live_voice.py").read_text()
        tree = syntax.parse(source)
        reads = [node for node in syntax.walk(tree)
                 if isinstance(node, syntax.Call)
                 and isinstance(node.func, syntax.Attribute)
                 and node.func.attr == "current_opportunity_id"
                 and getattr(node.func.value, "id", None) == "occasion_spend"]
        guarded = [node for node in syntax.walk(tree)
                   if isinstance(node, syntax.IfExp)
                   and "planned_dispatch.get()" in syntax.unparse(node.test)]
        self.assertEqual(len(reads), 1)
        self.assertTrue(any(reads[0] in tuple(syntax.walk(item.orelse)) for item in guarded))
        bind = next(node for node in syntax.walk(tree)
                    if isinstance(node, syntax.FunctionDef)
                    and node.name == "bind_planned_dispatch")
        self.assertIn("planned_dispatch.set(True)", syntax.unparse(bind))
        self.assertIn("current_conversation_id.set(conversation_id)", syntax.unparse(bind))
        # The executing call and conversation are per-context, never a
        # process-wide holder another turn could overwrite mid-step.
        for name in ("current_conversation_id", "current_call_id", "current_goal_state"):
            self.assertIn(f"{name}: ContextVar", source)
            self.assertNotIn(f"{name}[0]", source)


class StoreSerializationTests(PlanHarness):
    def test_a_read_never_sees_another_threads_uncommitted_write(self):
        entered = threading.Event()
        release = threading.Event()
        original = self.store._replace_rows

        def slow_replace(*arguments, **keywords):
            result = original(*arguments, **keywords)
            entered.set()
            release.wait(5)
            return result
        self.store._replace_rows = slow_replace
        snapshot = self.store.load("goal")
        writer = threading.Thread(target=lambda: self.store.replace(
            replace(snapshot.state, context={"written": True}),
            snapshot.retention_until, snapshot.revision))
        writer.start()
        self.assertTrue(entered.wait(5))
        read = {}
        reader = threading.Thread(target=lambda: read.setdefault("state", self.state()))
        reader.start()
        reader.join(0.3)
        # Still blocked while the write is in progress, not reading it raw.
        self.assertTrue(reader.is_alive())
        release.set()
        writer.join(5)
        reader.join(5)
        self.assertEqual(dict(read["state"].context), {"written": True})

    def test_every_shared_connection_store_serializes_its_operations(self):
        from alx.continuity.ledger import SQLiteOpportunityLedger
        from alx.continuity.store import SQLiteContinuityStore
        from alx.conversation import SQLiteConversationStore
        from alx.memories.store import SQLiteMemoryStore
        from alx.projects.store import SQLiteProjectStore
        from alx.providers.icloud_mail import SQLiteMailObservationState
        from alx.research.store import SQLiteResearchStore

        for store in (SQLiteGoalStore, SQLiteOpportunityLedger, SQLiteContinuityStore,
                      SQLiteConversationStore, SQLiteMemoryStore, SQLiteProjectStore,
                      SQLiteMailObservationState, SQLiteResearchStore):
            with self.subTest(store=store.__name__):
                public = [value for name, value in vars(store).items()
                          if not name.startswith("_") and callable(value)
                          and not isinstance(value, (staticmethod, classmethod))]
                self.assertTrue(public)
                self.assertTrue(all(hasattr(item, "__wrapped__") for item in public))


class WorkerLifecycleTests(unittest.IsolatedAsyncioTestCase, PlanHarness):
    def setUp(self):
        PlanHarness.setUp(self)

    def second_plan(self, *steps):
        self.store.create(new_goal("second"), "thread", RETENTION)
        snapshot = self.store.load("second")
        self.store.replace(replace(snapshot.state, execution_plan=plan(
            *steps, plan_id="second-plan", objective_source="turn:person-1",
            objective_summary="Do the work")), snapshot.retention_until, snapshot.revision)

    async def test_a_worker_that_raises_harms_no_other_and_strands_nothing(self):
        agent = self.installed(step("coding"))
        self.second_plan(step("cleanup"))
        original = agent.run_planned_dispatch

        def raising(job):
            if job.goal_id == "goal":
                raise RuntimeError("binding failed")
            return original(job)
        agent.run_planned_dispatch = raising
        workers = PlanWorkers(agent, asyncio.Lock())
        await workers.advance()
        await workers.drain()
        self.assertEqual(self.plan_of().attention.reason, "dispatch_interrupted")
        self.assertEqual(self.plan_of("second").attention.reason, "plan_steps_done")
        self.assertEqual(workers._tasks, set())

    async def test_a_failure_recording_the_result_is_recovered_as_interrupted(self):
        agent = self.installed(step("coding"))
        original = agent.finish_planned_dispatch

        def failing(job, attempt, **_options):
            agent._live_plan_dispatches.discard(job.call.call_id)
            raise RuntimeError("store unavailable")
        agent.finish_planned_dispatch = failing
        workers = PlanWorkers(agent, asyncio.Lock())
        await workers.advance()
        await workers.drain()
        agent.finish_planned_dispatch = original
        self.work(agent)
        self.assertEqual(self.plan_of().attention.reason, "dispatch_interrupted")
        self.assertEqual(self.names(), ["coding"])

    async def test_shutdown_while_a_step_runs_is_recovered_without_replay(self):
        release = threading.Event()
        started = threading.Event()

        def long_step(call):
            started.set()
            release.wait(5)
            return CapabilityAttempt(call, CapabilityAttemptDisposition.EXECUTED, True,
                                     CapabilityResult(call.call_id, call.capability_id,
                                                      CapabilityResultState.SUCCEEDED))
        self.outputs["coding"] = long_step
        agent = self.installed(step("coding"))
        workers = PlanWorkers(agent, asyncio.Lock())
        await workers.advance()
        self.assertTrue(await asyncio.to_thread(started.wait, 5))
        for task in tuple(workers._tasks):
            task.cancel()
        release.set()
        await asyncio.gather(*tuple(workers._tasks), return_exceptions=True)
        self.restart()
        self.work(self.agent())
        self.assertEqual(self.plan_of().attention.reason, "dispatch_interrupted")
        self.assertEqual(self.names(), ["coding"])


class ShutdownTests(unittest.IsolatedAsyncioTestCase, PlanHarness):
    """Stores close only after every started step has recorded its result."""

    def setUp(self):
        PlanHarness.setUp(self)

    def long(self, name, release, started):
        def run(call):
            started.set()
            release.wait(5)
            return CapabilityAttempt(call, CapabilityAttemptDisposition.EXECUTED, True,
                                     CapabilityResult(call.call_id, call.capability_id,
                                                      CapabilityResultState.SUCCEEDED))
        self.outputs[name] = run

    async def stop_during(self, name, *steps, cancel=None):
        release, started = threading.Event(), threading.Event()
        self.long(name, release, started)
        agent = self.installed(*steps)
        cancelled = []

        def cancel_dispatch(job):
            cancelled.append(job.call.capability_id)
            release.set()
        workers = PlanWorkers(agent, asyncio.Lock(), cancel_dispatch=cancel or cancel_dispatch)
        await workers.advance()
        self.assertTrue(await asyncio.to_thread(started.wait, 5))
        stopping = asyncio.ensure_future(workers.stop())
        await asyncio.sleep(0.05)
        return agent, workers, stopping, release, cancelled

    async def test_shutdown_while_coding_runs_waits_for_its_recorded_result(self):
        _agent, workers, stopping, _release, cancelled = await self.stop_during(
            "coding", step("coding"), step("merge"))
        await stopping
        # The coding job was asked to stop; its result was recorded first.
        self.assertEqual(cancelled, ["coding"])
        self.assertEqual(workers._tasks, set())
        recorded = [item for item in self.state().attempts if item.call.capability_id == "coding"]
        self.assertEqual(recorded[0].disposition, CapabilityAttemptDisposition.EXECUTED)
        # Nothing further started while stopping, and a restart does not
        # treat the recorded completion as an interruption.
        self.assertEqual(self.names(), ["coding"])
        self.restart()
        self.work(self.agent())
        self.assertEqual(self.names(), ["coding", "merge"])
        self.assertEqual(self.plan_of().attention.reason, "plan_steps_done")

    async def test_shutdown_while_an_observation_runs_cannot_close_the_store_under_it(self):
        _agent, workers, stopping, release, _cancelled = await self.stop_during(
            "ci", step("ci", wait=10), cancel=lambda _job: None)
        # The step has no cancel of its own: shutdown waits for it.
        await asyncio.sleep(0.1)
        self.assertFalse(stopping.done())
        release.set()
        await stopping
        self.assertEqual(self.plan_of().attention.reason, "plan_steps_done")

    async def test_a_step_not_yet_started_at_shutdown_never_starts(self):
        agent = self.installed(step("coding"))
        lock = asyncio.Lock()
        workers = PlanWorkers(agent, lock)
        await workers.advance()
        # A Core turn takes the lock before the worker reaches its boundary,
        # and shutdown begins meanwhile.
        await lock.acquire()
        stopping = asyncio.ensure_future(workers.stop())
        await asyncio.sleep(0.05)
        lock.release()
        await stopping
        self.assertEqual(self.calls, [])
        # Its checkpoint was never started, so restart simply runs it.
        self.assertFalse(self.plan_of().inflight.started)
        self.restart()
        self.work(self.agent())
        self.assertEqual(self.names(), ["coding"])

    async def test_a_runtime_killed_mid_step_restarts_into_interruption(self):
        release, started = threading.Event(), threading.Event()
        self.long("coding", release, started)
        agent = self.installed(step("coding"))
        workers = PlanWorkers(agent, asyncio.Lock())
        await workers.advance()
        self.assertTrue(await asyncio.to_thread(started.wait, 5))
        # No stop: the process simply dies with the call in flight.
        for task in tuple(workers._tasks):
            task.cancel()
        release.set()
        await asyncio.gather(*tuple(workers._tasks), return_exceptions=True)
        self.restart()
        self.work(self.agent())
        self.assertEqual(self.plan_of().attention.reason, "dispatch_interrupted")
        self.assertEqual(self.names(), ["coding"])


class PaidOfferTests(PlanHarness):
    """Only an offer that reached a provider is paid."""

    def needing_core(self):
        agent = self.installed(step("coding"))
        self.outputs["coding"] = FAILED
        self.work(agent)

    def test_refusals_before_the_provider_never_block_but_still_back_off(self):
        self.needing_core()
        spend = Spend()
        source = PlanAttentionSource(self.store, Ledger(), True, clock=lambda: self.now,
                                     spend=spend)
        for _ in range(MAX_PAID_PLAN_OFFERS + 2):
            (offer,) = source.due_opportunities()
            source.claim(offer)
            # A budget stop or disabled reasoner: the turn checkpointed, the
            # runner honoured it, and the spend ledger shows no dispatch.
            source.mark_honoured(offer)
            source.settle()
            self.assertEqual(source.due_opportunities(), ())   # backed off
            self.later(PLAN_BACKOFF_CAP)
        attention = self.plan_of().attention
        self.assertEqual((attention.paid_offers, attention.blocked), (0, False))
        self.assertEqual(attention.offers, MAX_PAID_PLAN_OFFERS + 2)

    def test_the_paid_count_is_read_from_the_ledger_after_restart(self):
        self.needing_core()
        spend = Spend()
        source = PlanAttentionSource(self.store, Ledger(), True, clock=lambda: self.now,
                                     spend=spend)
        (offer,) = source.due_opportunities()
        source.claim(offer)
        spend.reached.add(offer.opportunity_id)
        self.restart()
        PlanAttentionSource(self.store, Ledger(), True, clock=lambda: self.now,
                            spend=spend).settle()
        self.assertEqual(self.plan_of().attention.paid_offers, 1)


class CandidateFairnessTests(PlanHarness):
    def test_plans_needing_her_cannot_crowd_out_this_conversations_goals(self):
        attention = PlanAttention(1, "planned_result_failed", ("planned_result_failed",), NOW)
        for index in range(12):
            goal_id = f"attention-{index}"
            self.store.create(new_goal(goal_id), "elsewhere", RETENTION)
            snapshot = self.store.load(goal_id)
            self.store.replace(replace(snapshot.state, execution_plan=plan(
                step("coding"), plan_id=goal_id, objective_source="turn:person-1",
                objective_summary="Do the work", status=PlanStatus.NEEDS_CORE,
                attention_seq=1, attention=attention)),
                snapshot.retention_until, snapshot.revision)
        for index in range(3):
            self.store.create(new_goal(f"mine-{index}"), "thread", RETENTION)
        reasoner = Reasoner(AgentDecision(response="Hello."))
        self.person(self.agent(reasoner))
        listed = [item.goal_id for item in reasoner.contexts[0].unfinished_goals]
        for goal_id in ("goal", "mine-0", "mine-1", "mine-2"):
            self.assertIn(goal_id, listed)
        needing = [goal_id for goal_id in listed if goal_id.startswith("attention-")]
        self.assertTrue(needing)
        self.assertLessEqual(len(needing), 10 + 5)
        self.assertLessEqual(len(listed), 10 + 5)


class CheckPrecedenceTests(unittest.TestCase):
    def outcome(self, *runs, statuses=()):
        def read(_request):
            return PullRequestChecks(95, HEAD, tuple(runs), tuple(statuses))
        return build_pull_request_checks_executors(read, lambda: "r")[
            "read_pull_request_checks"]({"pull_request_number": 95, "head_sha": HEAD}).outcome

    @staticmethod
    def run_(status, conclusion=None):
        return CheckRun("check", status, conclusion, None, None, None, "github-actions",
                        "Actions", None, None, None)

    def test_failure_then_judgment_then_pending_then_success(self):
        queued = self.run_("queued")
        cases = {
            "action_required beside queued": ((self.run_("completed", "action_required"), queued),
                                              (), ExecutionOutcome.AMBIGUOUS),
            "stale beside queued": ((self.run_("completed", "stale"), queued), (),
                                    ExecutionOutcome.AMBIGUOUS),
            "failure beside action_required": ((self.run_("completed", "failure"),
                                                self.run_("completed", "action_required")),
                                               (), ExecutionOutcome.FAILURE),
            "unknown status state beside pending": (
                (), (CommitStatus("a", "expected", None, None),
                     CommitStatus("b", "pending", None, None)), ExecutionOutcome.AMBIGUOUS),
            "only unresolved": ((queued,), (CommitStatus("a", "pending", None, None),),
                                ExecutionOutcome.PENDING),
            "all passing": ((self.run_("completed", "neutral"),),
                            (CommitStatus("a", "success", None, None),), ExecutionOutcome.SUCCESS),
        }
        for name, (runs, statuses, expected) in cases.items():
            with self.subTest(case=name):
                self.assertEqual(self.outcome(*runs, statuses=statuses), expected)


class WaitDeadlineTests(PlanHarness):
    def test_polls_never_pass_the_deadline_and_the_deadline_wakes_her(self):
        agent = self.installed(ExecutionStep(CapabilityCall("ci", "ci", {}), (), 15, 24))
        self.outputs["ci"] = [PENDING, PENDING]
        self.work(agent)
        self.assertEqual(self.plan_of().next_due_at, NOW + timedelta(seconds=15))
        self.later(15)
        self.work(agent)
        # 30s would pass the 24s bound: the next poll is the bound itself.
        self.assertEqual(self.plan_of().next_due_at, NOW + timedelta(seconds=24))
        self.later(9)
        self.work(agent)
        self.assertEqual(self.plan_of().attention.reason, "plan_wait_exceeded")
        # No observation was made at or after the deadline.
        self.assertEqual(self.names(), ["ci", "ci"])

    def test_a_success_recorded_after_the_deadline_does_not_advance(self):
        agent = self.installed(ExecutionStep(CapabilityCall("ci", "ci", {}), (), 10, 20),
                               step("merge"))
        self.outputs["ci"] = [PENDING, SUCCESS]
        self.work(agent)
        self.later(10)
        (job,) = agent.advance_due_plans()
        job = agent.begin_planned_dispatch(job)
        attempt = agent.run_planned_dispatch(job)
        # The observation returns exactly at the deadline: that is too late.
        self.later(10)
        agent.finish_planned_dispatch(job, attempt)
        self.assertEqual(self.plan_of().attention.reason, "plan_wait_exceeded")
        self.assertNotIn("merge", self.names())


class EvidenceConcurrencyTests(PlanHarness):
    def needing_judgment_after_restart(self):
        agent = self.installed(step("review", wait=10))
        self.outputs["review"] = [JUDGE, JUDGE]
        self.work(agent)
        self.restart()

    def test_a_person_turn_never_waits_on_an_evidence_read(self):
        self.needing_judgment_after_restart()
        reasoner = Reasoner(SELECT, AgentDecision(response="Still reading.", goal_id="goal"))
        agent = self.agent(reasoner)
        (job,) = agent.advance_due_plans()            # scheduled, not yet run
        self.person(self.same_process(agent, reasoner))
        self.assertEqual(self.names(), ["review"])    # the turn read nothing
        job = agent.begin_planned_dispatch(job)
        agent.finish_planned_dispatch(job, agent.run_planned_dispatch(job))
        self.assertTrue(agent.plan_evidence_ready("goal"))

    def test_a_late_evidence_read_cannot_serve_a_newer_attention(self):
        self.needing_judgment_after_restart()
        agent = self.agent()
        (job,) = agent.advance_due_plans()
        job = agent.begin_planned_dispatch(job)
        attempt = agent.run_planned_dispatch(job)
        # Meanwhile the attention was answered and a new one raised.
        current = self.plan_of()
        self.set_goal(execution_plan=replace(current, attention_seq=2, attention=replace(
            current.attention, seq=2)))
        agent.finish_planned_dispatch(job, attempt)
        self.assertNotIn(job.evidence_for, agent._plan_evidence_cache)


class OrphanedEvidenceReadTests(PlanHarness):
    """TREX: an evidence read stopped mid-flight must recover on ticks alone."""

    def offerable(self, agent):
        return PlanAttentionSource(self.store, Ledger(), True, clock=lambda: self.now,
                                   ready=agent.plan_evidence_ready).due_opportunities()

    def test_a_read_stopped_by_a_restart_recovers_without_any_core_turn(self):
        agent = self.installed(step("review", wait=10))
        self.outputs["review"] = [JUDGE, JUDGE]
        self.work(agent)
        self.restart()
        stopped = self.agent()
        (read,) = stopped.advance_due_plans()        # checkpointed ...
        self.assertTrue(read.call.call_id.startswith("plan-evidence-"))
        self.restart()                                # ... and the process stops
        recovered = self.agent()
        self.assertEqual(self.offerable(recovered), ())
        self.work(recovered)                          # ticks only: no person, no Core
        self.assertEqual(self.names(), ["review", "review"])
        orphan = next(item for item in self.state().attempts
                      if item.call.call_id == read.call.call_id)
        self.assertEqual(orphan.reason_code, "dispatch_interrupted")
        self.assertTrue(recovered.plan_evidence_ready("goal"))
        self.assertEqual(len(self.offerable(recovered)), 1)

    def test_a_read_whose_result_was_not_recorded_is_read_again_in_process(self):
        agent = self.installed(step("review", wait=10))
        self.outputs["review"] = [JUDGE, JUDGE, JUDGE]
        self.work(agent)
        self.restart()
        agent = self.agent()
        (read,) = agent.advance_due_plans()
        read = agent.begin_planned_dispatch(read)
        agent.run_planned_dispatch(read)
        agent._live_plan_dispatches.discard(read.call.call_id)  # recording failed
        self.work(agent)
        self.assertTrue(agent.plan_evidence_ready("goal"))
        self.assertEqual(self.names(), ["review", "review", "review"])

    def test_a_failed_checkpoint_write_leaves_nothing_scheduled_and_is_retried(self):
        agent = self.installed(step("review", wait=10))
        self.outputs["review"] = [JUDGE, JUDGE]
        self.work(agent)
        self.restart()
        agent = self.agent()
        original = self.store.replace
        failures = []

        def failing_once(state, *arguments, **keywords):
            if not failures and any(item.call is not None
                                    and item.call.call_id.startswith("plan-evidence-")
                                    for item in state.attempts):
                failures.append(True)
                raise OSError("disk full")
            return original(state, *arguments, **keywords)
        self.store.replace = failing_once
        self.assertEqual(agent.advance_due_plans(), ())
        self.assertEqual(agent._evidence_scheduled, set())
        self.assertFalse(agent.plan_evidence_ready("goal"))
        self.work(agent)                              # the store has recovered
        self.assertEqual(failures, [True])
        self.assertTrue(agent.plan_evidence_ready("goal"))
        self.assertEqual(len(self.offerable(agent)), 1)


class CheckStepPrecedenceTests(unittest.TestCase):
    @staticmethod
    def outcome(*runs):
        def read(_request):
            return PullRequestChecks(95, HEAD, tuple(runs), ())
        return build_pull_request_checks_executors(read, lambda: "r")[
            "read_pull_request_checks"]({"pull_request_number": 95, "head_sha": HEAD}).outcome

    @staticmethod
    def run_(status, conclusion=None, steps=None):
        return CheckRun("check", status, conclusion, None, None, None, "github-actions",
                        "Actions", None, None, steps)

    def test_a_settled_step_is_never_hidden_by_anything_still_running(self):
        queued = self.run_("queued")
        cases = {
            "action_required step in a running job": (
                (self.run_("in_progress", None, (("build", "success"),
                                                 ("approve", "action_required"))), queued),
                ExecutionOutcome.AMBIGUOUS),
            "stale step in a running job": (
                (self.run_("in_progress", None, (("lint", "stale"),)), queued),
                ExecutionOutcome.AMBIGUOUS),
            "failed step beside an action_required one": (
                (self.run_("in_progress", None, (("test", "failure"),
                                                 ("approve", "action_required"))),),
                ExecutionOutcome.FAILURE),
            "steps still running": (
                (self.run_("in_progress", None, (("build", "success"), ("test", None))), queued),
                ExecutionOutcome.PENDING),
            "completed with passing steps": (
                (self.run_("completed", "success", (("build", "success"), ("docs", "skipped"))),),
                ExecutionOutcome.SUCCESS),
        }
        for name, (runs, expected) in cases.items():
            with self.subTest(case=name):
                self.assertEqual(self.outcome(*runs), expected)


class ReplyDurabilityTests(PlanHarness):
    """A finish or cancel and the reply announcing it are never inconsistent."""

    def setUp(self):
        PlanHarness.setUp(self)
        from alx.conversation import ConversationGateway, SQLiteConversationStore

        self.conversation_path = self.path.with_name("conversations.sqlite3")
        self.conversations = SQLiteConversationStore(self.conversation_path)
        self.addCleanup(lambda: self.conversations.close())
        self.gateway_class = ConversationGateway

    def finish_ready(self):
        agent = self.installed(step("coding"))
        self.work(agent)
        self.assertEqual(self.plan_of().attention.reason, "plan_steps_done")

    def gateway(self, reasoner):
        return self.gateway_class(self.agent(reasoner), self.conversations)

    def finish_turn(self, gateway):
        turn = ConversationTurn("thread", f"person-{len(self.calls)}-{self.now.timestamp()}",
                                ConversationOrigin.TYPED, "Thanks, close it.", self.now, "friedl")
        return gateway.receive_conversation_turn(turn, 4, RETENTION)

    def replies(self):
        return [item for item in self.conversations.load("thread").turns
                if item.origin is ConversationOrigin.ALX_RESPONSE]

    def finishing(self):
        return Reasoner(SELECT, resolve(PlanOperation.FINISH, "It is merged and done."))

    def test_the_ordinary_path_stores_the_reply_once_and_clears_it(self):
        self.finish_ready()
        gateway = self.gateway(self.finishing())
        self.finish_turn(gateway)
        self.assertEqual([item.content for item in self.replies()], ["It is merged and done."])
        self.assertEqual(self.plan_of().status, PlanStatus.COMPLETED)
        self.assertEqual(self.plan_of().announcements, ())
        self.assertEqual(gateway.reconcile_plan_announcements(), 0)
        self.assertEqual(len(self.replies()), 1)

    def test_a_failed_resolution_write_stores_no_reply(self):
        self.finish_ready()
        gateway = self.gateway(self.finishing())
        original = self.store.replace

        def failing(state, *arguments, **keywords):
            if (state.execution_plan is not None
                    and state.execution_plan.status is PlanStatus.COMPLETED):
                raise OSError("goal store unavailable")
            return original(state, *arguments, **keywords)
        self.store.replace = failing
        with self.assertRaises(OSError):
            self.finish_turn(gateway)
        self.store.replace = original
        self.assertEqual(self.replies(), [])
        self.assertEqual(self.plan_of().status, PlanStatus.NEEDS_CORE)
        self.assertEqual(gateway.reconcile_plan_announcements(), 0)
        self.assertEqual(self.replies(), [])

    def test_a_failed_reply_store_is_recovered_once(self):
        self.finish_ready()
        gateway = self.gateway(self.finishing())
        original = self.conversations.append
        state = {"failed": False}

        def failing(turn, *arguments, **keywords):
            if turn.origin is ConversationOrigin.ALX_RESPONSE and not state["failed"]:
                state["failed"] = True
                raise OSError("conversation store unavailable")
            return original(turn, *arguments, **keywords)
        self.conversations.append = failing
        with self.assertRaises(OSError):
            self.finish_turn(gateway)
        self.assertEqual(self.replies(), [])
        self.assertEqual(self.plan_of().status, PlanStatus.COMPLETED)
        self.assertTrue(self.plan_of().announcements)
        self.assertEqual(gateway.reconcile_plan_announcements(), 1)
        self.assertEqual([item.content for item in self.replies()], ["It is merged and done."])
        self.assertEqual(self.plan_of().announcements, ())

    def test_a_crash_between_the_writes_is_recovered_after_restart(self):
        self.finish_ready()
        self.conversations.create("thread", RETENTION)          # the person's turn was stored
        agent = self.agent(self.finishing())
        outcome = agent.process(conversation(), RETENTION, 4)   # the gateway never runs
        self.assertIsNotNone(outcome.response_turn_id)
        self.restart()
        gateway = self.gateway(Reasoner())
        self.assertEqual(gateway.reconcile_plan_announcements(), 1)
        self.assertEqual(gateway.reconcile_plan_announcements(), 0)
        (reply,) = self.replies()
        self.assertEqual((reply.turn_id, reply.content),
                         (outcome.response_turn_id, "It is merged and done."))

    def test_a_failed_acknowledgement_never_duplicates_the_reply(self):
        self.finish_ready()
        gateway = self.gateway(self.finishing())
        original = gateway._core.plan_announcement_stored
        gateway._core.plan_announcement_stored = lambda *_arguments: (_ for _ in ()).throw(
            OSError("goal store unavailable"))
        self.finish_turn(gateway)
        self.assertTrue(self.plan_of().announcements)
        gateway._core.plan_announcement_stored = original
        self.restart()
        gateway = self.gateway(Reasoner())
        gateway.reconcile_plan_announcements()
        self.assertEqual(len(self.replies()), 1)
        self.assertEqual(self.plan_of().announcements, ())

    def test_a_reply_she_did_not_deliver_is_never_announced(self):
        self.finish_ready()
        # Remaining work is immediately executable, so the first answer is
        # deferred; her second step answers without closing the plan.
        self.set_goal(outstanding_work=(WorkItem("notes", "write release notes"),))
        reasoner = Reasoner(SELECT, resolve(PlanOperation.FINISH, "All done."),
                            AgentDecision(response="Writing the release notes next.",
                                          goal_id="goal"))
        gateway = self.gateway(reasoner)
        self.finish_turn(gateway)
        self.assertEqual([item.content for item in self.replies()],
                         ["Writing the release notes next."])
        self.assertEqual(self.plan_of().status, PlanStatus.NEEDS_CORE)
        self.assertEqual(self.plan_of().announcements, ())
        self.assertEqual(gateway.reconcile_plan_announcements(), 0)

    def test_cancel_follows_the_same_rule(self):
        self.finish_ready()
        gateway = self.gateway(Reasoner(SELECT, resolve(PlanOperation.CANCEL, "Stopped it.")))
        original = self.conversations.append
        calls = {"n": 0}

        def failing(turn, *arguments, **keywords):
            if turn.origin is ConversationOrigin.ALX_RESPONSE and calls["n"] == 0:
                calls["n"] += 1
                raise OSError("down")
            return original(turn, *arguments, **keywords)
        self.conversations.append = failing
        with self.assertRaises(OSError):
            self.finish_turn(gateway)
        self.assertEqual(self.plan_of().status, PlanStatus.CANCELLED)
        gateway.reconcile_plan_announcements()
        self.assertEqual([item.content for item in self.replies()], ["Stopped it."])

    def test_a_replacement_closing_before_reconciliation_loses_no_reply(self):
        self.finish_ready()
        original = self.conversations.append
        storage = {"down": True}

        def failing(turn, *arguments, **keywords):
            if storage["down"] and turn.turn_id.startswith("alx-plan-reply:"):
                raise OSError("conversation store unavailable")
            return original(turn, *arguments, **keywords)
        self.conversations.append = failing
        with self.assertRaises(OSError):                      # plan A's reply
            self.finish_turn(self.gateway(self.finishing()))
        self.now += timedelta(seconds=1)
        self.finish_turn(self.gateway(Reasoner(            # plan B replaces it
            SELECT, install(plan(step("cleanup")), "Starting the cleanup."))))
        self.work(self.agent())
        self.now += timedelta(seconds=1)
        with self.assertRaises(OSError):                      # plan B's reply
            self.finish_turn(self.gateway(Reasoner(
                SELECT, resolve(PlanOperation.CANCEL, "Cleanup stopped."))))
        self.assertEqual(len(self.plan_of().announcements), 2)
        storage["down"] = False
        gateway = self.gateway(Reasoner())
        self.assertEqual(gateway.reconcile_plan_announcements(), 2)
        self.assertEqual(gateway.reconcile_plan_announcements(), 0)
        self.assertEqual([item.content for item in self.replies()],
                         ["Starting the cleanup.", "It is merged and done.", "Cleanup stopped."])
        self.assertEqual(self.plan_of().announcements, ())


class RejectedDecisionCorrectionTests(PlanHarness):
    """A decision the validator rejects gets one correction, never more."""

    class Rejecting(Reasoner):
        def decide(self, context):
            self.contexts.append(context)
            item = self.decisions.pop(0)
            if isinstance(item, Exception):
                raise item
            return item

    def rejected(self, path="succeeded"):
        from alx.contracts import DecisionValidationError
        return DecisionValidationError(f"plan condition path is unusable: {path!r}")

    def test_a_corrected_decision_installs_normally_after_one_rejection(self):
        reasoner = self.Rejecting(self.rejected(), install(plan(step("coding", completion=(
            PlanCondition("state", "succeeded"),))), "Started; I will report back."))
        outcome = self.person(self.agent(reasoner))
        self.assertEqual(reasoner.calls, 2)
        (refusal,) = reasoner.contexts[1].refused_calls
        self.assertEqual(refusal, {"reason": "decision_rejected",
                                   "subject": "plan condition path is unusable: 'succeeded'"})
        self.assertEqual((outcome.state, outcome.response),
                         (CoreState.RESPONDED, "Started; I will report back."))
        self.assertEqual(self.plan_of().status, PlanStatus.RUNNING)
        self.work(self.agent())
        self.assertEqual(self.names(), ["coding"])

    def test_a_second_rejection_stops_and_the_person_is_still_answered(self):
        reasoner = self.Rejecting(self.rejected(), self.rejected("values..x"),
                                  AgentDecision(response="I could not start that work."))
        outcome = self.person(self.agent(reasoner), budget=6)
        # Two decisions that could plan, then one that can only speak.
        self.assertEqual(reasoner.calls, 3)
        self.assertIsNone(reasoner.contexts[1].response_only_reason)
        self.assertEqual(reasoner.contexts[2].response_only_reason, "decision_rejected")
        self.assertEqual([item["subject"] for item in reasoner.contexts[2].refused_calls],
                         ["plan condition path is unusable: 'succeeded'",
                          "plan condition path is unusable: 'values..x'"])
        self.assertEqual((outcome.state, outcome.response),
                         (CoreState.RESPONDED, "I could not start that work."))
        self.assertIsNone(self.plan_of())
        self.assertEqual(self.state().attempts, ())
        self.assertEqual(self.calls, [])

    def test_an_autonomous_turn_stops_after_one_correction_without_speaking(self):
        reasoner = self.Rejecting(self.rejected(), self.rejected())
        outcome = self.agent(reasoner).process(conversation(), RETENTION, 6,
                                               origin=CognitionOrigin.WORK_COMPLETED)
        self.assertEqual(reasoner.calls, 2)
        self.assertEqual((outcome.state, outcome.reason), (CoreState.ERROR, "decision_rejected"))
        self.assertIsNone(self.plan_of())

    def test_no_correction_is_bought_without_a_step_to_spend(self):
        reasoner = self.Rejecting(self.rejected(), AgentDecision(response="Could not start."))
        outcome = self.person(self.agent(reasoner), budget=1)
        self.assertEqual(reasoner.contexts[1].response_only_reason, "decision_rejected")
        self.assertEqual(outcome.response, "Could not start.")


class PlannedCallIdentityTests(PlanHarness):
    """Planned call ids obey the contract of the capabilities that consume them."""

    def test_a_planned_coding_step_passes_the_real_coding_validation(self):
        from alx.contracts.coding import job_id_permitted
        from alx.tools.coding import DEFINITION as CODING, build_coding_executors

        current = {"call_id": ""}
        jobs = []

        def run_job(request):
            jobs.append(request)
            raise RuntimeError("the job itself is not under test")
        executors = build_coding_executors(run_job, lambda: current["call_id"],
                                           lambda: self.state())

        def dispatch(call, _state):
            current["call_id"] = call.call_id
            self.calls.append(call)
            result = executors["run_coding_task"](dict(call.arguments))
            return CapabilityAttempt(call, CapabilityAttemptDisposition.EXECUTED, True, result)
        # The exact arguments Core planned in the real acceptance run.
        workflow = plan(ExecutionStep(CapabilityCall("code", "run_coding_task", {
            "task": "Append exactly one new line to the end of docs/FOUNDATION_PROOF.md "
                    "that reads: Acceptance run marker for PR #95 (throwaway, do not "
                    "merge).  Change nothing else in the file or repository.",
            "acceptance_criteria": [
                "The last line of docs/FOUNDATION_PROOF.md is exactly: Acceptance run "
                "marker for PR #95 (throwaway, do not merge).",
                "No other file or existing line is changed."],
            "context": "Acceptance test of background execution plans in a scratch clone.",
            "repair_branch": "acceptance/pr95-marker",
            "commit_message": "docs: add throwaway acceptance run marker for PR #95",
            "step_budget": 8,
        }), ()))
        agent = CoreAgent(self.store, Reasoner(install(workflow)), dispatch,
                          (*DEFINITIONS, CODING), clock=lambda: self.now,
                          budget_check=self.budget, plan_continuation=True)
        self.person(agent)
        (job,) = agent.advance_due_plans()
        self.assertTrue(job_id_permitted(job.call.call_id))
        job = agent.begin_planned_dispatch(job)
        attempt = agent.run_planned_dispatch(job)
        # Validation accepted the planned id: the job runner was reached with it.
        self.assertEqual([item.job_id for item in jobs], [job.call.call_id])
        self.assertNotEqual((attempt.result.failure or {}).get("code"), "arguments_unusable")
        agent.finish_planned_dispatch(job, attempt)
        (recorded,) = [item for item in self.state().attempts
                       if item.call.call_id == job.call.call_id]
        self.assertIs(recorded.disposition, CapabilityAttemptDisposition.EXECUTED)

    def test_every_planned_call_id_is_a_valid_workspace_identity_and_unique(self):
        from alx.contracts.coding import job_id_permitted
        from alx.core.loop import planned_call_id

        identifiers = [planned_call_id(prefix) for prefix in ("plan", "plan-evidence")
                       for _ in range(200)]
        self.assertEqual(len(set(identifiers)), len(identifiers))
        self.assertTrue(all(job_id_permitted(item) and ":" not in item
                            for item in identifiers))

    def test_the_runner_produces_only_valid_ids_for_steps_and_evidence_reads(self):
        from alx.contracts.coding import job_id_permitted

        agent = self.installed(step("review", wait=10))
        self.outputs["review"] = [JUDGE, JUDGE]
        self.work(agent)
        self.restart()
        agent = self.agent()
        (read,) = agent.advance_due_plans()
        self.assertTrue(read.call.call_id.startswith("plan-evidence-"))
        planned = [item.call.call_id for item in self.state().attempts]
        self.assertTrue(planned)
        self.assertTrue(all(job_id_permitted(item) for item in planned))

    def test_no_runtime_source_generates_a_colon_call_id(self):
        source = (SRC / "core" / "loop.py").read_text()
        self.assertNotIn('call_id=f"plan:', source)
        self.assertNotIn('call_id=f"plan-evidence:', source)


class LatestReviewRegressionTests(PlanHarness):
    """Greptile's review of 0c47fb2: a failed start, and a replaced plan's reply."""

    def test_a_failed_started_write_never_strands_the_plan(self):
        agent = self.installed(step("coding"))
        (job,) = agent.advance_due_plans()
        original = self.store.replace

        def failing(state, *arguments, **keywords):
            plan_ = state.execution_plan
            if plan_ is not None and plan_.inflight is not None and plan_.inflight.started:
                raise OSError("goal store unavailable")
            return original(state, *arguments, **keywords)
        self.store.replace = failing
        with self.assertRaises(OSError):
            agent.begin_planned_dispatch(job)
        self.store.replace = original
        self.assertNotIn(job.call.call_id, agent._live_plan_dispatches)
        self.work(agent)                      # the same process, storage recovered
        self.assertEqual(self.names(), ["coding"])
        self.assertEqual(self.plan_of().attention.reason, "plan_steps_done")

    def test_a_replacement_plan_keeps_a_reply_not_yet_stored(self):
        from alx.contracts import PlanAnnouncement

        agent = self.installed(step("coding"))
        self.work(agent)
        finished = replace(self.plan_of(), status=PlanStatus.COMPLETED, attention=None,
                           announcements=(PlanAnnouncement("alx-plan-reply:1", "Done."),))
        self.set_goal(execution_plan=finished)
        self.person(self.same_process(agent, Reasoner(install(plan(step("cleanup")),
                                                              "Next job started."))))
        replacement = self.plan_of()
        self.assertEqual(replacement.status, PlanStatus.RUNNING)
        self.assertEqual(replacement.announcements,
                         (PlanAnnouncement("alx-plan-reply:1", "Done."),))


class StopDuringBeginTests(unittest.IsolatedAsyncioTestCase, PlanHarness):
    def setUp(self):
        PlanHarness.setUp(self)

    async def test_shutdown_during_the_dispatch_boundary_still_asks_the_step_to_cancel(self):
        agent = self.installed(step("coding"))
        inside, release = threading.Event(), threading.Event()
        original = agent.begin_planned_dispatch

        def slow_begin(job):
            inside.set()
            release.wait(5)
            return original(job)
        agent.begin_planned_dispatch = slow_begin
        cancelled = []
        workers = PlanWorkers(agent, asyncio.Lock(),
                              cancel_dispatch=lambda job: cancelled.append(job.call.call_id))
        await workers.advance()
        self.assertTrue(await asyncio.to_thread(inside.wait, 5))
        stopping = asyncio.ensure_future(workers.stop())
        await asyncio.sleep(0.05)
        release.set()
        await stopping
        # The worker crossed the boundary before stop took effect, so it was
        # registered in time to be asked to cancel.
        self.assertEqual(len(cancelled), 1)
        self.assertEqual(workers._tasks, set())


class GateTests(PlanHarness):
    """Expiry, budget, finished goals, and installation refusals."""

    def test_an_expired_goal_is_neither_executed_nor_offered(self):
        agent = self.installed(step("coding"))
        self.now = RETENTION + timedelta(seconds=1)
        self.work(agent)
        self.assertEqual(self.calls, [])
        self.assertEqual(PlanAttentionSource(self.store, Ledger(), True,
                                             clock=lambda: self.now).due_opportunities(), ())

    def test_a_budget_stop_defers_the_step_without_waking_her(self):
        agent = self.installed(step("coding"))
        self.budget_stopped = True
        self.work(agent)
        current = self.plan_of()
        self.assertEqual((current.status, current.attention), (PlanStatus.WAITING, None))
        self.assertEqual(self.state().attempts, ())
        self.budget_stopped = False
        self.later(300)
        self.work(agent)
        self.assertEqual(self.names(), ["coding"])

    def test_a_finished_goal_closes_its_plan(self):
        agent = self.installed(step("coding"), step("merge"))
        self.set_goal(status=GoalStatus.CANCELLED, stop_reason=GoalStopReason.CANCELLED)
        self.work(agent)
        self.assertEqual(self.plan_of().status, PlanStatus.CANCELLED)
        self.assertEqual(self.calls, [])

    def test_installation_refusals(self):
        cases = {
            "turn-bound step": (plan(step("merge")), {"turn_bound_capabilities":
                                                      frozenset({"merge"})},
                                "plan_requires_fresh_authority"),
            "wait on a capability that is not an observation": (
                plan(step("coding", wait=10)), {}, "plan_wait_unsafe"),
            "no way back to her": (plan(step("coding")), {"plan_continuation": False},
                                   "plan_continuation_unavailable"),
        }
        for name, (workflow, options, reason) in cases.items():
            with self.subTest(case=name):
                self.set_goal(execution_plan=None)
                reasoner = Reasoner(install(workflow),
                                    AgentDecision(response="I cannot plan that.",
                                                  goal_id="goal"))
                self.person(self.agent(reasoner, **options))
                self.assertEqual(reasoner.contexts[1].refused_calls[0]["reason"], reason)
                self.assertIsNone(self.plan_of())

    def test_a_repeated_identical_refusal_answers_the_person_once_and_stops(self):
        reasoner = Reasoner(install(plan(step("coding", wait=10))),
                            install(plan(step("coding", wait=10))),
                            AgentDecision(response="I cannot wait on that.", goal_id="goal"))
        outcome_ = self.person(self.agent(reasoner), budget=6)
        self.assertEqual(outcome_.state, CoreState.RESPONDED)
        self.assertEqual(reasoner.contexts[2].response_only_reason, "plan_wait_unsafe")
        self.assertIsNone(self.plan_of())

    def test_resolving_an_attention_needs_the_step_to_have_seen_it(self):
        self.installed(step("coding"))
        self.outputs["coding"] = FAILED
        self.work(self.agent())
        # Selected and resolved in one decision: she never saw the attention.
        reasoner = Reasoner(resolve(PlanOperation.RESUME),
                            resolve(PlanOperation.RESUME, "Retrying."))
        self.person(self.agent(reasoner))
        self.assertEqual(reasoner.contexts[1].refused_calls[0]["reason"], "plan_not_current")
        self.assertEqual(self.plan_of().status, PlanStatus.RUNNING)

    def test_a_replacement_needs_the_plan_it_replaces_to_have_been_seen(self):
        self.installed(step("coding"))
        reasoner = Reasoner(install(plan(step("merge"))),
                            AgentDecision(response="Let me look first.", goal_id="goal"))
        self.person(self.agent(reasoner))
        self.assertEqual(reasoner.contexts[1].refused_calls[0]["reason"],
                         "plan_replaces_unseen_plan")

    def test_the_runtime_owns_what_the_plan_serves_and_answers(self):
        self.installed(step("coding"), source_turn_id="invented",
                       objective_summary="Something else")
        installed = self.plan_of()
        self.assertEqual((installed.objective_source, installed.objective_summary,
                          installed.source_turn_id),
                         ("turn:person-1", "Do the work", "person-1"))
        self.assertTrue(installed.plan_id.startswith("plan:"))

    def test_a_plan_update_travels_only_with_words_or_silence(self):
        with self.assertRaisesRegex(ValueError, "response or silence"):
            AgentDecision(goal_id="goal", plan_update=PlanUpdate(PlanOperation.RESUME))


class RecordTests(unittest.TestCase):
    def test_wait_bounds_and_state_shapes(self):
        with self.assertRaises(ValueError):
            step("ci", wait=10, bound=86_401)
        with self.assertRaises(ValueError):
            ExecutionStep(CapabilityCall("c", "ci", {}), (), 0, 60)
        bound = dict(objective_source="turn:t", objective_summary="s")
        with self.assertRaises(ValueError):
            plan(step("ci"), status=PlanStatus.NEEDS_CORE, **bound)
        with self.assertRaises(ValueError):
            plan(step("ci"), status=PlanStatus.WAITING, inflight=PlanDispatch(0, "x"),
                 next_due_at=NOW, **bound)
        with self.assertRaises(ValueError):
            PlanAttention(1, "reason", ("other",), NOW)

    def test_condition_paths_follow_the_one_grammar(self):
        for path in ("state", "values", "values.merged", "values.check.conclusion",
                     "failure", "failure.code", "failure.details.reason"):
            with self.subTest(accepted=path):
                self.assertEqual(PlanCondition(path, True).path, path)
        for path in ("succeeded", "state.x", "result.state", "values..x", "values._x",
                     "values.*", "failure.co*de", "", " "):
            with self.subTest(rejected=path):
                with self.assertRaisesRegex(ValueError, "plan condition path is unusable"):
                    PlanCondition(path, True)

    def test_a_goal_cannot_claim_a_dispatch_it_did_not_record(self):
        with self.assertRaisesRegex(ValueError, "recorded attempt"):
            new_goal(execution_plan=plan(step("ci"), inflight=PlanDispatch(0, "missing"),
                                         objective_source="turn:person-1",
                                         objective_summary="Do the work"))


class JsonConditionTests(unittest.TestCase):
    def test_json_equal_keeps_booleans_and_numbers_distinct(self):
        self.assertFalse(json_equal(True, 1))
        self.assertFalse(json_equal(False, 0))
        self.assertTrue(json_equal(1, 1.0))
        self.assertFalse(json_equal(None, False))
        self.assertTrue(json_equal({"a": [1, True]}, {"a": (1.0, True)}))
        self.assertFalse(json_equal({"a": [1]}, {"a": [True]}))

    def test_conditions_use_json_semantics_and_absence_is_not_a_value(self):
        document = {"values": {"merged": 1, "nothing": None}}
        self.assertFalse(condition_matches(document, PlanCondition("values.merged", True)))
        self.assertTrue(condition_matches(document, PlanCondition("values.nothing", None)))
        self.assertFalse(condition_matches(document, PlanCondition("values.absent", None)))
        self.assertFalse(condition_matches(document,
                                           PlanCondition("values.absent", None, True)))
        self.assertTrue(condition_matches(document, PlanCondition("values.merged", 2, True)))

    def test_preconditions_use_the_same_equality(self):
        workflow = plan(step("ci"), objective_source="turn:person-1",
                        objective_summary="Do the work", context_preconditions={"ok": True})
        self.assertEqual(plan_invalidation_facts(workflow, new_goal(context={"ok": 1}), NOW),
                         ("plan_precondition_changed",))
        self.assertEqual(plan_invalidation_facts(workflow, new_goal(context={"ok": True}), NOW),
                         ())


class SinglePathTests(unittest.TestCase):
    """Acceptance invariant 12: one classifier, one reducer, nothing superseded left."""

    @staticmethod
    def callers(name):
        found = []
        for path in SRC.rglob("*.py"):
            tree = ast.parse(path.read_text())
            for function in ast.walk(tree):
                if not isinstance(function, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    continue
                for node in ast.walk(function):
                    if (isinstance(node, ast.Call)
                            and getattr(node.func, "id", getattr(node.func, "attr", None))
                            == name):
                        found.append(f"{path.relative_to(SRC)}::{function.name}")
        return sorted(set(found))

    def test_results_are_classified_and_reduced_in_one_place(self):
        self.assertEqual(self.callers("classify_planned_result"),
                         ["core/loop.py::_reduce_planned_result"])
        self.assertEqual(self.callers("reduce_plan"), ["core/loop.py::_reduce_planned_result"])
        self.assertEqual(self.callers("outcome_of"),
                         ["core/plan_results.py::classify_planned_result"])

    def test_superseded_continuation_machinery_is_deleted(self):
        import alx.continuity.ledger as ledger
        import alx.continuity.plan_source as plan_source
        import alx.conversation.gateway as gateway
        for name in ("acknowledge_plan_response", "plan_response_turn_id",
                     "_continuation_identity", "_answered_continuation",
                     "_handle_resolved_plan", "_uncheckpointed_plan_attempt",
                     "_plan_step_for", "_active_plan_attempt", "_plan_mechanical_blocker"):
            self.assertFalse(hasattr(CoreAgent, name), name)
        self.assertFalse(hasattr(plan_source, "PlanContinuationSource"))
        self.assertFalse(hasattr(PlanAttentionSource, "recover"))
        self.assertFalse(hasattr(ledger.SQLiteOpportunityLedger, "outcome"))
        self.assertFalse(hasattr(gateway.ConversationGateway, "advance_due_plans"))
        # The gateway stores replies through one idempotent append; only a
        # finish or cancel reply carries a fixed id, and it is held on the
        # plan, not on any continuation identity.
        self.assertEqual(self.callers("_append_reply"),
                         ["conversation/gateway.py::_store_response",
                          "conversation/gateway.py::reconcile_plan_announcements"])


if __name__ == "__main__":
    unittest.main()
