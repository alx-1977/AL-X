"""Coding retry protection: at most four counted failures in a row per goal.

Friedl's rule of 2026-10-24. A succeeded coding job resets the count. A
correction is a job like any other: it may run under the same goal, on the
exact preserved checkout, with the recorded failure reproduced into the
coding session, and it counts like any other job. Failures that say nothing
about the code (quota, login, CLI crash, interruption, infrastructure)
do not count.
"""

from __future__ import annotations

import json
import sys
import tempfile
import unittest
from dataclasses import replace
from datetime import timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from tests.test_coding_agent import (  # noqa: E402
    NOW, RETENTION, _FIXED, _worktree, PlanningModel, RecordingSession,
)
from alx.contracts import (  # noqa: E402
    AgentDecision, CapabilityAttempt, CapabilityAttemptDisposition,
    CapabilityCall, CapabilityDefinition, CapabilityResult,
    CapabilityResultState, ConversationOrigin, ConversationSnapshot,
    ConversationTurn, GoalState, Objective, SideEffect, StructuredSchema,
    SuccessCriterion, ValueKind,
)
from alx.contracts.coding import CodingError, CodingSessionResult  # noqa: E402
from alx.contracts.coding_verification import pytest_failed_tests  # noqa: E402
from alx.core import CoreAgent  # noqa: E402
from alx.goals import SQLiteGoalStore  # noqa: E402
from alx.providers.coding_agent import CodingAgent  # noqa: E402
from alx.tools.coding import build_coding_executors  # noqa: E402

DEFINITION = CapabilityDefinition(
    "run_coding_task", "bounded coding job",
    StructuredSchema(ValueKind.OBJECT), StructuredSchema(ValueKind.OBJECT),
    SideEffect.EFFECTFUL,
)
_WRONG = "def add(a, b):\n    return a * b\n"
_FAILED_TEST = "test_app.py::AddTests::test_add"


def verification_failed(call_id: str, arguments: dict, *, tests=(),
                        digest: str = "d" * 64) -> CapabilityAttempt:
    """A run that implemented and then failed its targeted tests."""
    checkpoint = {
        "job_id": call_id, "branch": "feat/work", "stage": "test",
        "head_sha": "a" * 40, "state_digest": digest,
    }
    return CapabilityAttempt(
        CapabilityCall(call_id, "run_coding_task", arguments),
        CapabilityAttemptDisposition.EXECUTED, True,
        CapabilityResult(
            call_id, "run_coding_task", CapabilityResultState.FAILED,
            {"status": "failed", "checkpoint": json.dumps(checkpoint),
             "verification": {"checks": [
                 {"name": "diff_check", "passed": True, "findings": []},
                 {"name": "pytest_targeted", "passed": False,
                  "findings": list(tests)},
             ]}},
            {"code": "required_verification_failed", "status": "failed",
             "phase": "test", "session_completed": True},
        ),
    )


def timed_out(call_id: str, arguments: dict) -> CapabilityAttempt:
    """A pre-watchdog timeout; each one recorded progress on the tree."""
    return CapabilityAttempt(
        CapabilityCall(call_id, "run_coding_task", arguments),
        CapabilityAttemptDisposition.EXECUTED, True,
        CapabilityResult(
            call_id, "run_coding_task", CapabilityResultState.FAILED,
            {"status": "failed", "checkpoint": json.dumps({
                "job_id": call_id, "stage": "execution", "head_sha": "a" * 40,
                "state_digest": call_id.ljust(64, "0"),
            })},
            {"code": "session_failed", "phase": "execution",
             "reason_code": "session_timeout"},
        ),
    )


def refused(call_id: str, arguments: dict, reason: str) -> CapabilityAttempt:
    return CapabilityAttempt(
        CapabilityCall(call_id, "run_coding_task", arguments),
        CapabilityAttemptDisposition.REJECTED, False, reason_code=reason,
    )


