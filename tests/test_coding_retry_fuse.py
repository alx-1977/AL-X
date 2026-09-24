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


def state(*attempts):
    return GoalState("goal-a", Objective("turn:t", "bounded work"), (SuccessCriterion("c", "done"),), attempts=attempts)


class CodingRetryFuseTests(unittest.TestCase):
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
                    AgentDecision(call=CapabilityCall("same", "run_coding_task", {"task": "first", "worktree": "."}), goal_id="goal-a"),
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
            self.assertEqual(outcome.reason, "coding_retry_exhausted")
            self.assertEqual(reworded_outcome.reason, "coding_retry_exhausted")


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
        # The third job reached implementation, and it alone is counted.
        self.assertEqual(self.model.calls, 1)
        self.assertEqual(CoreAgent._failed_coding_executions(state(*attempts)), 1)

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
