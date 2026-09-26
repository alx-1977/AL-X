"""The Coding Agent failed-run fuse is durable, goal-scoped, and pre-dispatch."""

from __future__ import annotations

import subprocess
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
from alx.contracts.coding import CodingError, CodingRequest  # noqa: E402
from alx.core import CoreAgent  # noqa: E402
from alx.conversation import ConversationGateway, SQLiteConversationStore  # noqa: E402
from alx.goals import SQLiteGoalStore  # noqa: E402
from alx.providers.coding_agent import CodingAgent  # noqa: E402
from alx.providers.coding_git import coding_job_lock  # noqa: E402
from alx.tools.coding import build_coding_executors  # noqa: E402


def attempt(call_id: str, *, failed=True, invoked=True, disposition=CapabilityAttemptDisposition.EXECUTED, failure_code="task_failed", arguments=None):
    call = CapabilityCall(call_id, "run_coding_task", arguments or {"task": call_id, "worktree": "."})
    if disposition is CapabilityAttemptDisposition.REJECTED:
        return CapabilityAttempt(call, disposition, False, reason_code="input_invalid")
    result = CapabilityResult(call_id, "run_coding_task", CapabilityResultState.FAILED if failed else CapabilityResultState.SUCCEEDED, failure={"code": failure_code} if failed else None)
    return CapabilityAttempt(call, disposition, invoked, result, "executor_error" if disposition is CapabilityAttemptDisposition.BROKER_FAILURE else None)


def planning_failure(call_id: str):
    call = CapabilityCall(call_id, "run_coding_task", {"task": call_id})
    result = CapabilityResult(
        call_id,
        "run_coding_task",
        CapabilityResultState.FAILED,
        failure={
            "code": "planning_failed",
            "phase": "planning",
            "planning_attempts": 3,
            "structured_output_received": True,
            "parsing_succeeded": True,
            "validation_succeeded": False,
            "implementation_reached": False,
        },
    )
    return CapabilityAttempt(call, CapabilityAttemptDisposition.EXECUTED, True, result)


def state(*attempts):
    return GoalState("goal-a", Objective("turn:t", "bounded work"), (SuccessCriterion("c", "done"),), attempts=attempts)


