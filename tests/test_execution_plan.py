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
                         clock=lambda: self.now, budget_check=self.budget, **options)

    @staticmethod
    def same_process(agent, reasoner):
        """The one Core of a running process, deciding with another reasoner."""
        agent._reasoner = reasoner
        return agent

    def work(self, agent):
        """Every due step, run to the next wait or attention, as workers would."""
        jobs = list(agent.advance_due_plans())
        while jobs:
            job = jobs.pop(0)
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

    def test_late_result_of_a_cancelled_plan_is_recorded_and_moves_nothing(self):
        agent = self.installed(step("coding"), step("merge"))
        (job,) = agent.advance_due_plans()
        # She stops it while the step is still running in the background.
        self.person(self.same_process(
            agent, Reasoner(SELECT, resolve(PlanOperation.CANCEL, "Stopped."))))
        self.assertEqual(self.plan_of().status, PlanStatus.CANCELLED)
        self.assertEqual(agent.finish_planned_dispatch(job, agent.run_planned_dispatch(job)), ())
        recorded = [item for item in self.state().attempts
                    if item.call.call_id == job.call.call_id]
        self.assertEqual(recorded[0].disposition, CapabilityAttemptDisposition.EXECUTED)
        self.assertEqual(self.plan_of().status, PlanStatus.CANCELLED)
        self.assertEqual(self.names(), ["coding"])

    def test_late_result_of_a_replaced_plan_moves_nothing(self):
        agent = self.installed(step("coding"))
        (job,) = agent.advance_due_plans()
        self.person(self.same_process(
            agent, Reasoner(SELECT, install(plan(step("cleanup")), "Replaced."))))
        replacement = self.plan_of()
        agent.finish_planned_dispatch(job, agent.run_planned_dispatch(job))
        self.assertEqual(self.plan_of(), replacement)

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
        agent.advance_due_plans()
        self.restart()
        self.work(self.agent())
        self.assertEqual(self.plan_of().attention.reason, "dispatch_interrupted")
        self.assertEqual(self.calls, [])

    def test_interrupted_observation_is_observed_again_without_her(self):
        agent = self.installed(step("ci", wait=10))
        agent.advance_due_plans()
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
        self.occasion(self.agent(reasoner))
        evidence = reasoner.contexts[0].transient_attempts
        self.assertEqual(evidence[0].result.values["findings"], ("fix",))
        self.assertEqual(self.names(), ["review", "review"])
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

    def source(self, notices=None):
        return PlanAttentionSource(
            self.store, Ledger(), enabled=True, clock=lambda: self.now,
            notify=(lambda conversation_id, values: notices.append((conversation_id, values)))
            if notices is not None else None,
        )

    def test_exhausted_offers_block_and_notify_once_without_reasoning(self):
        self.needing_core()
        notices = []
        source = self.source(notices)
        for _ in range(MAX_PAID_PLAN_OFFERS):
            (offer,) = source.due_opportunities()
            self.assertTrue(source.claim(offer))
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
        self.source(restarted).settle()
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
        self.assertFalse(hasattr(gateway.ConversationGateway, "_store_response"))


if __name__ == "__main__":
    unittest.main()