def succeeded(call_id: str) -> CapabilityAttempt:
    return CapabilityAttempt(
        CapabilityCall(call_id, "run_coding_task", {"task": call_id}),
        CapabilityAttemptDisposition.EXECUTED, True,
        CapabilityResult(call_id, "run_coding_task",
                         CapabilityResultState.SUCCEEDED, {"status": "succeeded"}),
    )


def goal(*attempts: CapabilityAttempt) -> GoalState:
    return GoalState(
        "goal-a", Objective("turn:t", "add the checks capability"),
        (SuccessCriterion("c", "merged"),), attempts=attempts,
    )


def corrective(resume: str, action: str) -> CapabilityCall:
    return CapabilityCall("probe", "run_coding_task", {
        "resume_job_id": resume, "corrective_action": action,
    })


def recorded_case() -> GoalState:
    """The shape of the GitHub-checks goal as its durable record holds it.

    Two pre-watchdog timeouts, a refusal recorded while they still counted,
    then two resumes that failed the same verification on an unchanged tree.
    """
    return goal(
        timed_out("code-01", {"task": "add checks", "repair_branch": "feat/work",
                              "commit_message": "add checks"}),
        timed_out("resume-01", {"resume_job_id": "code-01"}),
        refused("resume-02", {"resume_job_id": "resume-01"}, "coding_retry_exhausted"),
        verification_failed("resume-04", {"resume_job_id": "resume-01"}),
        verification_failed("resume-05", {"resume_job_id": "resume-01"}),
    )


def failed(call_id: str, failure: dict, values: dict | None = None,
           state: CapabilityResultState = CapabilityResultState.FAILED,
           arguments: dict | None = None) -> CapabilityAttempt:
    return CapabilityAttempt(
        CapabilityCall(call_id, "run_coding_task", arguments or {"task": call_id}),
        CapabilityAttemptDisposition.EXECUTED, True,
        CapabilityResult(call_id, "run_coding_task", state,
                         {"status": "failed", **(values or {})}, failure),
    )


def uncounted_failures() -> tuple[CapabilityAttempt, ...]:
    """Failures that say nothing about the code the job was given."""
    commit_checkpoint = json.dumps({"stage": "commit", "state_digest": "c" * 64})
    return (
        failed("quota", {"code": "session_failed", "reason_code": "usage_exhausted"}),
        failed("login", {"code": "session_failed", "reason_code": "unauthenticated"}),
        failed("crash", {"code": "session_failed", "reason_code": "session_exited"}),
        failed("provider", {"code": "provider_failed", "phase": "execution"}),
        failed("unavailable", {"code": "coding_unavailable"}),
        failed("sandbox", {"code": "sandbox_unusable"}),
        failed("dispatch", {"code": "dispatch_interrupted"}),
        failed("interrupted", {"code": "session_interrupted"},
               {"status": "interrupted"}, CapabilityResultState.PARTIAL),
        failed("cancelled", {"code": "coding_cancelled", "phase": "test"}),
        failed("arguments", {"code": "arguments_unusable"}),
        failed("before", {"code": "git_refused", "reason_code": "canonical_checkout_dirty",
                          "implementation_reached": False}),
        failed("test-infra", {"code": "required_verification_failed",
                              "failure_class": "test_infrastructure"}),
        failed("commit-infra", {"code": "git_refused",
                                "failure_class": "commit_infrastructure"}),
        failed("commit-phase", {"code": "git_refused", "phase": "commit"}),
        failed("commit-stage", {"code": "task_failed"},
               {"checkpoint": commit_checkpoint}),
        failed("reviewer", {"code": "review_failed", "phase": "local_review",
                            "review_classification": "infrastructure"}),
    )