class CodingRetryFuseTests(unittest.TestCase):
    def test_stage_infrastructure_and_cancellation_do_not_spend_implementation_retry(self):
        failures = [
            {"code": "review_failed", "phase": "local_review",
             "review_classification": "infrastructure"},
            {"code": "required_verification_failed", "phase": "test",
             "failure_class": "test_infrastructure"},
            {"code": "git_refused", "phase": "commit",
             "failure_class": "commit_infrastructure"},
            {"code": "coding_cancelled", "phase": "review"},
        ]
        attempts = tuple(
            CapabilityAttempt(
                CapabilityCall(f"stage-{index}", "run_coding_task", {"task": "work"}),
                CapabilityAttemptDisposition.EXECUTED, True,
                CapabilityResult(f"stage-{index}", "run_coding_task",
                                 CapabilityResultState.FAILED, failure=failure),
            ) for index, failure in enumerate(failures)
        )
        self.assertEqual(CoreAgent._failed_coding_executions(state(*attempts)), 0)

    def test_correction_failure_counts_after_an_earlier_review_transport_failure(self):
        failure = {
            "code": "session_failed", "phase": "local_review",
            "review_classification": "infrastructure",
        }
        item = CapabilityAttempt(
            CapabilityCall("correction", "run_coding_task", {"task": "work"}),
            CapabilityAttemptDisposition.EXECUTED, True,
            CapabilityResult("correction", "run_coding_task",
                             CapabilityResultState.FAILED, failure=failure),
        )
        self.assertEqual(CoreAgent._failed_coding_executions(state(item)), 1)

    def test_changed_arguments_do_not_change_goal_scoped_identity(self):
        self.assertEqual(CoreAgent._failed_coding_executions(state(attempt("first"), attempt("rewritten"))), 2)

    def test_success_and_pre_effect_rejection_do_not_consume_allowance(self):
        self.assertEqual(CoreAgent._failed_coding_executions(state(attempt("ok", failed=False), attempt("rejected", disposition=CapabilityAttemptDisposition.REJECTED))), 0)

    def test_pre_effect_argument_validation_does_not_consume_allowance(self):
        invalid = attempt("invalid", failure_code="arguments_unusable")
        self.assertEqual(CoreAgent._failed_coding_executions(state(invalid, attempt("ran"))), 1)

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

    def test_same_and_reworded_attempts_after_exhaustion_stay_stably_exhausted(self):
        now = datetime(2026, 9, 16, tzinfo=UTC)
        retention = now + timedelta(days=1)

        class Reasoner:
            def __init__(self):
                self.decisions = [
                    AgentDecision(call=CapabilityCall("first-refusal", "run_coding_task", {"task": "first", "worktree": "."}), goal_id="goal-a"),
                    AgentDecision(call=CapabilityCall("first-refusal", "run_coding_task", {"task": "first", "worktree": "."}), goal_id="goal-a"),
                    AgentDecision(response="The coding path is exhausted.", goal_id="goal-a"),
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
            outcome = agent.process(conversation, retention, 4)
            class Reworded:
                def decide(self, context):
                    return AgentDecision(call=CapabilityCall("reworded", "run_coding_task", {"task": "different wording", "worktree": "."}), goal_id="goal-a")

            reworded = CoreAgent(store, Reworded(), lambda *_: self.fail("must not dispatch"), (capability,), clock=lambda: now)
            reworded_outcome = reworded.process(conversation, retention, 1)
            refusals = [item for item in store.load("goal-a").state.attempts if item.reason_code == "coding_retry_exhausted"]
            self.assertEqual(len(refusals), 1)
            self.assertEqual(outcome.response, "The coding path is exhausted.")
            self.assertEqual(reworded_outcome.reason, "budget_exhausted")


def git(repository: Path, *argv: str) -> str:
    return subprocess.run(
        ["git", *argv], cwd=repository, check=True, capture_output=True, text=True
    ).stdout


class RecordingModel:
    """Records whether implementation was reached; never plans anything."""

    def __init__(self) -> None:
        self.calls = 0

    def complete(self, request):
        self.calls += 1
        raise RuntimeError("implementation reached")


class CheckoutPreconditionsDoNotSpendTheAllowance(unittest.TestCase):
    """A checkout refused before its feature branch exists spent nothing.

    D-030's amendment counts implementation-reaching failures. A checkout that
    is off main, dirty, or held by another job refuses before any model is
    asked anything, so two such refusals must not exhaust the goal.
    """

    def setUp(self) -> None:
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name).resolve()
        git(self.root, "init", "-q", "-b", "main")
        git(self.root, "config", "user.email", "test@example.test")
        git(self.root, "config", "user.name", "test")
        (self.root / "app.py").write_text("value = 1\n", encoding="utf-8")
        git(self.root, "add", "app.py")
        git(self.root, "commit", "-qm", "base")
        self.model = RecordingModel()
        self.agent = CodingAgent(
            self.model, None, self.model, repository=self.root
        )

    def request(self, job_id: str = "job-1") -> CodingRequest:
        return CodingRequest(
            task="change app", job_id=job_id,
            repair_branch="feat/change", commit_message="Change app",
        )

    def refusal(self) -> CodingError:
        with self.assertRaises(CodingError) as caught:
            self.agent.run(self.request())
        self.assertEqual(self.model.calls, 0)
        self.assertIs(caught.exception.details["implementation_reached"], False)
        return caught.exception

    def test_every_checkout_precondition_is_marked_before_implementation(self) -> None:
        git(self.root, "switch", "-q", "-c", "left-behind")
        self.assertEqual(
            self.refusal().details["reason_code"], "canonical_checkout_not_on_main"
        )
        git(self.root, "switch", "-q", "main")
        (self.root / "notes.txt").write_text("mine\n", encoding="utf-8")
        self.assertEqual(
            self.refusal().details["reason_code"], "canonical_checkout_dirty"
        )
        (self.root / "notes.txt").unlink()
        with coding_job_lock(self.root):
            self.assertEqual(
                self.refusal().details["reason_code"], "coding_job_active"
            )

    def test_refusals_leave_the_goal_able_to_dispatch_once_restored(self) -> None:
        now = datetime(2026, 9, 24, tzinfo=UTC)
        retention = now + timedelta(days=1)
        executor = None
        dispatched: list[str] = []
        root = self.root

        def dispatch(call, authority):
            dispatched.append(call.call_id)
            result = executor(call.arguments)
            return CapabilityAttempt(
                call, CapabilityAttemptDisposition.EXECUTED, True, result
            )

        executor = build_coding_executors(
            self.agent.run, lambda: dispatched[-1]
        )["run_coding_task"]
        arguments = {
            "task": "change app",
            "repair_branch": "feat/change",
            "commit_message": "Change app",
        }

        class Reasoner:
            def __init__(self):
                self.decisions = 0

            def decide(self, context):
                self.decisions += 1
                if self.decisions == 3:
                    # Friedl, or AL/X's repository authority, restores main.
                    git(root, "switch", "-q", "main")
                if self.decisions <= 3:
                    return AgentDecision(
                        call=CapabilityCall(
                            f"job-{self.decisions}", "run_coding_task", arguments
                        ),
                        goal_id="goal-a",
                    )
                return AgentDecision(response="done", goal_id="goal-a")

        git(self.root, "switch", "-q", "-c", "left-behind")
        with tempfile.TemporaryDirectory() as directory:
            store = SQLiteGoalStore(Path(directory) / "goals.sqlite3")
            self.addCleanup(store.close)
            store.create(state(), "conversation", retention)
            schema = StructuredSchema(ValueKind.OBJECT)
            capability = CapabilityDefinition(
                "run_coding_task", "bounded job", schema, schema,
                SideEffect.EFFECTFUL,
            )
            agent = CoreAgent(
                store, Reasoner(), dispatch, (capability,), clock=lambda: now
            )
            conversation = ConversationSnapshot("conversation", (
                ConversationTurn(
                    "conversation", "t", ConversationOrigin.TYPED, "continue",
                    now, "friedl",
                ),
            ), 1, retention)
            agent.process(conversation, retention, 6)
            attempts = store.load("goal-a").state.attempts

        self.assertEqual(dispatched, ["job-1", "job-2", "job-3"])
        self.assertNotIn(
            "coding_retry_exhausted", [item.reason_code for item in attempts]
        )
        refused = [item.result.failure for item in attempts[:2]]
        self.assertEqual(
            [item["reason_code"] for item in refused],
            ["canonical_checkout_not_on_main"] * 2,
        )
        # The third job reached the planning provider and failed before a plan
        # reached deterministic validation, so neither allowance is spent.
        self.assertEqual(self.model.calls, 1)
        self.assertEqual(CoreAgent._failed_coding_executions(state(*attempts)), 0)
        self.assertEqual(CoreAgent._planning_coding_failures(state(*attempts)), 0)

    def test_an_implementation_reaching_failure_still_counts(self) -> None:
        reached = attempt("reached", failure_code="provider_failed")
        refused = CapabilityAttempt(
            CapabilityCall("refused", "run_coding_task", {"task": "x"}),
            CapabilityAttemptDisposition.EXECUTED, True,
            CapabilityResult(
                "refused", "run_coding_task", CapabilityResultState.FAILED,
                failure={
                    "code": "git_refused",
                    "reason_code": "canonical_checkout_dirty",
                    "implementation_reached": False,
                },
            ),
        )
        self.assertEqual(
            CoreAgent._failed_coding_executions(state(refused, refused, reached)), 1
        )



