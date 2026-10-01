"""Coding retry protection bounds failure episodes, not a goal's coding work.

Repeating a failed attempt without a new diagnosis is a repeat against the
same open episode and is refused after two failures, however it is worded. A
correction AL/X diagnoses against a recorded failure closes that episode and
may run under the same goal, on the exact preserved checkout, with the
recorded failure reproduced into the coding session. Corrections are bounded
too: they must answer evidence recorded since the last one, may not repeat a
diagnosis, and at most two may answer the same failure.
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


def plain(resume: str) -> CapabilityCall:
    return CapabilityCall("probe", "run_coding_task", {"resume_job_id": resume})


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


class FailureEpisodeAccounting(unittest.TestCase):
    def test_two_failures_on_unchanged_evidence_exhaust_one_episode(self):
        state = recorded_case()
        self.assertEqual(
            [item.call.call_id for item in CoreAgent._open_coding_failure_episode(state)],
            ["resume-04", "resume-05"],
        )
        for call in (plain("resume-05"), plain("resume-01"),
                     CapabilityCall("fresh", "run_coding_task", {"task": "reworded"})):
            with self.subTest(call=call.arguments):
                self.assertEqual(
                    CoreAgent._coding_exhaustion_reason(state, call),
                    "coding_retry_exhausted",
                )

    def test_a_distinct_evidence_backed_correction_is_allowed_under_the_same_goal(self):
        state = recorded_case()
        call = corrective("resume-05", "Both provider stubs omit the new read; add it.")
        self.assertIsNone(CoreAgent._coding_exhaustion_reason(state, call))
        self.assertIsNone(CoreAgent._coding_interruption_exhaustion_reason(state, call))
        self.assertIsNone(CoreAgent._binding_rejected_call(state, call, NOW))

    def test_old_timeout_interruptions_do_not_count(self):
        state = goal(
            timed_out("t1", {"task": "work"}),
            timed_out("t2", {"resume_job_id": "t1"}),
            timed_out("t3", {"resume_job_id": "t2"}),
            verification_failed("f1", {"resume_job_id": "t3"}),
        )
        self.assertEqual(CoreAgent._failed_coding_executions(state), 1)
        self.assertIsNone(CoreAgent._coding_exhaustion_reason(state, plain("f1")))

    def test_stale_refusals_do_not_count_as_implementation_failures(self):
        state = goal(
            refused("r1", {"task": "x"}, "coding_retry_exhausted"),
            refused("r2", {"task": "y"}, "coding_retry_exhausted"),
            verification_failed("f1", {"task": "z"}),
        )
        self.assertEqual(CoreAgent._failed_coding_executions(state), 1)
        self.assertIsNone(CoreAgent._coding_exhaustion_reason(state, plain("f1")))
        # Remaining evidence: the refusals are still in the durable record.
        self.assertEqual(
            [item.reason_code for item in state.attempts[:2]],
            ["coding_retry_exhausted"] * 2,
        )
        # Recorded before the latest run, so they describe older evidence.
        self.assertFalse(CoreAgent._coding_retry_already_exhausted(
            state, "coding_retry_exhausted"
        ))

    def test_a_success_resolves_the_episode(self):
        state = goal(
            verification_failed("f1", {"task": "a"}),
            verification_failed("f2", {"task": "a"}),
            succeeded("ok"),
        )
        self.assertEqual(CoreAgent._failed_coding_executions(state), 0)
        self.assertIsNone(CoreAgent._coding_exhaustion_reason(
            state, CapabilityCall("next", "run_coding_task", {"task": "next"})
        ))

    def test_a_blank_diagnosis_is_judged_as_a_plain_retry(self):
        state = recorded_case()
        for value in ("   ", None):
            with self.subTest(value=value):
                call = CapabilityCall("probe", "run_coding_task", {
                    "resume_job_id": "resume-05", "corrective_action": value,
                })
                self.assertEqual(
                    CoreAgent._coding_exhaustion_reason(state, call),
                    "coding_retry_exhausted",
                )

    def test_a_correction_must_answer_a_failure_in_the_open_episode(self):
        state = recorded_case()
        # The parent checkpoint is a timeout, not recorded failure evidence.
        self.assertEqual(
            CoreAgent._coding_exhaustion_reason(state, corrective("resume-01", "fix")),
            "coding_correction_unanchored",
        )
        self.assertEqual(
            CoreAgent._coding_exhaustion_reason(state, corrective("unknown", "fix")),
            "coding_correction_unanchored",
        )

    def test_repeated_no_progress_corrections_stay_bounded(self):
        same = ("tests/test_x.py::T::test_one", "tests/test_x.py::T::test_two")
        first = verification_failed("f1", {"task": "a"}, tests=same)
        c1 = verification_failed(
            "c1", {"resume_job_id": "f1", "corrective_action": "stub one"},
            tests=same, digest="e" * 64,
        )
        # c1 changed the tree but the same tests still fail: no progress.
        state = goal(first, c1)
        self.assertEqual(
            [item.call.call_id for item in CoreAgent._open_coding_failure_episode(state)],
            ["c1"],
        )
        self.assertEqual(
            CoreAgent._coding_exhaustion_reason(state, corrective("c1", "  STUB   one ")),
            "coding_correction_repeated",
        )
        self.assertIsNone(CoreAgent._coding_exhaustion_reason(state, corrective("c1", "stub two")))
        c2 = verification_failed(
            "c2", {"resume_job_id": "c1", "corrective_action": "stub two"},
            tests=same, digest="f" * 64,
        )
        state = goal(first, c1, c2)
        self.assertEqual(
            CoreAgent._coding_exhaustion_reason(state, corrective("c2", "a third idea")),
            "coding_correction_exhausted",
        )
        # A plain retry of the correction's own failure is bounded as usual.
        self.assertIsNone(CoreAgent._coding_exhaustion_reason(state, plain("c2")))
        again = verification_failed("c2-again", {"resume_job_id": "c2"}, tests=same)
        self.assertEqual(
            CoreAgent._coding_exhaustion_reason(goal(first, c1, c2, again), plain("c2")),
            "coding_retry_exhausted",
        )

    def test_a_correction_that_changes_the_failure_opens_new_evidence(self):
        first = verification_failed("f1", {"task": "a"}, tests=("t::one", "t::two"))
        c1 = verification_failed(
            "c1", {"resume_job_id": "f1", "corrective_action": "fix one"},
            tests=("t::two",),
        )
        c2 = verification_failed(
            "c2", {"resume_job_id": "c1", "corrective_action": "fix two"},
            tests=("t::three",),
        )
        self.assertIsNone(CoreAgent._coding_exhaustion_reason(
            goal(first, c1, c2), corrective("c2", "fix three")
        ))

    def test_a_correction_refused_before_implementation_neither_closes_nor_counts(self):
        state = recorded_case()
        before = CapabilityAttempt(
            CapabilityCall("c1", "run_coding_task", {
                "resume_job_id": "resume-05", "corrective_action": "fix stubs",
            }),
            CapabilityAttemptDisposition.EXECUTED, True,
            CapabilityResult("c1", "run_coding_task", CapabilityResultState.FAILED,
                             failure={"code": "git_refused",
                                      "reason_code": "resume_state_changed",
                                      "implementation_reached": False}),
        )
        state = replace(state, attempts=(*state.attempts, before))
        self.assertEqual(CoreAgent._failed_coding_executions(state), 2)
        self.assertIsNone(CoreAgent._coding_exhaustion_reason(
            state, corrective("resume-05", "fix stubs")
        ))

    def test_the_bounds(self):
        from alx.core.loop import (
            _MAX_CORRECTIONS_PER_FAILURE, _MAX_FAILED_CODING_EXECUTIONS,
        )
        self.assertEqual(_MAX_FAILED_CODING_EXECUTIONS, 2)
        self.assertEqual(_MAX_CORRECTIONS_PER_FAILURE, 2)


class CoreOwnsTheTransition(unittest.TestCase):
    """No reset, no new goal and no override: Core dispatches the correction."""

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

    def test_core_refuses_the_replay_and_dispatches_the_correction(self):
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
        attempts = store.load("goal-a").state.attempts
        self.assertEqual([call.call_id for call in dispatched], ["repair"])
        self.assertEqual(attempts[-2].reason_code, "coding_retry_exhausted")
        self.assertEqual(attempts[-1].call.call_id, "repair")
        self.assertIsNone(attempts[-1].call.approval_id)
        self.assertEqual(outcome.response, "Repaired.")
        # Restart: a fresh read of the durable goal reaches the same verdicts.
        reread = store.load("goal-a").state
        self.assertEqual(CoreAgent._failed_coding_executions(reread), 0)

    def test_a_correction_loop_that_makes_no_progress_terminates(self):
        dispatched = []
        same = ("tests/test_x.py::T::test_one",)

        def dispatch(call, authority):
            dispatched.append(call.call_id)
            attempt = verification_failed(call.call_id, dict(call.arguments), tests=same)
            return replace(attempt, call=call)

        counter = iter(range(1, 100))

        def decisions(context):
            # A model that never stops: always a fresh diagnosis of the latest
            # failure, alternating with a plain retry.
            # The first step selects the goal, so none is loaded yet.
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
        refusals = [item.reason_code for item in store.load("goal-a").state.attempts
                    if item.disposition is CapabilityAttemptDisposition.REJECTED]
        # Two corrections, each followed by one plain retry, then both bounds.
        self.assertEqual(dispatched, ["call-1", "call-2", "call-3", "call-4"])
        self.assertTrue(refusals)
        self.assertTrue(set(refusals) <= {
            "coding_retry_exhausted", "coding_correction_exhausted",
        })
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
        self.assertEqual(CoreAgent._failed_coding_executions(self.goal), 0)

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
        # That failure opened the next episode; the correction closed the first.
        self.assertEqual(
            [item.call.call_id for item in CoreAgent._open_coding_failure_episode(self.goal)],
            ["job-2"],
        )

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
        self.assertIsNone(CoreAgent._coding_exhaustion_reason(self.goal, plain("job-2")))
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