class ConsecutiveFailureAccounting(unittest.TestCase):
    """Friedl, 2026-10-24: at most four counted failures in a row per goal."""

    def test_four_counted_failures_in_a_row_exhaust_the_goal(self):
        three = tuple(verification_failed(f"f{n}", {"task": "a"}) for n in range(3))
        self.assertEqual(CoreAgent._consecutive_coding_failures(goal(*three)), 3)
        self.assertIsNone(CoreAgent._coding_exhaustion_reason(goal(*three)))
        four = goal(*three, verification_failed("f3", {"task": "a"}))
        self.assertEqual(CoreAgent._consecutive_coding_failures(four), 4)
        self.assertEqual(
            CoreAgent._coding_exhaustion_reason(four), "coding_retry_exhausted"
        )

    def test_a_success_resets_the_count(self):
        state = goal(
            *(verification_failed(f"f{n}", {"task": "a"}) for n in range(3)),
            succeeded("ok"),
            *(verification_failed(f"g{n}", {"task": "b"}) for n in range(3)),
        )
        self.assertEqual(CoreAgent._consecutive_coding_failures(state), 3)
        self.assertIsNone(CoreAgent._coding_exhaustion_reason(state))

    def test_the_refused_third_correction_of_2026_10_24_is_allowed(self):
        """Corrections of earlier, solved failures no longer count against a new one.

        A failing AL/X-requested check records no test identifiers, so
        unrelated failures looked identical and the third, different
        correction was refused as coding_correction_exhausted.
        """
        state = goal(
            verification_failed("f1", {"task": "a"}),
            verification_failed("c1", {"resume_job_id": "f1",
                                       "corrective_action": "first fix"}),
            succeeded("c2"),
            verification_failed("f2", {"task": "b"}),
            verification_failed("c3", {"resume_job_id": "f2",
                                       "corrective_action": "second fix"}),
        )
        call = corrective("c3", "a third, different fix")
        self.assertEqual(CoreAgent._consecutive_coding_failures(state), 2)
        self.assertIsNone(CoreAgent._coding_exhaustion_reason(state))
        self.assertIsNone(CoreAgent._coding_interruption_exhaustion_reason(state, call))
        self.assertIsNone(CoreAgent._binding_rejected_call(state, call, NOW))

    def test_a_correction_is_counted_like_any_job_and_never_refused_as_the_same(self):
        same = ("tests/test_x.py::T::test_one",)
        state = goal(
            verification_failed("f1", {"task": "a"}, tests=same),
            verification_failed("c1", {"resume_job_id": "f1",
                                       "corrective_action": "stub one"}, tests=same),
            verification_failed("c2", {"resume_job_id": "c1",
                                       "corrective_action": "stub one"}, tests=same),
        )
        self.assertEqual(CoreAgent._consecutive_coding_failures(state), 3)
        # The same diagnosis of the same failure again is AL/X's judgement.
        self.assertIsNone(CoreAgent._coding_exhaustion_reason(state))
        self.assertIsNone(CoreAgent._binding_rejected_call(
            state, corrective("c2", "stub one"), NOW
        ))
        fourth = verification_failed(
            "c3", {"resume_job_id": "c2", "corrective_action": "stub two"}, tests=same,
        )
        self.assertEqual(
            CoreAgent._coding_exhaustion_reason(goal(*state.attempts, fourth)),
            "coding_retry_exhausted",
        )

    def test_failures_that_say_nothing_about_the_code_do_not_count(self):
        for item in uncounted_failures():
            with self.subTest(failure=item.call.call_id):
                self.assertFalse(CoreAgent._counts_as_coding_failure(item))
        state = goal(
            *(verification_failed(f"f{n}", {"task": "a"}) for n in range(3)),
            *uncounted_failures(), *uncounted_failures(),
        )
        # Neither counted nor a reset: three stays three.
        self.assertEqual(CoreAgent._consecutive_coding_failures(state), 3)
        self.assertIsNone(CoreAgent._coding_exhaustion_reason(state))
        state = goal(*state.attempts, verification_failed("f3", {"task": "a"}))
        self.assertEqual(
            CoreAgent._coding_exhaustion_reason(state), "coding_retry_exhausted"
        )

    def test_failures_about_the_task_count(self):
        counted = (
            failed("planning", {"code": "planning_failed", "phase": "planning",
                                "implementation_reached": False}),
            failed("conflict", {"code": "required_verification_failed",
                                "failure_class": "request_conflict"}),
            failed("verification", {"code": "required_verification_failed",
                                    "phase": "test"}),
            failed("task", {"code": "task_failed"}),
        )
        for item in counted:
            with self.subTest(failure=item.call.call_id):
                self.assertTrue(CoreAgent._counts_as_coding_failure(item))
        self.assertEqual(
            CoreAgent._coding_exhaustion_reason(goal(*counted)), "coding_retry_exhausted"
        )

    def test_stale_refusals_do_not_count(self):
        state = goal(
            refused("r1", {"task": "x"}, "coding_retry_exhausted"),
            refused("r2", {"task": "y"}, "coding_correction_exhausted"),
            verification_failed("f1", {"task": "z"}),
        )
        self.assertEqual(CoreAgent._consecutive_coding_failures(state), 1)
        self.assertIsNone(CoreAgent._coding_exhaustion_reason(state))
        # Recorded before the latest run, so they describe older evidence.
        self.assertFalse(CoreAgent._coding_retry_already_exhausted(
            state, "coding_retry_exhausted"
        ))
        # A refusal of a retired bound does not bind an identical call.
        self.assertIsNone(CoreAgent._binding_rejected_call(
            state, CapabilityCall("again", "run_coding_task", {"task": "y"}), NOW
        ))

    def test_the_recorded_timeouts_do_not_count(self):
        state = recorded_case()
        self.assertEqual(CoreAgent._consecutive_coding_failures(state), 2)
        self.assertIsNone(CoreAgent._coding_exhaustion_reason(state))

    def test_the_bound(self):
        from alx.core.loop import _MAX_CONSECUTIVE_CODING_FAILURES
        self.assertEqual(_MAX_CONSECUTIVE_CODING_FAILURES, 4)

    def test_no_other_coding_bound_remains(self):
        import alx.core.loop as loop
        for retired in (
            "_MAX_FAILED_CODING_EXECUTIONS", "_MAX_CORRECTIONS_PER_FAILURE",
            "_MAX_PLANNING_CODING_FAILURES", "_MAX_REQUEST_CONFLICT_CODING_EXECUTIONS",
            "_CODING_CORRECTION_REFUSALS",
        ):
            self.assertFalse(hasattr(loop, retired), retired)
        for retired in (
            "_open_coding_failure_episode", "_failed_coding_executions",
            "_coding_failure_signature", "_coding_correction_refusal",
            "_request_conflict_coding_executions", "_planning_coding_failures",
            "_coding_execution_failures", "_is_corrective_coding_dispatch",
            "_is_coding_execution_failure",
        ):
            self.assertFalse(hasattr(CoreAgent, retired), retired)