def conflict(call_id: str):
    """A run that implemented but whose required check only the request blocked."""
    call = CapabilityCall(call_id, "run_coding_task", {"task": call_id})
    result = CapabilityResult(
        call_id, "run_coding_task", CapabilityResultState.FAILED,
        failure={
            "code": "required_verification_failed",
            "failure_class": "request_conflict",
        },
    )
    return CapabilityAttempt(call, CapabilityAttemptDisposition.EXECUTED, True, result)


def before_implementation(call_id: str):
    call = CapabilityCall(call_id, "run_coding_task", {"task": call_id})
    result = CapabilityResult(
        call_id, "run_coding_task", CapabilityResultState.FAILED,
        failure={
            "code": "git_refused",
            "reason_code": "canonical_checkout_dirty",
            "implementation_reached": False,
        },
    )
    return CapabilityAttempt(call, CapabilityAttemptDisposition.EXECUTED, True, result)


class RequestConflictsHaveTheirOwnBound(unittest.TestCase):
    """Three classes, two bounds, no reset.

    Genuine implementation failures spend D-028's allowance of two. A run whose
    required verification only the request's own blocked paths stopped is a
    request conflict: Core can correct the plan and dispatch again, but at most
    two per goal. A refusal before implementation spends neither.
    """

    def process(self, attempts, calls):
        """Run Core once over a goal holding `attempts`; return what it did."""
        now = datetime(2026, 9, 24, tzinfo=UTC)
        retention = now + timedelta(days=1)
        dispatched: list[str] = []

        def dispatch(call, authority):
            dispatched.append(call.call_id)
            result = CapabilityResult(
                call.call_id, "run_coding_task", CapabilityResultState.SUCCEEDED,
                {"status": "succeeded"},
            )
            return CapabilityAttempt(
                call, CapabilityAttemptDisposition.EXECUTED, True, result
            )

        class Reasoner:
            def __init__(self):
                self.decisions = [
                    AgentDecision(
                        call=CapabilityCall(
                            call_id, "run_coding_task", {"task": f"changed plan {call_id}"}
                        ),
                        goal_id="goal-a",
                    )
                    for call_id in calls
                ] + [AgentDecision(response="done", goal_id="goal-a")]

            def decide(self, context):
                return self.decisions.pop(0)

        with tempfile.TemporaryDirectory() as directory:
            store = SQLiteGoalStore(Path(directory) / "goals.sqlite3")
            self.addCleanup(store.close)
            store.create(state(*attempts), "conversation", retention)
            schema = StructuredSchema(ValueKind.OBJECT)
            capability = CapabilityDefinition(
                "run_coding_task", "bounded job", schema, schema, SideEffect.EFFECTFUL
            )
            agent = CoreAgent(store, Reasoner(), dispatch, (capability,), clock=lambda: now)
            conversation = ConversationSnapshot("conversation", (
                ConversationTurn(
                    "conversation", "t", ConversationOrigin.TYPED, "continue", now, "friedl"
                ),
            ), 1, retention)
            self.last_outcome = agent.process(conversation, retention, len(calls) + 2)
            final = store.load("goal-a").state.attempts
        refused = [
            item for item in final
            if item.reason_code in {
                "coding_retry_exhausted",
                "coding_planning_exhausted",
            }
        ]
        return dispatched, refused

    def test_the_classes_are_counted_apart(self) -> None:
        goal = state(
            conflict("c1"), attempt("i1"), before_implementation("p1"), conflict("c2")
        )
        self.assertEqual(CoreAgent._failed_coding_executions(goal), 1)
        self.assertEqual(CoreAgent._request_conflict_coding_executions(goal), 2)

    def test_genuine_implementation_failures_still_exhaust_the_allowance(self) -> None:
        dispatched, refused = self.process(
            [conflict("c1"), attempt("i1"), attempt("i2")], ["next"]
        )
        self.assertEqual(dispatched, [])
        self.assertEqual(len(refused), 1)
        self.assertEqual(refused[0].reason_code, "coding_retry_exhausted")
        self.assertEqual(self.last_outcome.response, "done")

    def test_refusals_before_implementation_spend_nothing(self) -> None:
        dispatched, refused = self.process(
            [before_implementation(f"p{n}") for n in range(4)], ["next"]
        )
        self.assertEqual(dispatched, ["next"])
        self.assertEqual(refused, [])

    def test_a_request_conflict_can_be_corrected_and_dispatched_again(self) -> None:
        """The acceptance run of 2026-09-24, replayed.

        Planning failed before implementation, then an edit to a governed document
        could not be verified because the request blocked the gate's script.
        Core changed the plan; that third job must dispatch.
        """
        dispatched, refused = self.process(
            [planning_failure("plan"), conflict("gate-blocked")],
            ["corrected"],
        )
        self.assertEqual(dispatched, ["corrected"])
        self.assertEqual(refused, [])

    def test_planning_and_implementation_allowances_are_independent(self) -> None:
        planning = planning_failure("plan")
        goal = state(attempt("implementation"), planning)
        self.assertEqual(CoreAgent._failed_coding_executions(goal), 1)
        self.assertEqual(CoreAgent._planning_coding_failures(goal), 1)
        dispatched, refused = self.process(
            [attempt("implementation"), planning], ["next-implementation"]
        )
        self.assertEqual(dispatched, ["next-implementation"])
        self.assertEqual(refused, [])

    def test_nonvalidation_planning_errors_do_not_consume_the_allowance(self) -> None:
        failures = []
        for call_id, code in (
            ("provider", "provider_failed"),
            ("transport", "transport_failed"),
            ("parsing", "plan_parse_failed"),
            ("persistence", "persistence_failed"),
        ):
            call = CapabilityCall(call_id, "run_coding_task", {"task": call_id})
            result = CapabilityResult(
                call_id,
                "run_coding_task",
                CapabilityResultState.FAILED,
                failure={
                    "code": code,
                    "phase": "planning",
                    "implementation_reached": False,
                },
            )
            failures.append(
                CapabilityAttempt(
                    call, CapabilityAttemptDisposition.EXECUTED, True, result
                )
            )

        self.assertEqual(CoreAgent._planning_coding_failures(state(*failures)), 0)
        dispatched, refused = self.process(failures, ["next-plan"])
        self.assertEqual(dispatched, ["next-plan"])
        self.assertEqual(refused, [])

    def test_two_planning_jobs_exhaust_only_the_planning_allowance(self) -> None:
        planning = [
            planning_failure("plan-1"),
            planning_failure("plan-2"),
        ]
        dispatched, refused = self.process(planning, ["reworded-plan"])
        self.assertEqual(dispatched, [])
        self.assertEqual(
            [item.reason_code for item in refused],
            ["coding_planning_exhausted"],
        )
        self.assertEqual(CoreAgent._failed_coding_executions(state(*planning)), 0)
        self.assertEqual(self.last_outcome.state.value, "responded")
        self.assertEqual(self.last_outcome.response, "done")

    def test_gateway_delivers_the_answer_after_planning_exhaustion(self) -> None:
        now = datetime(2026, 9, 24, tzinfo=UTC)
        retention = now + timedelta(days=1)

        class Reasoner:
            def __init__(self) -> None:
                self.decisions = [
                    AgentDecision(
                        call=CapabilityCall(
                            "reworded", "run_coding_task", {"task": "try again"}
                        ),
                        goal_id="goal-a",
                    ),
                    AgentDecision(
                        response="The coding planner is bounded; I can still help.",
                        goal_id="goal-a",
                    ),
                ]

            def decide(self, context):
                return self.decisions.pop(0)

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            goals = SQLiteGoalStore(root / "goals.sqlite3")
            conversations = SQLiteConversationStore(root / "conversations.sqlite3")
            self.addCleanup(goals.close)
            self.addCleanup(conversations.close)
            goals.create(
                state(
                    planning_failure("plan-1"),
                    planning_failure("plan-2"),
                ),
                "conversation",
                retention,
            )
            schema = StructuredSchema(ValueKind.OBJECT)
            capability = CapabilityDefinition(
                "run_coding_task", "bounded job", schema, schema,
                SideEffect.EFFECTFUL,
            )
            core = CoreAgent(
                goals, Reasoner(), lambda *_: self.fail("must not dispatch"),
                (capability,), clock=lambda: now,
            )
            gateway = ConversationGateway(
                core, conversations, identifier_factory=lambda: "alx-response",
                clock=lambda: now,
            )
            outcome = gateway.receive_conversation_turn(
                ConversationTurn(
                    "conversation", "person-turn", ConversationOrigin.TYPED,
                    "Continue", now, "friedl",
                ),
                3,
                retention,
            )
            persisted = conversations.load("conversation")

        self.assertEqual(outcome.response, "The coding planner is bounded; I can still help.")
        self.assertEqual(persisted.turns[-1].content, outcome.response)

    def test_repeated_request_conflicts_cannot_loop(self) -> None:
        # Two conflicts reach the bound whatever the wording of the next plan,
        # and the refusal is recorded once, however often Core asks again.
        dispatched, refused = self.process(
            [conflict("c1"), conflict("c2")], ["reworded", "reworded-again"]
        )
        self.assertEqual(dispatched, [])
        self.assertEqual(len(refused), 1)
        self.assertEqual(
            refused[0].reason_code, "coding_retry_exhausted"
        )
        self.assertEqual(self.last_outcome.response, "done")

    def test_each_bound_refuses_on_its_own(self) -> None:
        dispatched, _ = self.process([conflict("c1"), attempt("i1")], ["next"])
        self.assertEqual(dispatched, ["next"])
        dispatched, refused = self.process(
            [conflict("c1"), attempt("i1"), conflict("c2")], ["next"]
        )
        self.assertEqual(dispatched, [])
        self.assertEqual(len(refused), 1)
        self.assertEqual(self.last_outcome.response, "done")

    def test_an_error_carrying_a_class_reaches_core_without_it(self) -> None:
        """Only the Coding Agent's own verification records assign the class.

        A CodingError raised anywhere, carrying `failure_class`, reaches Core
        without it and so spends the implementation allowance like any other.
        """
        self.assertNotIn(
            "failure_class",
            CodingError("task_failed", failure_class="request_conflict").details,
        )

        def run_job(request):
            raise CodingError(
                "task_failed", reason_code="claimed", failure_class="request_conflict"
            )

        executor = build_coding_executors(run_job, lambda: "job-1")["run_coding_task"]
        result = executor({
            "task": "change app", "repair_branch": "feat/change",
            "commit_message": "Change app",
        })
        self.assertIs(result.state, CapabilityResultState.FAILED)
        self.assertEqual(result.failure["reason_code"], "claimed")
        self.assertNotIn("failure_class", result.failure)

        call = CapabilityCall("job-1", "run_coding_task", {"task": "change app"})
        goal = state(
            CapabilityAttempt(call, CapabilityAttemptDisposition.EXECUTED, True, result)
        )
        self.assertEqual(CoreAgent._failed_coding_executions(goal), 1)
        self.assertEqual(CoreAgent._request_conflict_coding_executions(goal), 0)

    def test_the_bounds(self) -> None:
        from alx.core.loop import (
            _MAX_FAILED_CODING_EXECUTIONS,
            _MAX_PLANNING_CODING_FAILURES,
            _MAX_REQUEST_CONFLICT_CODING_EXECUTIONS,
        )

        self.assertEqual(_MAX_FAILED_CODING_EXECUTIONS, 2)
        self.assertEqual(_MAX_PLANNING_CODING_FAILURES, 2)
        self.assertEqual(_MAX_REQUEST_CONFLICT_CODING_EXECUTIONS, 2)


