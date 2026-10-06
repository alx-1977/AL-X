"""Stage recovery and cancellation preserve the one visible checkout."""

from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import threading
import unittest
from dataclasses import replace
from datetime import timedelta
from pathlib import Path
from unittest.mock import patch

from tests.test_coding_agent import (
    _FIXED, _worktree, PlanningModel, RecordingSession,
)
from alx.contracts.coding import (
    CodingCommandRecord, CodingError, CodingRequest, CodingSessionResult,
)
from alx.bootstrap.coding import build_coding_runtime
from alx.core import CoreAgent
from alx.capabilities import CapabilityBroker, CapabilityRegistry
from alx.contracts import (
    CapabilityAttempt, CapabilityAttemptDisposition, CapabilityCall,
    CapabilityResult, CapabilityResultState, GoalState, Objective, SuccessCriterion,
)
from alx.safety import AuthorityContext, SafetyGate
from tests.test_coding_agent import NOW
from alx.goals import SQLiteGoalStore
from alx.providers.coding_agent import CodingAgent
from alx.providers.coding_process import run_coding_subprocess
from alx.providers import coding_agent as coding_agent_module
from alx.tools.coding import build_coding_executors


def _wait_in_child(started: threading.Event) -> None:
    started.set()
    run_coding_subprocess(
        subprocess.run,
        [sys.executable, "-c", "import time; time.sleep(30)"],
        capture_output=True, text=True, timeout=35, check=False,
    )


class BlockingModel(PlanningModel):
    def __init__(self, stage: str, started: threading.Event) -> None:
        super().__init__()
        self.stage = stage
        self.started = started

    def complete(self, request):
        if request.output_schema_name == self.stage:
            _wait_in_child(self.started)
        return super().complete(request)


class BlockingSession(RecordingSession):
    def __init__(self, started: threading.Event) -> None:
        super().__init__()
        self.started = started

    def run_session(self, request, briefing):
        self.calls.append((request, briefing))
        (Path(request.worktree) / "app.py").write_text(_FIXED, encoding="utf-8")
        _wait_in_child(self.started)
        return CodingSessionResult(True, "completed after child")