class CoreOwnsTheTransition(unittest.TestCase):
    """No reset, no new goal and no override: Core dispatches until the bound."""

    def process(self, state, decisions, dispatch, steps=None):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        store = SQLiteGoalStore(Path(directory.name) / "goals.sqlite3")
        self.addCleanup(store.close)
        store.create(state, "conversation", RETENTION)

        class Reasoner:
            def decide(self, context):
                return decisions(context)

        conversation = ConversationSnapshot("conversation", (ConversationTurn(
            "conversation", "t", ConversationOrigin.TYPED, "continue", NOW, "friedl",
        ),), 1, RETENTION)
        outcome = CoreAgent(
            store, Reasoner(), dispatch, (DEFINITION,), clock=lambda: NOW,
        ).process(conversation, RETENTION, steps or 4)
        return outcome, store

    def test_core_dispatches_a_retry_and_a_correction_under_the_bound(self):
        dispatched = []

        def dispatch(call, authority):
            dispatched.append(call)
            return CapabilityAttempt(
                call, CapabilityAttemptDisposition.EXECUTED, True,
                CapabilityResult(call.call_id, "run_coding_task",
                                 CapabilityResultState.SUCCEEDED, {"status": "succeeded"}),
            )

        script = [
            AgentDecision(call=CapabilityCall("replay", "run_coding_task",
                                              {"resume_job_id": "resume-05"}),
                          goal_id="goal-a"),
            AgentDecision(call=CapabilityCall("repair", "run_coding_task", {
                "resume_job_id": "resume-05",
                "corrective_action": "Both provider stubs lack the new method; add it.",
            }), goal_id="goal-a"),
            AgentDecision(response="Repaired.", goal_id="goal-a"),
        ]
        outcome, store = self.process(recorded_case(), lambda _: script.pop(0), dispatch)
        self.assertEqual([call.call_id for call in dispatched], ["replay", "repair"])
        self.assertEqual(outcome.response, "Repaired.")
        # Restart: a fresh read of the durable goal reaches the same verdict.
        self.assertEqual(
            CoreAgent._consecutive_coding_failures(store.load("goal-a").state), 0
        )

    def test_a_correction_after_four_failures_in_a_row_is_refused(self):
        state = goal(*(verification_failed(f"f{n}", {"task": "a"}) for n in range(4)))
        script = [
            AgentDecision(call=CapabilityCall("repair", "run_coding_task", {
                "resume_job_id": "f3", "corrective_action": "a new diagnosis",
            }), goal_id="goal-a"),
            AgentDecision(response="Four in a row failed.", goal_id="goal-a"),
        ]
        outcome, store = self.process(
            state, lambda _: script.pop(0),
            lambda *_: self.fail("exhaustion must not reach dispatch"),
        )
        attempts = store.load("goal-a").state.attempts
        self.assertEqual(attempts[-1].reason_code, "coding_retry_exhausted")
        self.assertFalse(attempts[-1].implementation_invoked)
        self.assertEqual(outcome.response, "Four in a row failed.")

    def test_a_job_loop_that_never_succeeds_terminates_at_four(self):
        dispatched = []
        same = ("tests/test_x.py::T::test_one",)

        def dispatch(call, authority):
            dispatched.append(call.call_id)
            attempt = verification_failed(call.call_id, dict(call.arguments), tests=same)
            return replace(attempt, call=call)

        counter = iter(range(1, 100))

        def decisions(context):
            # A model that never stops: corrections alternating with retries
            # of the latest failure. The first step selects the goal.
            attempts = context.active_goal.attempts if context.active_goal else ()
            latest = ([item.call.call_id for item in attempts
                       if item.result is not None
                       and item.result.state is CapabilityResultState.FAILED]
                      or ["f0"])[-1]
            number = next(counter)
            arguments = {"resume_job_id": latest}
            if number % 2:
                arguments["corrective_action"] = f"diagnosis {number}"
            return AgentDecision(
                call=CapabilityCall(f"call-{number}", "run_coding_task", arguments),
                goal_id="goal-a",
            )

        outcome, store = self.process(
            goal(verification_failed("f0", {"task": "a"}, tests=same)),
            decisions, dispatch, steps=40,
        )
        refusals = {item.reason_code for item in store.load("goal-a").state.attempts
                    if item.disposition is CapabilityAttemptDisposition.REJECTED}
        self.assertEqual(dispatched, ["call-1", "call-2", "call-3"])
        self.assertEqual(refusals, {"coding_retry_exhausted"})
        self.assertNotEqual(outcome.state.value, "error")