class TheVerificationChangeDoesNotTouchTheRetryAccounting(unittest.TestCase):
    """10. D-028's fuse counts failed executions, whatever made them fail.

    The verification model changed underneath it: a job can now fail because a
    law gate failed rather than because pytest did. The fuse is indifferent to
    the reason — it counts failed coding executions on a goal — and this holds
    that indifference explicitly, so a later change to the limits or to what
    counts as a failure has to break a test that says so.
    """

    def test_the_allowance_and_its_limit_are_unchanged(self) -> None:
        from alx.core.loop import _MAX_FAILED_CODING_EXECUTIONS

        self.assertEqual(_MAX_FAILED_CODING_EXECUTIONS, 2)

    def test_a_verification_failure_consumes_one_allowance_like_any_other(self) -> None:
        """A required-check failure is one failed execution, not a new class."""
        counted = CoreAgent._failed_coding_executions(
            state(
                attempt("first", failure_code="required_verification_failed"),
                attempt("second", failure_code="task_failed"),
            )
        )
        self.assertEqual(counted, 2)

    def test_a_succeeded_unverified_job_still_consumes_nothing(self) -> None:
        """Success is success; the fuse counts failures only."""
        self.assertEqual(
            CoreAgent._failed_coding_executions(
                state(attempt("ok", failed=False), attempt("also-ok", failed=False))
            ),
            0,
        )


if __name__ == "__main__":
    unittest.main()