class CodingRecoveryTests(unittest.TestCase):
    def setUp(self) -> None:
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = _worktree(Path(directory.name))
        self.request = CodingRequest(
            task="fix add", job_id="job-1", repair_branch="fix/job-1",
            commit_message="fix add",
        )

    def _agent(self, model=None, session=None, reviewer=None, telemetry=None):
        return CodingAgent(
            model or PlanningModel(), session or RecordingSession(edits={"app.py": _FIXED}),
            reviewer or PlanningModel(), repository=self.root,
            telemetry_sink=telemetry,
        )

    def test_interrupted_execution_resumes_from_exact_checkout_through_verified_commit(self):
        class InterruptedAfterEdit(RecordingSession):
            def run_session(self, request, briefing):
                self.calls.append((request, briefing))
                (Path(request.worktree) / "app.py").write_text(_FIXED, encoding="utf-8")
                raise CodingError("session_interrupted", reason_code="session_stalled")

        first = self._agent(session=InterruptedAfterEdit()).run(self.request)
        self.assertEqual(first.status, "interrupted")
        self.assertTrue(first.diff_preserved)
        self.assertIsNone(first.commit)
        self.assertEqual(json.loads(first.checkpoint)["stage"], "execution")
        original = CapabilityAttempt(
            CapabilityCall("job-1", "run_coding_task", {
                "task": "fix add", "repair_branch": "fix/job-1",
                "commit_message": "fix add",
            }),
            CapabilityAttemptDisposition.EXECUTED, True,
            CapabilityResult(
                "job-1", "run_coding_task", CapabilityResultState.PARTIAL,
                first.as_values(), durable_values=first.durable_values(),
            ),
        )
        goal = GoalState("goal", Objective("turn:t", "repair"),
                         (SuccessCriterion("c", "fixed"),), attempts=(original,))
        durable_path = self.root.parent / "interrupted-goal.sqlite3"
        stored = SQLiteGoalStore(durable_path)
        stored.create(goal, "conversation", NOW + timedelta(days=1))
        stored.close()
        reopened = SQLiteGoalStore(durable_path)
        self.addCleanup(reopened.close)
        goal = reopened.load("goal").state
        resumed_session = RecordingSession(edits={"app.py": _FIXED})
        agent = self._agent(session=resumed_session)
        result = build_coding_executors(
            agent.run, lambda: "job-2", lambda: goal,
        )["run_coding_task"]({"resume_job_id": "job-1"})
        self.assertEqual(result.state, CapabilityResultState.SUCCEEDED)
        self.assertTrue(result.values["commit_sha"])
        self.assertTrue(result.values["all_required_verification_passed"])
        self.assertEqual(len(resumed_session.calls), 1)

    def test_interrupted_review_correction_resumes_execution_before_review_and_commit(self):
        class CorrectionInterrupted(RecordingSession):
            def run_session(self, request, briefing):
                if self.calls:
                    self.calls.append((request, briefing))
                    (Path(request.worktree) / "app.py").write_text(
                        _FIXED + "# partial correction\n", encoding="utf-8"
                    )
                    raise CodingError("session_interrupted", reason_code="session_stalled")
                return super().run_session(request, briefing)

        findings = {"findings": [{
            "severity": "high", "title": "finish correction",
            "evidence": "partial result", "correction": "finish the change",
        }]}
        first = self._agent(
            session=CorrectionInterrupted(edits={"app.py": _FIXED}),
            reviewer=PlanningModel(reviews=[findings]),
        ).run(self.request)
        self.assertEqual(first.status, "interrupted")
        checkpoint = json.loads(first.checkpoint)
        self.assertEqual(checkpoint["stage"], "execution")
        self.assertTrue(checkpoint["review_findings"])
        self.assertIsNone(first.commit)
        resumed_session = RecordingSession(edits={"app.py": _FIXED})
        second = self._agent(session=resumed_session).run(replace(
            self.request, job_id="job-2", resume_checkpoint=checkpoint,
        ))
        self.assertEqual(second.status, "succeeded")
        self.assertIsNotNone(second.commit)
        self.assertIn("finish correction", resumed_session.calls[0][1])
        self.assertTrue(second.verification.all_required_passed)

    def _cancel_at(self, agent: CodingAgent, started: threading.Event):
        result = []
        worker = threading.Thread(target=lambda: result.append(agent.run(self.request)))
        worker.start()
        self.assertTrue(started.wait(5), "stage did not start")
        self.assertTrue(agent.cancel("job-1"))
        worker.join(8)
        self.assertFalse(worker.is_alive(), "cancel did not stop the child")
        self.assertEqual(result[0].status, "cancelled")
        self.assertEqual(result[0].diagnostics["cancelled_stage"],
                         json.loads(result[0].checkpoint)["stage"])
        self.assertEqual(json.loads(result[0].checkpoint)["branch"], "fix/job-1")
        self.assertEqual(subprocess.check_output(
            ["git", "branch", "--show-current"], cwd=self.root, text=True).strip(),
            "fix/job-1")
        return result[0]

    def test_cancel_during_planning(self):
        started = threading.Event()
        outcome = self._cancel_at(
            self._agent(model=BlockingModel("alx_coding_plan", started)), started,
        )
        self.assertFalse(outcome.files_changed)
        self.assertEqual(json.loads(outcome.checkpoint)["stage"], "planning")

    def test_cancel_during_execution_preserves_modified_file(self):
        started = threading.Event()
        outcome = self._cancel_at(self._agent(session=BlockingSession(started)), started)
        self.assertIn("app.py", outcome.files_changed)
        self.assertEqual((self.root / "app.py").read_text(), _FIXED)
        self.assertIn("app.py", json.loads(outcome.checkpoint)["files"])
        self.assertTrue(outcome.diff_preserved)

    def test_cancel_during_review_and_resume_only_review(self):
        started = threading.Event()
        session = RecordingSession(edits={"app.py": _FIXED})
        first = self._cancel_at(
            self._agent(session=session,
                        reviewer=BlockingModel("alx_coding_local_review", started)), started,
        )
        checkpoint = json.loads(first.checkpoint)
        self.assertEqual(checkpoint["stage"], "review")
        second = self._agent(session=session).run(replace(
            self.request, job_id="job-2", resume_checkpoint=checkpoint,
        ))
        self.assertEqual(second.status, "succeeded")
        self.assertEqual(len(session.calls), 1)
        self.assertIsNotNone(second.commit)

    def test_cancel_during_tests_and_resume_only_tests(self):
        started = threading.Event()
        session = RecordingSession(edits={"app.py": _FIXED})
        agent = self._agent(session=session)
        original = coding_agent_module.run_permitted_command

        def blocked(*args, **kwargs):
            _wait_in_child(started)
            return original(*args, **kwargs)

        with patch.object(coding_agent_module, "run_permitted_command", blocked):
            first = self._cancel_at(agent, started)
        self.assertEqual(json.loads(first.checkpoint)["stage"], "test")
        second = self._agent(session=session).run(replace(
            self.request, job_id="job-2", resume_checkpoint=json.loads(first.checkpoint),
        ))
        self.assertEqual(second.status, "succeeded")
        self.assertEqual(len(session.calls), 1)

    def test_failed_correction_after_review_retry_spends_implementation_allowance(self):
        class CorrectionFails(RecordingSession):
            def run_session(self, request, briefing):
                if self.calls:
                    raise CodingError("session_failed", reason_code="correction_failed")
                return super().run_session(request, briefing)

        reviewer = PlanningModel(reviews=[
            {"findings": "invalid schema"},
            {"findings": [{"severity": "high", "title": "repair incomplete",
                           "evidence": "adjacent path remains wrong",
                           "correction": "finish the adjacent path"}]},
        ])
        outcome = self._agent(
            session=CorrectionFails(edits={"app.py": _FIXED}), reviewer=reviewer,
        ).run(self.request)
        self.assertEqual(outcome.status, "failed")
        self.assertIn("session_failed", outcome.unresolved_issues)
        self.assertEqual(outcome.review_classification, "infrastructure")
        failure = {"code": "session_failed", **outcome.diagnostics,
                   "review_classification": outcome.review_classification}
        from alx.contracts import CapabilityAttempt, CapabilityAttemptDisposition, CapabilityResult
        attempt = CapabilityAttempt(
            CapabilityCall("job-1", "run_coding_task", {"task": "fix add"}),
            CapabilityAttemptDisposition.EXECUTED, True,
            CapabilityResult("job-1", "run_coding_task", CapabilityResultState.FAILED,
                             failure=failure),
        )
        goal = GoalState("goal", Objective("turn:t", "repair"),
                         (SuccessCriterion("c", "fixed"),), attempts=(attempt,))
        self.assertEqual(CoreAgent._failed_coding_executions(goal), 1)

    def test_commit_failure_after_passing_tests_resumes_commit_only(self):
        session = RecordingSession(edits={"app.py": _FIXED})
        transitions = []
        agent = self._agent(session=session, telemetry=lambda item: transitions.append(item.transition))
        with patch.object(coding_agent_module, "commit_job_changes",
                          side_effect=CodingError("git_refused", reason_code="commit_failed")):
            first = agent.run(self.request)
        self.assertEqual(first.status, "failed")
        self.assertTrue(first.verification.all_required_passed)
        self.assertIn("TEST completed", transitions)
        self.assertIn("COMMIT started", transitions)
        self.assertEqual(json.loads(first.checkpoint)["stage"], "commit")
        second = self._agent(session=session).run(replace(
            self.request, job_id="job-2", resume_checkpoint=json.loads(first.checkpoint),
        ))
        self.assertEqual(second.status, "succeeded")
        self.assertEqual(len(session.calls), 1)
        self.assertIsNotNone(second.commit)

    def test_post_commit_readback_failure_verifies_existing_commit_without_recommitting(self):
        session = RecordingSession(edits={"app.py": _FIXED})
        original = coding_agent_module.commit_job_changes

        def committed_then_unverified(*args, **kwargs):
            created = original(*args, **kwargs)
            raise CodingError("git_unavailable", reason_code="commit_created_but_unverified",
                              commit_sha=created.commit_sha)

        with patch.object(coding_agent_module, "commit_job_changes", committed_then_unverified):
            first = self._agent(session=session).run(self.request)
        self.assertEqual(first.status, "failed")
        self.assertTrue(first.verification.all_required_passed)
        sha = first.diagnostics["commit_sha"]
        checkpoint = json.loads(first.checkpoint)
        self.assertEqual(checkpoint["commit_candidate_sha"], sha)
        second = self._agent(session=session).run(replace(
            self.request, job_id="job-2", resume_checkpoint=checkpoint,
        ))
        self.assertEqual(second.status, "succeeded")
        self.assertEqual(second.commit.commit_sha, sha)
        self.assertEqual(len(session.calls), 1)
        self.assertEqual(subprocess.check_output(
            ["git", "rev-parse", "HEAD"], cwd=self.root, text=True).strip(), sha)

    def test_resume_refuses_changed_checkout_without_touching_it(self):
        reviewer = PlanningModel(reviews=[{"findings": "bad"}])
        first = self._agent(reviewer=reviewer).run(self.request)
        self.assertEqual(first.status, "failed")
        (self.root / "app.py").write_text(_FIXED + "# later edit\n")
        model = PlanningModel()
        session = RecordingSession()
        with self.assertRaises(CodingError) as caught:
            self._agent(model=model, session=session).run(replace(
                self.request, job_id="job-2", resume_checkpoint=json.loads(first.checkpoint),
            ))
        self.assertEqual(caught.exception.details["reason_code"], "resume_state_changed")
        self.assertFalse(model.requests)
        self.assertFalse(session.calls)

    def test_review_schema_failure_is_secret_redacted_and_resumed_through_goal(self):
        secret = "sk-MUST-NOT-SURVIVE"
        subprocess.run(["git", "branch", "fix/job-1"], cwd=self.root, check=True)
        current_goal = GoalState(
            "goal-a", Objective("turn:t", "repair"), (SuccessCriterion("c", "fixed"),),
        )
        current_call = ["job-1"]
        session = RecordingSession(edits={"app.py": _FIXED})
        reviewer = PlanningModel(reviews=[{"findings": secret, "api_key": secret}])
        resumed_reviewer = PlanningModel()

        def dispatch(active_reviewer):
            runtime = build_coding_runtime(
                True, PlanningModel(), lambda: current_call[0],
                session=session, reviewer=active_reviewer, repository=self.root,
                goal_state_source=lambda: current_goal,
            )
            broker = CapabilityBroker(CapabilityRegistry(runtime.definitions),
                                      SafetyGate(runtime.policies), runtime.executors)
            arguments = {
                "task": "fix add", "repair_branch": "fix/job-1",
                "commit_message": "fix add",
            }
            if current_call[0] == "job-1":
                arguments.update({
                    "context": "the operation subtracts instead of adding",
                    "acceptance_criteria": ["addition returns the expected sum"],
                    "test_guidance": "run the app test",
                    "step_budget": 7,
                })
            else:
                arguments = {"resume_job_id": "job-1"}
            durable = {
                key: arguments[key] for key in runtime.definitions[0].durable_input_fields
                if key in arguments
            }
            return broker.dispatch(
                CapabilityCall(current_call[0], "run_coding_task", arguments,
                               durable_arguments=durable),
                AuthorityContext("friedl", runtime.permissions, NOW),
            )

        first = dispatch(reviewer)
        self.assertEqual(first.result.state, CapabilityResultState.FAILED)
        self.assertEqual(first.result.failure["review_classification"], "infrastructure")
        self.assertEqual(first.result.values["branch"], "fix/job-1-2")
        self.assertNotIn(secret, str(first.result.values))
        self.assertNotIn(secret, str(first.result.durable_values))
        current_goal = replace(current_goal, attempts=(first,))
        goal_path = self.root.parent / "coding-goal.sqlite3"
        store = SQLiteGoalStore(goal_path)
        store.create(current_goal, "conversation", NOW + timedelta(days=1))
        store.close()
        reopened = SQLiteGoalStore(goal_path)
        self.addCleanup(reopened.close)
        current_goal = reopened.load("goal-a").state
        self.assertEqual(current_goal.attempts[0].call.arguments["step_budget"], 7)
        self.assertEqual(current_goal.attempts[0].call.arguments["context"],
                         "the operation subtracts instead of adding")
        current_call[0] = "job-2"
        owned_goal = current_goal
        current_goal = GoalState(
            "goal-b", Objective("turn:other", "other repair"),
            (SuccessCriterion("c", "fixed"),),
        )
        foreign = dispatch(PlanningModel())
        self.assertEqual(foreign.result.failure["reason_code"],
                         "resume_ownership_unproven")
        current_goal = owned_goal
        second = dispatch(resumed_reviewer)
        self.assertEqual(second.result.state, CapabilityResultState.SUCCEEDED,
                         second.result.failure)
        self.assertEqual(len(session.calls), 1)
        self.assertTrue(second.result.values["commit_sha"])
        review_material = json.loads(resumed_reviewer.requests[0].messages[1].content)
        self.assertEqual(review_material["root_cause_context"],
                         "the operation subtracts instead of adding")
        self.assertEqual(review_material["acceptance_criteria"],
                         ["addition returns the expected sum"])
        self.assertEqual(review_material["test_guidance"], "run the app test")

    def test_adapter_coding_error_cannot_spoof_schema_evidence(self):
        secret = "sk-MUST-NOT-SURVIVE"

        class UntrustedReviewer(PlanningModel):
            def complete(self, request):
                raise CodingError("review_failed", reason_code="review_schema_invalid",
                                  error_message=secret, raw_excerpt=secret)

        outcome = self._agent(reviewer=UntrustedReviewer()).run(self.request)
        self.assertEqual(outcome.status, "failed")
        self.assertEqual(outcome.review_classification, "infrastructure")
        self.assertEqual(outcome.review_attempts[0].reason, "provider_failed")
        self.assertNotIn(secret, str(outcome.as_values()))

    def test_test_transport_timeout_retries_test_only(self):
        session = RecordingSession(edits={"app.py": _FIXED})
        original = coding_agent_module.run_permitted_command
        calls = []

        def one_timeout(*args, **kwargs):
            calls.append(tuple(args[0]))
            if len(calls) == 1:
                return CodingCommandRecord(tuple(args[0]), -1, "", "", True, True)
            return original(*args, **kwargs)

        with patch.object(coding_agent_module, "run_permitted_command", one_timeout):
            outcome = self._agent(session=session).run(self.request)
        self.assertEqual(outcome.status, "succeeded")
        self.assertEqual(len(session.calls), 1)
        self.assertEqual(calls[0], calls[1])
        self.assertTrue(outcome.verification.all_required_passed)

    def test_post_test_success_has_explicit_commit_and_no_failed_transition(self):
        transitions = []
        outcome = self._agent(telemetry=lambda item: transitions.append(item.transition)).run(
            self.request
        )
        self.assertEqual(outcome.status, "succeeded")
        self.assertIn("TEST completed", transitions)
        self.assertIn("COMMIT completed", transitions)
        self.assertNotIn("FAILED", transitions)

    def test_stop_coding_job_stops_only_the_named_running_job(self):
        """AL/X stops a job herself; it replaced the console's Stop button."""
        from alx.tools.coding import STOP_CODING_JOB, build_coding_executors

        asked = []
        executors = build_coding_executors(
            lambda request: None, lambda: "call-stop",
            stop_job=lambda job_id: asked.append(job_id) or job_id in (None, "job-1"),
        )
        stop = executors[STOP_CODING_JOB]
        self.assertTrue(stop({"job_id": "job-1"}).values["stopped"])
        self.assertFalse(stop({"job_id": "job-2"}).values["stopped"])
        self.assertTrue(stop({}).values["stopped"])
        self.assertEqual(asked, ["job-1", "job-2", None])
        refused = stop({"job_id": "../etc"})
        self.assertEqual(refused.failure["code"], "arguments_unusable")
        self.assertEqual(asked, ["job-1", "job-2", None])

    def test_stopping_is_registered_under_the_coding_permission(self):
        from alx.bootstrap.coding import CODING_EXECUTE_PERMISSION
        from alx.tools.coding import STOP_CODING_JOB

        runtime = build_coding_runtime(
            True, None, lambda: "call-1", agent=self._agent(),
            repository=self.root,
        )
        self.assertIn(STOP_CODING_JOB, runtime.executors)
        self.assertIn(STOP_CODING_JOB, {item.capability_id for item in runtime.definitions})
        policy = runtime.policies[STOP_CODING_JOB]
        self.assertEqual(policy.permission_references, frozenset({CODING_EXECUTE_PERMISSION}))
        self.assertFalse(policy.approval_required)
        # Nothing is running, so there is nothing to stop.
        self.assertFalse(runtime.executors[STOP_CODING_JOB]({}).values["stopped"])