class CorrectiveSessionCarriesTheFailure(unittest.TestCase):
    """The provider reproduces the recorded failure into the session."""

    def setUp(self) -> None:
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = _worktree(Path(directory.name))
        self.planner = PlanningModel()
        self.goal: GoalState = goal()

    def run_call(self, call_id: str, arguments: dict, session) -> CapabilityResult:
        agent = CodingAgent(self.planner, session, self.planner, repository=self.root)
        result = build_coding_executors(
            agent.run, lambda: call_id, lambda: self.goal,
        )["run_coding_task"](arguments)
        attempt = CapabilityAttempt(
            CapabilityCall(call_id, "run_coding_task", arguments),
            CapabilityAttemptDisposition.EXECUTED, True, result,
        )
        self.goal = replace(self.goal, attempts=(*self.goal.attempts, attempt))
        return result

    def failed_job(self) -> CapabilityResult:
        result = self.run_call("job-1", {
            "task": "fix add", "repair_branch": "fix/job-1", "commit_message": "fix add",
        }, RecordingSession(edits={"app.py": _WRONG}))
        self.assertIs(result.state, CapabilityResultState.FAILED)
        self.assertEqual(result.failure["code"], "required_verification_failed")
        return result

    def test_failure_is_recorded_as_test_identifiers_not_output(self):
        result = self.failed_job()
        checkpoint = json.loads(result.durable_values["checkpoint"])
        failed = [check for check in checkpoint["verification"]["checks"]
                  if not check["passed"]]
        self.assertEqual([check["findings"] for check in failed], [[_FAILED_TEST]])
        self.assertNotIn("assert", result.durable_values["checkpoint"].casefold())

    def test_verification_failure_details_reach_the_corrective_session(self):
        self.failed_job()
        session = RecordingSession(edits={"app.py": _FIXED})
        plans_before = sum(1 for item in self.planner.requests
                           if item.output_schema_name == "alx_coding_plan")
        result = self.run_call("job-2", {
            "resume_job_id": "job-1",
            "corrective_action": "add multiplies; it must return a + b.",
        }, session)
        self.assertIs(result.state, CapabilityResultState.SUCCEEDED, result.failure)
        self.assertTrue(result.values["all_required_verification_passed"])
        self.assertTrue(result.values["commit_sha"])
        briefing = session.calls[0][1]
        self.assertIn("# Correction of a recorded failure", briefing)
        self.assertIn("required_verification_failed", briefing)
        self.assertIn(_FAILED_TEST, briefing)
        self.assertIn("### Output of pytest_targeted", briefing)
        self.assertIn("2 != 3", briefing)
        self.assertIn("add multiplies; it must return a + b.", briefing)
        self.assertNotIn(str(self.root), briefing)
        # The preserved plan is reused; nothing is planned again.
        self.assertEqual(plans_before, sum(
            1 for item in self.planner.requests
            if item.output_schema_name == "alx_coding_plan"
        ))
        self.assertEqual(CoreAgent._consecutive_coding_failures(self.goal), 0)

    def test_a_correction_that_does_not_repair_is_not_committed(self):
        self.failed_job()
        result = self.run_call("job-2", {
            "resume_job_id": "job-1", "corrective_action": "try subtraction",
        }, RecordingSession(edits={"app.py": "def add(a, b):\n    return b - a\n"}))
        self.assertIs(result.state, CapabilityResultState.FAILED)
        self.assertEqual(result.failure["code"], "required_verification_failed")
        self.assertFalse(result.values.get("commit_sha"))
        checkpoint = json.loads(result.durable_values["checkpoint"])
        self.assertEqual(checkpoint["corrective_action"], "try subtraction")
        # The correction's failure counts like any other job's.
        self.assertEqual(CoreAgent._consecutive_coding_failures(self.goal), 2)

    def test_an_interrupted_correction_resumes_with_its_diagnosis(self):
        self.failed_job()

        class Interrupted(RecordingSession):
            def run_session(self, request, briefing):
                self.calls.append((request, briefing))
                raise CodingError("session_interrupted", reason_code="session_stalled")

        first = self.run_call("job-2", {
            "resume_job_id": "job-1", "corrective_action": "return a + b",
        }, Interrupted())
        self.assertIs(first.state, CapabilityResultState.PARTIAL)
        self.assertIsNone(CoreAgent._coding_exhaustion_reason(self.goal))
        session = RecordingSession(edits={"app.py": _FIXED})
        result = self.run_call("job-3", {"resume_job_id": "job-2"}, session)
        self.assertIs(result.state, CapabilityResultState.SUCCEEDED, result.failure)
        self.assertIn("return a + b", session.calls[0][1])
        self.assertIn(_FAILED_TEST, session.calls[0][1])

    def test_the_executor_refuses_corrections_without_failure_evidence(self):
        refused_before = (
            ({"task": "x", "repair_branch": "fix/x", "commit_message": "x",
              "corrective_action": "fix"}, "requires_resume"),
            ({"resume_job_id": "job-1", "corrective_action": "   "}, "blank"),
        )
        self.failed_job()
        for arguments, reason in refused_before:
            with self.subTest(reason=reason):
                result = build_coding_executors(
                    lambda request: self.fail("must not run"), lambda: "job-9",
                    lambda: self.goal,
                )["run_coding_task"](arguments)
                self.assertEqual(result.failure["code"], "arguments_unusable")
                self.assertEqual(result.failure["reason_code"], reason)

        interrupted = CapabilityAttempt(
            CapabilityCall("job-i", "run_coding_task", {"resume_job_id": "job-1"}),
            CapabilityAttemptDisposition.EXECUTED, True,
            CapabilityResult("job-i", "run_coding_task", CapabilityResultState.PARTIAL,
                             {"status": "interrupted", "checkpoint": json.dumps({
                                 "job_id": "job-i", "branch": "fix/job-1",
                                 "stage": "execution"})}),
        )
        at_commit = CapabilityAttempt(
            CapabilityCall("job-c", "run_coding_task", {"resume_job_id": "job-1"}),
            CapabilityAttemptDisposition.EXECUTED, True,
            CapabilityResult("job-c", "run_coding_task", CapabilityResultState.FAILED,
                             {"status": "failed", "checkpoint": json.dumps({
                                 "job_id": "job-c", "branch": "fix/job-1",
                                 "stage": "commit", "commit_candidate_sha": "b" * 40})},
                             {"code": "git_refused", "phase": "commit"}),
        )
        self.goal = replace(self.goal, attempts=(*self.goal.attempts, interrupted, at_commit))
        for resume, reason in (("job-i", "corrective_action_without_failure"),
                               ("job-c", "corrective_action_stage_unsupported")):
            with self.subTest(reason=reason):
                result = build_coding_executors(
                    lambda request: self.fail("must not run"), lambda: "job-9",
                    lambda: self.goal,
                )["run_coding_task"]({"resume_job_id": resume, "corrective_action": "fix"})
                self.assertEqual(result.failure["reason_code"], reason)
                self.assertIs(result.failure["implementation_reached"], False)

    def test_a_correction_cannot_change_the_authorised_request(self):
        self.failed_job()
        result = build_coding_executors(
            lambda request: self.fail("must not run"), lambda: "job-9", lambda: self.goal,
        )["run_coding_task"]({
            "resume_job_id": "job-1", "corrective_action": "fix",
            "task": "something else entirely",
        })
        self.assertEqual(result.failure["reason_code"], "resume_request_changed")


class FailedTestIdentifiers(unittest.TestCase):
    def test_identifiers_only_and_bounded(self):
        output = "\n".join([
            "....F",
            "FAILED tests/a.py::T::test_one - AssertionError: secret value 42",
            "ERROR tests/b.py::test_two - ImportError: nope",
            "FAILED tests/a.py::T::test_one - AssertionError: again",
            *(f"FAILED tests/c.py::test_{n}" for n in range(40)),
        ])
        found = pytest_failed_tests(output)
        self.assertEqual(found[:2], ("tests/a.py::T::test_one", "tests/b.py::test_two"))
        self.assertEqual(len(found), 20)
        self.assertFalse(any("secret" in item for item in found))


if __name__ == "__main__":
    unittest.main()
