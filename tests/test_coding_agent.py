"""CA-MVP: Core-delegated coding jobs, bounded and fail-closed, under D-028."""

from __future__ import annotations

import ast
import hashlib
import json
import os
import subprocess
import sys
import tempfile
import unittest
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest.mock import patch

REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPOSITORY_ROOT / "src"))

from alx.bootstrap.coding import (  # noqa: E402
    CODING_EXECUTE_PERMISSION,
    build_coding_runtime,
)
from alx.capabilities import CapabilityBroker, CapabilityRegistry  # noqa: E402
from alx.contracts import (  # noqa: E402
    AgentDecision,
    CapabilityAttemptDisposition,
    CapabilityCall,
    CapabilityResultState,
    ConversationOrigin,
    ConversationSnapshot,
    ConversationTurn,
    Evidence,
    GoalMutationKind,
    GoalProposal,
    GoalState,
    GoalStatus,
    ModelCompletion,
    Objective,
    SuccessCriterion,
)
from alx.core import CoreAgent, CoreState  # noqa: E402
from alx.goals import SQLiteGoalStore  # noqa: E402

from alx.contracts.coding import (  # noqa: E402
    CODING_FAILURES,
    DEFAULT_VERIFICATION_COMMAND_SECONDS,
    MAX_LOCAL_REVIEW_CYCLES,
    MAX_REVIEW_INFRASTRUCTURE_ATTEMPTS,
    MAX_REVIEW_RAW_EXCERPT_CHARACTERS,
    MAX_STEP_BUDGET,
    MAX_TASK_CHARACTERS,
    CodingCommandRecord,
    CodingError,
    CodingRequest,
    CodingSessionResult,
)
from alx.contracts.coding_verification import (  # noqa: E402
    required_verification,
)
from alx.providers import coding_containment  # noqa: E402
from alx.providers.coding_process import command_permitted  # noqa: E402
from alx.providers.coding_session import (  # noqa: E402
    NATIVE_TOOLS,
    WITHHELD_TOOLS,
    GrokCodingSession,
)
from alx.providers.coding_workspace import CodingWorkspace  # noqa: E402
from alx.providers.errors import ProviderError  # noqa: E402
from alx.providers import coding_agent as coding_agent_module  # noqa: E402
from alx.safety import AuthorityContext, SafetyGate  # noqa: E402
from alx.tools.coding import (  # noqa: E402
    DEFINITION,
    RUN_CODING_TASK,
    _OUTCOME_ISSUE_CODES,
)


NOW = datetime(2026, 9, 9, tzinfo=UTC)
RETENTION = datetime(2027, 9, 9, tzinfo=UTC)
PRODUCTION_ROOT = REPOSITORY_ROOT / "src" / "alx"
CODING_PROCESS = PRODUCTION_ROOT / "providers" / "coding_process.py"


# The branch every fixture repository starts on. Named here so a test that
# asserts where a job began does not have to guess what git called it.
FIXTURE_BRANCH = "main"


def _git(root: Path, *args: str) -> None:
    subprocess.run(
        ["git", *args],
        cwd=root,
        check=True,
        capture_output=True,
        text=True,
    )


def _worktree(parent: Path, name: str = "job") -> Path:
    root = parent / name
    root.mkdir()
    (root / "app.py").write_text("def add(a, b):\n    return a - b\n", encoding="utf-8")
    (root / "test_app.py").write_text(
        "import unittest\nfrom app import add\n\n\n"
        "class AddTests(unittest.TestCase):\n"
        "    def test_add(self):\n"
        "        self.assertEqual(add(1, 2), 3)\n",
        encoding="utf-8",
    )
    # The initial branch is named explicitly rather than inherited. A bare
    # `git init` takes the host's `init.defaultBranch`, which is `main` on this
    # workstation and `master` in CI, so tests that name the starting branch
    # passed locally and failed there. Nothing about D-031 depends on the name;
    # what the fixtures need is for it not to vary by machine.
    _git(root, "init", "-q", "-b", FIXTURE_BRANCH)
    _git(root, "config", "user.email", "test@example.invalid")
    _git(root, "config", "user.name", "test")
    _git(root, "add", ".")
    _git(root, "commit", "-m", "fixture")
    return root


def _plan(**changes) -> dict:
    values = {
        "problem_understanding": "Inspect the assigned task before making a bounded change.",
        "hypotheses": ["the relevant implementation may be incorrect"],
        "inspection_targets": [],
        "intended_changes": ["make only the task-scoped correction"],
        "verification": ["run relevant permitted tests"],
        "risks_constraints": ["bounded worktree and operation contract apply"],
        "more_context_required": False,
    }
    values.update(changes)
    return values


class PlanningModel:
    """Answers the planning turn only. Any other call is a defect."""

    def __init__(self, plan: dict | None = None, error: Exception | None = None,
                 reviews: list[dict] | None = None) -> None:
        self._plan = plan if plan is not None else _plan()
        self._error = error
        self._reviews = list(reviews or [{"findings": []}])
        self.requests = []

    def complete(self, request):
        self.requests.append(request)
        if request.output_schema_name == "alx_coding_local_review":
            if not self._reviews:
                raise AssertionError("the reviewer was called with no scripted review")
            # One scripted review is the reviewer's standing answer. The
            # review stage retries infrastructure failures against that same
            # answer; a longer script still plays in order, one call at a time.
            if len(self._reviews) == 1:
                review = self._reviews[0]
            else:
                review = self._reviews.pop(0)
            return ModelCompletion("xai", "scripted", review)
        if request.output_schema_name != "alx_coding_plan":
            raise AssertionError(
                "the native execution model must not ask the model for steps"
            )
        if self._error is not None:
            raise self._error
        return ModelCompletion("xai", "scripted", self._plan)


class RecordingSession:
    """A native session stand-in that edits the worktree the way Grok would."""

    def __init__(
        self,
        *,
        edits: dict[str, str] | None = None,
        completed: bool = True,
        report: str = "made the bounded change",
        failure_code: str = "",
        raises: CodingError | None = None,
        turns: int = 7,
    ) -> None:
        self.edits = edits or {}
        self.completed = completed
        self.report = report
        self.failure_code = failure_code
        self.raises = raises
        self.turns = turns
        self.calls: list[tuple[CodingRequest, str]] = []

    def run_session(self, request, briefing):
        self.calls.append((request, briefing))
        if self.raises is not None:
            raise self.raises
        root = Path(request.worktree)
        for name, content in self.edits.items():
            (root / name).write_text(content, encoding="utf-8")
        return CodingSessionResult(
            completed=self.completed,
            report=self.report,
            turns=self.turns,
            failure_code=self.failure_code,
        )


_FIXED = "def add(a, b):\n    return a + b\n"


class NativeExecutionTests(unittest.TestCase):
    """PLAN, then a native session, then AL/X's own verification."""

    def setUp(self) -> None:
        self.directory = tempfile.TemporaryDirectory()
        self.root = Path(self.directory.name)
        self.addCleanup(self.directory.cleanup)

    def _run(self, model, session, reviewer=None, **arguments):
        reviewer = reviewer or PlanningModel()
        activity_sink = arguments.pop("activity_sink", None)
        telemetry_sink = arguments.pop("telemetry_sink", None)
        # The test-only `worktree` keyword names the configured canonical
        # fixture repository; it never reaches the capability schema.
        repository = arguments.pop("worktree", None) or str(_worktree(self.root))
        arguments.setdefault("repair_branch", "fix/call-1")
        arguments.setdefault("commit_message", "complete coding job")
        runtime = build_coding_runtime(
            True, model, lambda: "call-1", session=session, reviewer=reviewer,
            activity_sink=activity_sink, telemetry_sink=telemetry_sink,
            repository=Path(repository),
        )
        self.assertIsNotNone(runtime)
        broker = CapabilityBroker(
            CapabilityRegistry(runtime.definitions),
            SafetyGate(runtime.policies),
            runtime.executors,
        )
        return broker.dispatch(
            CapabilityCall("call-1", RUN_CODING_TASK, arguments),
            AuthorityContext(
                "friedl", frozenset({CODING_EXECUTE_PERMISSION}), NOW
            ),
        )

    def test_plan_completes_before_the_native_session_begins(self) -> None:
        """1. PLAN runs first; the session only ever sees an accepted plan."""
        order: list[str] = []

        class OrderedModel(PlanningModel):
            def complete(self, request):
                order.append(
                    "review" if request.output_schema_name == "alx_coding_local_review"
                    else "plan"
                )
                return super().complete(request)

        class OrderedSession(RecordingSession):
            def run_session(self, request, briefing):
                order.append("session")
                return super().run_session(request, briefing)

        worktree = _worktree(self.root)
        session = OrderedSession(edits={"app.py": _FIXED})
        self._run(
            OrderedModel(), session, reviewer=OrderedModel(), task="fix add",
            worktree=str(worktree),
        )
        self.assertEqual(order, ["plan", "session", "review"])

    def test_native_session_and_reviewer_report_explicit_activity(self) -> None:
        worktree = _worktree(self.root)
        activities: list[str] = []
        self._run(
            PlanningModel(), RecordingSession(edits={"app.py": _FIXED}),
            reviewer=PlanningModel(), activity_sink=activities.append,
            task="fix add", worktree=str(worktree),
        )
        self.assertEqual(activities, ["coding", "reviewing", "reasoning"])

    def test_authoritative_telemetry_reports_real_job_lifecycle(self) -> None:
        worktree = _worktree(self.root)
        telemetry = []
        self._run(
            PlanningModel(), RecordingSession(edits={"app.py": _FIXED}),
            reviewer=PlanningModel(), telemetry_sink=telemetry.append,
            task="fix add", worktree=str(worktree),
        )
        phases = [item.phase for item in telemetry]
        self.assertEqual(phases[0], "plan")
        self.assertIn("execution", phases)
        self.assertIn("review", phases)
        self.assertIn("test", phases)
        self.assertIn("verify", phases)
        self.assertEqual(phases[-1], "complete")
        self.assertTrue(telemetry[-1].terminal)
        self.assertEqual(telemetry[-1].outcome, "succeeded")
        self.assertTrue(any(item.in_flight for item in telemetry if item.phase == "execution"))

    def test_telemetry_names_the_visible_feature_branch(self) -> None:
        worktree = _worktree(self.root)
        telemetry = []
        attempt = self._run(
            PlanningModel(), RecordingSession(edits={"app.py": _FIXED}),
            reviewer=PlanningModel(), telemetry_sink=telemetry.append,
            task="fix add", worktree=str(worktree),
        )
        reported = [item for item in telemetry if item.branch]
        self.assertTrue(reported)
        self.assertEqual(
            {item.branch for item in reported},
            {attempt.result.values["baseline"]["branch"]},
        )
        self.assertTrue(telemetry[-1].terminal)
        self.assertEqual(telemetry[-1].branch, "fix/call-1")

    def test_telemetry_before_branch_creation_names_no_branch(self) -> None:
        telemetry = []
        repository = _worktree(self.root)
        (repository / "dirty.txt").write_text("dirty\n", encoding="utf-8")
        agent = coding_agent_module.CodingAgent(
            PlanningModel(), RecordingSession(), PlanningModel(),
            telemetry_sink=telemetry.append, repository=repository,
        )
        with self.assertRaises(CodingError):
            agent.run(CodingRequest(
                task="fix add", job_id="job-1", repair_branch="fix/job-1",
                commit_message="fix add",
            ))
        self.assertTrue(telemetry)
        self.assertEqual(telemetry[0].branch, "")

    def test_failed_telemetry_delivery_keeps_the_original_elapsed_anchor(self) -> None:
        worktree = _worktree(self.root)
        delivered = []
        attempts = 0
        tick = 0

        def clock():
            nonlocal tick
            value = NOW + timedelta(seconds=tick)
            tick += 1
            return value

        def sink(item):
            nonlocal attempts
            attempts += 1
            if attempts == 1:
                raise RuntimeError("transport unavailable")
            delivered.append(item)

        agent = coding_agent_module.CodingAgent(
            PlanningModel(), RecordingSession(edits={"app.py": _FIXED}),
            PlanningModel(), telemetry_sink=sink, clock=clock,
            repository=worktree,
        )
        agent.run(CodingRequest(
            task="fix add", job_id="job-1", repair_branch="fix/job-1",
            commit_message="fix add",
        ))

        self.assertEqual(delivered[0].phase, "plan")
        self.assertEqual(delivered[0].started_at, NOW)

    def test_a_failing_activity_sink_does_not_destroy_the_outcome(self) -> None:
        """Telemetry is not part of the outcome, so it cannot fail the job.

        The sink is a transport supplied by the caller, and a transport can
        fail. It used to fail into the job: the exception propagated out of
        `run`, whose `finally` clause reports the final state *after* a valid
        outcome has been computed, so a broken status line destroyed a
        finished repair. The tool layer then returned `coding_unavailable`,
        telling Core the job failed while the worktree held the completed work.
        """
        worktree = _worktree(self.root)

        def exploding(activity: str) -> None:
            raise RuntimeError("telemetry transport died")

        attempt = self._run(
            PlanningModel(), RecordingSession(edits={"app.py": _FIXED}),
            reviewer=PlanningModel(), activity_sink=exploding,
            task="fix add", worktree=str(worktree),
        )

        self.assertEqual(attempt.result.state, CapabilityResultState.SUCCEEDED)
        self.assertIn("app.py", attempt.result.values["files_changed"])
        self.assertEqual((worktree / "app.py").read_text(), _FIXED)

    def test_a_failing_activity_sink_is_logged_rather_than_silent(self) -> None:
        """Swallowed is not the same as hidden: the failure is still evidence."""
        worktree = _worktree(self.root)

        def exploding(activity: str) -> None:
            raise RuntimeError("telemetry transport died")

        with self.assertLogs(
            "alx.providers.coding_agent", level="WARNING"
        ) as captured:
            self._run(
                PlanningModel(), RecordingSession(edits={"app.py": _FIXED}),
                reviewer=PlanningModel(), activity_sink=exploding,
                task="fix add", worktree=str(worktree),
            )

        self.assertTrue(
            any("activity sink failed" in line for line in captured.output)
        )

    def test_correction_cycle_reports_reviewing_coding_reviewing(self) -> None:
        worktree = _worktree(self.root)
        activities: list[str] = []
        reviewer = PlanningModel(reviews=[
            {"findings": [{
                "severity": "high", "title": "repair needed",
                "evidence": "candidate is incomplete", "correction": "finish it",
            }]},
            {"findings": []},
        ])
        class CorrectingSession(RecordingSession):
            def run_session(self, request, briefing):
                result = super().run_session(request, briefing)
                if len(self.calls) > 1:
                    path = Path(request.worktree) / "app.py"
                    path.write_text(path.read_text(encoding="utf-8") + "# corrected\n", encoding="utf-8")
                return result

        self._run(
            PlanningModel(), CorrectingSession(edits={"app.py": _FIXED}),
            reviewer=reviewer, activity_sink=activities.append,
            task="fix add", worktree=str(worktree),
        )
        self.assertEqual(
            activities,
            ["coding", "reviewing", "coding", "reviewing", "reasoning"],
        )

    def test_session_failure_restores_reasoning_activity_without_another_model_call(self) -> None:
        worktree = _worktree(self.root)
        activities: list[str] = []
        planner = PlanningModel()
        reviewer = PlanningModel()
        self._run(
            planner,
            RecordingSession(raises=CodingError("session_failed", reason_code="session_timeout")),
            reviewer=reviewer, activity_sink=activities.append,
            task="fix add", worktree=str(worktree),
        )
        self.assertEqual(activities, ["coding", "reasoning"])
        self.assertEqual(len(planner.requests), 1)
        self.assertEqual(reviewer.requests, [])

    def test_reviewer_failure_restores_reasoning_activity(self) -> None:
        worktree = _worktree(self.root)
        activities: list[str] = []
        self._run(
            PlanningModel(), RecordingSession(edits={"app.py": _FIXED}),
            reviewer=PlanningModel(reviews=[{"findings": "invalid"}]),
            activity_sink=activities.append, task="fix add", worktree=str(worktree),
        )
        self.assertEqual(activities[-1], "reasoning")

    def test_transient_activity_sink_failure_retries_terminal_reasoning(self) -> None:
        worktree = _worktree(self.root)
        activities: list[str] = []
        reasoning_attempts = 0

        def sink(activity: str) -> None:
            nonlocal reasoning_attempts
            if activity == "reasoning":
                reasoning_attempts += 1
                if reasoning_attempts == 1:
                    raise RuntimeError("transient sink failure")
            activities.append(activity)

        attempt = self._run(
            PlanningModel(), RecordingSession(edits={"app.py": _FIXED}),
            reviewer=PlanningModel(), activity_sink=sink,
            task="fix add", worktree=str(worktree),
        )
        self.assertEqual(reasoning_attempts, 2)
        self.assertEqual(activities, ["coding", "reviewing", "reasoning"])
        self.assertEqual(activities[-1], "reasoning")
        self.assertEqual(attempt.result.state, CapabilityResultState.SUCCEEDED)

    def test_a_failed_plan_never_reaches_the_session(self) -> None:
        model = PlanningModel(plan=_plan(problem_understanding="   "))
        session = RecordingSession(edits={"app.py": _FIXED})
        worktree = _worktree(self.root)
        attempt = self._run(
            model, session, task="fix add", worktree=str(worktree)
        )
        self.assertEqual(attempt.result.state, CapabilityResultState.FAILED)
        self.assertEqual(session.calls, [])
        self.assertEqual(attempt.result.failure["code"], "planning_failed")
        self.assertIs(attempt.result.failure["implementation_reached"], False)

    def test_planner_paths_are_repository_relative_and_inside_absolute_is_normalized(self) -> None:
        worktree = _worktree(self.root)
        model = PlanningModel(plan=_plan(inspection_targets=[
            "app.py", str(worktree / "test_app.py"),
        ]))
        agent = coding_agent_module.CodingAgent(
            model, RecordingSession(), PlanningModel(), repository=worktree
        )
        plan = agent._plan(
            CodingRequest(task="inspect", job_id="job-1"),
            CodingWorkspace(str(worktree)),
            [],
        )
        self.assertEqual(plan["inspection_targets"], ["app.py", "test_app.py"])
        payload = json.loads(model.requests[0].messages[-1].content)
        self.assertNotIn("worktree", payload)
        self.assertIn("repository_entries", payload)
        self.assertIn(
            "repository-relative path", model.requests[0].messages[0].content
        )

    def test_planner_rejects_absolute_paths_outside_the_repository(self) -> None:
        worktree = _worktree(self.root)
        workspace = CodingWorkspace(str(worktree))
        for target in (
            "/usr/bin/claude",
            str(Path.home() / "private.py"),
            "/tmp/outside.py",
            "/var/arbitrary/outside.py",
        ):
            with self.subTest(target=target), self.assertRaises(CodingError) as caught:
                workspace.validate_inspection_target(target)
            self.assertEqual(caught.exception.code, "path_outside_repository")
            self.assertEqual(caught.exception.details["path"], target)

    def test_planner_rejects_nul_paths_as_repository_path_errors(self) -> None:
        worktree = _worktree(self.root)
        target = str(worktree / "invalid\x00target.py")
        workspace = CodingWorkspace(str(worktree))

        with self.assertRaises(CodingError) as caught:
            workspace.validate_inspection_target(target)

        self.assertEqual(caught.exception.code, "path_outside_repository")
        self.assertEqual(caught.exception.details["path"], target)

        model = PlanningModel(plan=_plan(inspection_targets=[target]))
        session = RecordingSession(edits={"app.py": _FIXED})
        attempt = self._run(model, session, task="fix add", worktree=str(worktree))
        self.assertEqual(len(model.requests), 3)
        self.assertEqual(session.calls, [])
        self.assertEqual(attempt.result.failure["code"], "planning_failed")
        self.assertEqual(
            attempt.result.failure["reason_code"], "path_outside_repository"
        )
        self.assertNotIn("\x00", attempt.result.failure["inspection_target"])

    def test_rejected_target_reaches_retry_feedback_and_durable_failure(self) -> None:
        target = "/tmp/outside.py"
        model = PlanningModel(plan=_plan(inspection_targets=[target]))
        session = RecordingSession(edits={"app.py": _FIXED})
        worktree = _worktree(self.root)
        attempt = self._run(model, session, task="fix add", worktree=str(worktree))

        self.assertEqual(len(model.requests), 3)
        self.assertEqual(session.calls, [])
        self.assertEqual(attempt.result.failure["code"], "planning_failed")
        self.assertEqual(attempt.result.failure["reason_code"], "path_outside_repository")
        self.assertEqual(attempt.result.failure["inspection_target"], target)
        second_payload = json.loads(model.requests[1].messages[-1].content)
        self.assertIn(target, second_payload["planning_feedback"][0])

    def test_production_no_longer_emits_the_worktree_path_error_name(self) -> None:
        for path in PRODUCTION_ROOT.rglob("*.py"):
            self.assertNotIn(
                "path_outside_worktree", path.read_text(encoding="utf-8"),
                path.as_posix(),
            )

    def test_local_reviewer_catches_an_adjacent_unfixed_path_and_rechecks(self) -> None:
        """A plausible one-line repair is not accepted while its twin is wrong."""
        worktree = _worktree(self.root)
        (worktree / "parallel.py").write_text(
            "def add(a, b):\n    return a - b\n", encoding="utf-8"
        )
        _git(worktree, "add", "parallel.py")
        _git(worktree, "commit", "-m", "add parallel helper")

        class CorrectingSession(RecordingSession):
            def run_session(self, request, briefing):
                self.calls.append((request, briefing))
                root = Path(request.worktree)
                if len(self.calls) == 1:
                    (root / "app.py").write_text(_FIXED, encoding="utf-8")
                else:
                    (root / "parallel.py").write_text(_FIXED, encoding="utf-8")
                return CodingSessionResult(True, "corrected", turns=2)

        model = PlanningModel(plan=_plan(
            inspection_targets=["app.py", "parallel.py"]
        ))
        reviewer = PlanningModel(reviews=[
            {"findings": [{
                "severity": "high", "title": "parallel helper remains wrong",
                "evidence": "parallel.py still subtracts", "correction": "fix parallel.py",
            }]},
            {"findings": []},
        ])
        session = CorrectingSession()
        attempt = self._run(
            model, session, reviewer=reviewer, task="fix both add helpers",
            worktree=str(worktree),
        )

        self.assertEqual(attempt.result.state, CapabilityResultState.SUCCEEDED)
        self.assertEqual(len(session.calls), 2)
        self.assertIn("Local reviewer findings", session.calls[1][1])
        first_review = json.loads(reviewer.requests[0].messages[-1].content)
        self.assertIn("parallel.py", first_review["changed_file_context"])
        self.assertIn("parallel.py", attempt.result.values["files_changed"])
        self.assertIn(
            "parallel.py", attempt.result.values["commit"]["committed_files"]
        )
        self.assertEqual(
            [request.output_schema_name for request in model.requests],
            ["alx_coding_plan"],
        )
        self.assertEqual(
            [request.output_schema_name for request in reviewer.requests],
            ["alx_coding_local_review", "alx_coding_local_review"],
        )

    def test_local_reviewer_failure_never_accepts_the_job(self) -> None:
        """An unusable local review fails as review_failed, not task_failed."""
        worktree = _worktree(self.root)
        reviewer = PlanningModel(reviews=[{"findings": "not-a-list"}])
        attempt = self._run(
            PlanningModel(), RecordingSession(edits={"app.py": _FIXED}),
            reviewer=reviewer, task="fix add", worktree=str(worktree),
        )
        self.assertEqual(attempt.result.state, CapabilityResultState.FAILED)
        self.assertEqual(attempt.result.failure["code"], "review_failed")
        self.assertIn("review_failed", attempt.result.values["unresolved_issues"])
        self.assertNotEqual(attempt.result.failure["code"], "task_failed")

    def test_unavailable_local_reviewer_fails_closed(self) -> None:
        """An unavailable local reviewer fails as review_failed, not task_failed."""
        class UnavailableReviewer(PlanningModel):
            def complete(self, request):
                if request.output_schema_name == "alx_coding_local_review":
                    raise ProviderError("local", "unavailable")
                return super().complete(request)

        worktree = _worktree(self.root)
        planner = PlanningModel()
        attempt = self._run(
            planner, RecordingSession(edits={"app.py": _FIXED}),
            reviewer=UnavailableReviewer(),
            task="fix add", worktree=str(worktree),
        )
        self.assertEqual(attempt.result.state, CapabilityResultState.FAILED)
        self.assertEqual(attempt.result.failure["code"], "review_failed")
        self.assertIn("review_failed", attempt.result.values["unresolved_issues"])
        self.assertNotEqual(attempt.result.failure["code"], "task_failed")
        self.assertEqual(
            [item.output_schema_name for item in planner.requests],
            ["alx_coding_plan"],
        )

    def test_local_review_failure_retains_only_sanitised_diagnostics(self) -> None:
        """Every unusable reviewer category stays diagnosable without its payload."""
        cases = (
            ("cli_failed", {"exit_status": 17, "stdout_characters": 41, "stderr_characters": 83}, {}),
            ("reasoning_timeout", {"stdout_characters": 0, "stderr_characters": 0}, {"timed_out": True}),
            ("structured_output_missing", {"stdout_characters": 0, "stderr_characters": 0}, {"parse_category": "empty_response"}),
            ("structured_output_not_object", {"stdout_characters": 9, "stderr_characters": 0}, {"parse_category": "malformed_structured_output"}),
            ("response_invalid", {"stdout_characters": 12, "stderr_characters": 0}, {"parse_category": "parser_failure"}),
        )

        class FailingReviewer(PlanningModel):
            _model = "reviewer-model"

            def __init__(self, reason, details):
                super().__init__()
                self.reason = reason
                self.details = details

            def complete(self, request):
                if request.output_schema_name == "alx_coding_local_review":
                    raise ProviderError(
                        "codex_subscription", self.reason,
                        {**self.details, "access_token": "must-not-survive", "stderr": "cookie=must-not-survive"},
                    )
                return super().complete(request)

        for index, (reason, details, expected) in enumerate(cases):
            with self.subTest(reason=reason):
                attempt = self._run(
                    PlanningModel(), RecordingSession(edits={"app.py": _FIXED}),
                    reviewer=FailingReviewer(reason, details), task="fix add",
                    worktree=str(_worktree(self.root, f"diagnostic-{index}")),
                )
                self.assertEqual(attempt.result.failure["code"], "review_failed")
                failure = attempt.result.failure
                self.assertEqual(failure["phase"], "local_review")
                self.assertEqual(failure["provider"], "codex_subscription")
                self.assertEqual(failure["model"], "reviewer-model")
                self.assertEqual(failure["reason_code"], reason)
                for key, value in details.items():
                    self.assertEqual(failure[key], value)
                self.assertEqual({key: failure[key] for key in expected}, expected)
                persisted = json.dumps(dict(failure))
                self.assertNotIn("must-not-survive", persisted)
                self.assertNotIn("access_token", failure)
                self.assertNotIn("stderr", failure)

    def test_schema_invalid_local_review_retains_schema_category(self) -> None:
        worktree = _worktree(self.root, "schema-invalid")
        attempt = self._run(
            PlanningModel(), RecordingSession(edits={"app.py": _FIXED}),
            reviewer=PlanningModel(reviews=[{"findings": "not-a-list"}]),
            task="fix add", worktree=str(worktree),
        )
        self.assertEqual(attempt.result.failure["code"], "review_failed")
        self.assertEqual(attempt.result.failure["reason_code"], "review_schema_invalid")
        self.assertEqual(attempt.result.failure["parse_category"], "schema_invalid")

    def test_review_schema_invalid_retries_then_commits(self) -> None:
        """A schema failure retries the review only; the next valid one commits."""
        worktree = _worktree(self.root, "schema-then-valid")
        model = PlanningModel()
        reviewer = PlanningModel(reviews=[
            {"findings": "not-a-list"},
            {"findings": []},
        ])
        session = RecordingSession(edits={"app.py": _FIXED})
        attempt = self._run(
            model, session, reviewer=reviewer, task="fix add",
            worktree=str(worktree),
        )
        self.assertEqual(attempt.result.state, CapabilityResultState.SUCCEEDED)
        self.assertEqual(len(session.calls), 1)
        self.assertEqual(
            [item.output_schema_name for item in model.requests],
            ["alx_coding_plan"],
        )
        self.assertEqual(
            [item.output_schema_name for item in reviewer.requests],
            ["alx_coding_local_review", "alx_coding_local_review"],
        )
        values = attempt.result.values
        self.assertTrue(values["tests_run"])
        self.assertTrue(values["all_required_verification_passed"])
        self.assertTrue(values["commit_sha"])
        self.assertEqual(values["branch"], "fix/call-1")
        self.assertNotIn("diff_preserved", values)
        self.assertNotIn("uncommitted", values)
        self.assertEqual(values["review_classification"], "infrastructure")
        self.assertIsNone(attempt.result.failure)
        attempts = values["review_attempts"]
        self.assertEqual(len(attempts), 1)
        self.assertEqual(attempts[0]["attempt"], 1)
        self.assertEqual(attempts[0]["reason"], "review_schema_invalid")
        self.assertEqual(attempts[0]["error_message"], "findings is not a list")
        self.assertIn("not-a-list", attempts[0]["raw_excerpt"])
        self.assertLessEqual(
            len(attempts[0]["raw_excerpt"]), MAX_REVIEW_RAW_EXCERPT_CHARACTERS
        )
        self.assertEqual(
            list(attempt.result.durable_values["review_attempts"]),
            list(attempts),
        )
        self.assertEqual(
            attempt.result.durable_values["review_classification"],
            "infrastructure",
        )
        self.assertNotIn("diff_preserved", attempt.result.durable_values)
        log = subprocess.run(
            ["git", "log", "-1", "--pretty=%s"],
            cwd=worktree, check=True, capture_output=True, text=True,
        )
        self.assertEqual(log.stdout.strip(), "complete coding job")

    def test_review_schema_invalid_preserves_diff_when_retries_are_exhausted(self) -> None:
        """Three schema failures leave the branch and the uncommitted diff."""
        worktree = _worktree(self.root, "schema-exhausted")
        huge = "X" * (MAX_REVIEW_RAW_EXCERPT_CHARACTERS + 50)
        model = PlanningModel()
        reviewer = PlanningModel(reviews=[{"findings": huge}])
        session = RecordingSession(edits={"app.py": _FIXED})
        before = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=worktree, check=True, capture_output=True, text=True,
        ).stdout.strip()
        attempt = self._run(
            model, session, reviewer=reviewer, task="fix add",
            worktree=str(worktree),
        )
        self.assertEqual(attempt.result.state, CapabilityResultState.FAILED)
        self.assertEqual(len(session.calls), 1)
        self.assertEqual(
            [item.output_schema_name for item in model.requests],
            ["alx_coding_plan"],
        )
        self.assertEqual(
            len(reviewer.requests), MAX_REVIEW_INFRASTRUCTURE_ATTEMPTS
        )
        self.assertEqual(
            [item.output_schema_name for item in reviewer.requests],
            ["alx_coding_local_review"] * MAX_REVIEW_INFRASTRUCTURE_ATTEMPTS,
        )
        failure = attempt.result.failure
        self.assertEqual(failure["code"], "review_failed")
        self.assertEqual(failure["reason_code"], "review_schema_invalid")
        self.assertEqual(failure["review_classification"], "infrastructure")
        self.assertIs(failure["diff_preserved"], True)
        self.assertIs(failure["uncommitted"], True)
        self.assertEqual(failure["branch"], "fix/call-1")
        values = attempt.result.values
        self.assertEqual(values["review_classification"], "infrastructure")
        self.assertIs(values["diff_preserved"], True)
        self.assertIs(values["uncommitted"], True)
        self.assertEqual(values["branch"], "fix/call-1")
        self.assertNotIn("commit_sha", values)
        self.assertNotIn("commit", values)
        self.assertIn("review_failed", values["unresolved_issues"])
        self.assertNotIn("local_review_material_findings", values["unresolved_issues"])
        self.assertFalse(values["external_review_recommended"])
        self.assertFalse(values["tests_run"])
        attempts = values["review_attempts"]
        self.assertEqual(len(attempts), MAX_REVIEW_INFRASTRUCTURE_ATTEMPTS)
        self.assertEqual(list(attempt.result.durable_values["review_attempts"]), list(attempts))
        self.assertEqual(
            list(failure["review_attempts"]),
            list(attempts),
        )
        for index, record in enumerate(attempts, start=1):
            self.assertEqual(record["attempt"], index)
            self.assertEqual(record["reason"], "review_schema_invalid")
            self.assertEqual(record["error_message"], "findings is not a list")
            self.assertLessEqual(len(record["raw_excerpt"]), MAX_REVIEW_RAW_EXCERPT_CHARACTERS)
            self.assertEqual(len(record["raw_excerpt"]), MAX_REVIEW_RAW_EXCERPT_CHARACTERS)
            self.assertIn("findings", record["raw_excerpt"])
            self.assertNotIn(huge, record["raw_excerpt"])
        self.assertEqual((worktree / "app.py").read_text(encoding="utf-8"), _FIXED)
        head = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=worktree, check=True, capture_output=True, text=True,
        ).stdout.strip()
        branch = subprocess.run(
            ["git", "rev-parse", "--abbrev-ref", "HEAD"],
            cwd=worktree, check=True, capture_output=True, text=True,
        ).stdout.strip()
        status = subprocess.run(
            ["git", "status", "--porcelain"],
            cwd=worktree, check=True, capture_output=True, text=True,
        ).stdout
        branches = subprocess.run(
            ["git", "branch", "--list", "fix/call-1"],
            cwd=worktree, check=True, capture_output=True, text=True,
        ).stdout
        self.assertEqual(head, before)
        self.assertEqual(branch, "fix/call-1")
        self.assertIn("fix/call-1", branches)
        self.assertIn("app.py", status)
        self.assertIn("app.py", values["git_diff"])

    def test_reviewer_timeout_preserves_diff_after_bounded_retries(self) -> None:
        """A reviewer timeout is the same infrastructure outcome, not a finding."""

        class TimingOutReviewer(PlanningModel):
            _model = "reviewer-model"

            def complete(self, request):
                self.requests.append(request)
                if request.output_schema_name == "alx_coding_local_review":
                    raise ProviderError(
                        "codex_subscription", "reasoning_timeout",
                        {"stdout_characters": 0, "stderr_characters": 0},
                    )
                return super().complete(request)

        worktree = _worktree(self.root, "review-timeout")
        reviewer = TimingOutReviewer()
        session = RecordingSession(edits={"app.py": _FIXED})
        attempt = self._run(
            PlanningModel(), session, reviewer=reviewer, task="fix add",
            worktree=str(worktree),
        )
        self.assertEqual(len(session.calls), 1)
        self.assertEqual(len(reviewer.requests), MAX_REVIEW_INFRASTRUCTURE_ATTEMPTS)
        failure = attempt.result.failure
        self.assertEqual(failure["code"], "review_failed")
        self.assertEqual(failure["reason_code"], "reasoning_timeout")
        self.assertEqual(failure["review_classification"], "infrastructure")
        self.assertIs(failure["diff_preserved"], True)
        self.assertEqual(failure["branch"], "fix/call-1")
        attempts = attempt.result.values["review_attempts"]
        self.assertEqual(len(attempts), MAX_REVIEW_INFRASTRUCTURE_ATTEMPTS)
        for record in attempts:
            self.assertEqual(record["reason"], "reasoning_timeout")
            self.assertEqual(
                record["error_message"],
                "codex_subscription provider failure: reasoning_timeout",
            )
            self.assertEqual(record["raw_excerpt"], "")
        self.assertEqual((worktree / "app.py").read_text(encoding="utf-8"), _FIXED)
        self.assertNotIn("commit_sha", attempt.result.values)

    def test_material_review_findings_stay_advisory_without_infrastructure_retry(self) -> None:
        """A material finding is not an infrastructure failure and is not retried."""
        worktree = _worktree(self.root, "material-advisory")
        model = PlanningModel()
        reviewer = PlanningModel(reviews=[
            {"findings": [{
                "severity": "high", "title": "first material issue",
                "evidence": "candidate is incomplete", "correction": "complete it",
            }]},
            {"findings": [{
                "severity": "high", "title": "still incomplete",
                "evidence": "candidate remains incomplete", "correction": "complete it",
            }]},
        ])

        class OneCorrection(RecordingSession):
            def run_session(self, request, briefing):
                self.calls.append((request, briefing))
                root = Path(request.worktree)
                (root / "app.py").write_text(
                    _FIXED if len(self.calls) == 1 else _FIXED + "\n# reviewer correction\n",
                    encoding="utf-8",
                )
                return CodingSessionResult(True, "corrected", turns=2)

        session = OneCorrection()
        attempt = self._run(
            model, session, reviewer=reviewer, task="fix add",
            worktree=str(worktree),
        )
        self.assertEqual(attempt.result.state, CapabilityResultState.SUCCEEDED)
        self.assertEqual(len(session.calls), 2)
        self.assertEqual(len(reviewer.requests), MAX_LOCAL_REVIEW_CYCLES)
        self.assertIsNone(attempt.result.failure)
        values = attempt.result.values
        self.assertNotIn("review_classification", values)
        self.assertNotIn("diff_preserved", values)
        self.assertNotIn("review_attempts", values)
        self.assertNotIn("uncommitted", values)
        self.assertIn("local_review_material_findings", values["unresolved_issues"])
        self.assertNotIn("review_failed", values["unresolved_issues"])
        self.assertTrue(values["external_review_recommended"])
        self.assertEqual(values["review_findings"][0]["title"], "still incomplete")
        self.assertTrue(values["commit_sha"])
        self.assertTrue(values["all_required_verification_passed"])

    def test_review_status_codes_are_declared_on_the_coding_contract(self) -> None:
        declared = DEFINITION.possible_failure_codes
        self.assertEqual(declared, CODING_FAILURES)
        self.assertIn("review_failed", declared)
        self.assertIn("local_review_material_findings", declared)
        self.assertIn("task_failed", declared)
        self.assertEqual(
            CODING_FAILURES[CODING_FAILURES.index("planning_failed") + 1],
            "review_failed",
        )
        self.assertEqual(
            CODING_FAILURES[CODING_FAILURES.index("review_failed") + 1],
            "local_review_material_findings",
        )
        self.assertEqual(
            CODING_FAILURES[
                CODING_FAILURES.index("local_review_material_findings") + 1
            ],
            "git_refused",
        )
        self.assertLess(
            _OUTCOME_ISSUE_CODES.index("planning_failed"),
            _OUTCOME_ISSUE_CODES.index("review_failed"),
        )
        self.assertEqual(
            _OUTCOME_ISSUE_CODES.index("review_failed") + 1,
            _OUTCOME_ISSUE_CODES.index("local_review_material_findings"),
        )
        self.assertLess(
            _OUTCOME_ISSUE_CODES.index("local_review_material_findings"),
            _OUTCOME_ISSUE_CODES.index("git_refused"),
        )
        self.assertNotIn("task_failed", _OUTCOME_ISSUE_CODES)
        self.assertNotIn("no_files_changed", _OUTCOME_ISSUE_CODES)

    def test_reviewer_has_one_correction_and_one_recheck_bound(self) -> None:
        """The cycle stays bounded; surviving findings no longer fail the job.

        The bound is unchanged: one correction, one recheck. What changed is
        the disposition of findings that survive it. They used to fail the job
        closed, which destroyed the very artifact AL/X needed in order to judge
        them — the candidate was left as an uncommitted diff in a retained
        worktree. They are now advisory evidence returned beside the work.
        """
        worktree = _worktree(self.root)
        model = PlanningModel()
        reviewer = PlanningModel(reviews=[
            {"findings": [{
                "severity": "high", "title": "first material issue",
                "evidence": "candidate is incomplete", "correction": "complete it",
            }]},
            {"findings": [{
                "severity": "high", "title": "still incomplete",
                "evidence": "candidate remains incomplete", "correction": "complete it",
            }]},
        ])

        class OneCorrection(RecordingSession):
            def run_session(self, request, briefing):
                self.calls.append((request, briefing))
                root = Path(request.worktree)
                (root / "app.py").write_text(
                    _FIXED if len(self.calls) == 1 else _FIXED + "\n# reviewer correction\n",
                    encoding="utf-8",
                )
                return CodingSessionResult(True, "corrected", turns=2)

        session = OneCorrection()
        attempt = self._run(
            model, session, reviewer=reviewer, task="fix add", worktree=str(worktree)
        )
        # The bound itself is what this test guards: one correction, one
        # recheck, and no more.
        self.assertEqual(len(session.calls), 2)
        self.assertEqual(
            [item.output_schema_name for item in reviewer.requests],
            ["alx_coding_local_review", "alx_coding_local_review"],
        )
        # The job is no longer failed by the reviewer's opinion.
        self.assertEqual(attempt.result.state, CapabilityResultState.SUCCEEDED)
        values = attempt.result.values
        # It is still said plainly that findings remain.
        self.assertIn(
            "local_review_material_findings", values["unresolved_issues"]
        )
        self.assertNotIn("task_failed", values["unresolved_issues"])
        self.assertTrue(values["external_review_recommended"])
        # And the findings themselves reach AL/X, not just the code.
        self.assertEqual(len(values["review_findings"]), 1)
        self.assertEqual(
            values["review_findings"][0]["title"], "still incomplete"
        )
        self.assertEqual(values["material_review_findings"], 1)

    def test_clean_review_only_advises_and_proceeds_to_alx_verification(self) -> None:
        worktree = _worktree(self.root)
        model = PlanningModel()
        reviewer = PlanningModel(reviews=[{"findings": []}])
        session = RecordingSession(edits={"app.py": _FIXED})
        attempt = self._run(
            model, session, reviewer=reviewer, task="fix add", worktree=str(worktree),
            test_guidance="python -m unittest -q test_app",
        )
        self.assertEqual(attempt.result.state, CapabilityResultState.SUCCEEDED)
        self.assertEqual(len(session.calls), 1)
        self.assertEqual([item.output_schema_name for item in model.requests], ["alx_coding_plan"])
        review_request = reviewer.requests[0]
        self.assertEqual(review_request.output_schema_name, "alx_coding_local_review")
        instruction = review_request.messages[0].content.lower()
        for withheld in ("edit", "run commands", "commit", "push", "merge", "external review"):
            self.assertIn(withheld, instruction)
        review_payload = json.loads(review_request.messages[-1].content)
        self.assertNotIn("worktree", review_payload)
        self.assertTrue(attempt.result.values["tests_run"])

    def test_local_reviewer_does_not_touch_external_review_wiring(self) -> None:
        source = (
            REPOSITORY_ROOT / "src" / "alx" / "providers" / "coding_agent.py"
        ).read_text(encoding="utf-8")
        for external in ("Qodo", "request_external_review", "review.request"):
            self.assertNotIn(external, source)

    def test_the_session_receives_the_canonical_checkout_and_the_plan(self) -> None:
        worktree = _worktree(self.root)
        session = RecordingSession(edits={"app.py": _FIXED})
        self._run(
            PlanningModel(),
            session,
            task="fix the add helper",
            worktree=str(worktree),
            acceptance_criteria=["add returns the sum"],
        )
        request, briefing = session.calls[0]
        session_root = Path(request.worktree).resolve()
        self.assertEqual(session_root, worktree.resolve())
        self.assertTrue((session_root / ".git").is_dir())
        self.assertIn("fix the add helper", briefing)
        self.assertIn("add returns the sum", briefing)
        self.assertIn("Inspect the assigned task", briefing)

    def test_the_briefing_states_the_withheld_authority(self) -> None:
        """14. Commit, push, merge and review are refused in the instruction."""
        worktree = _worktree(self.root)
        session = RecordingSession(edits={"app.py": _FIXED})
        self._run(
            PlanningModel(), session, task="fix add", worktree=str(worktree)
        )
        briefing = session.calls[0][1].lower()
        for forbidden in ("commit", "push", "merge", "deploy", "review"):
            self.assertIn(forbidden, briefing)
        self.assertIn("no terminal", briefing)

    def test_alx_verifies_with_its_own_bounded_executor(self) -> None:
        """13. Checks are run by AL/X after the session, through the allowlist.

        The commands are now chosen by the deterministic policy rather than
        parsed out of `test_guidance`. A Python change is still verified by
        running Python tests; what changed is that the job no longer gets to
        nominate the command.
        """
        worktree = _worktree(self.root)
        session = RecordingSession(edits={"app.py": _FIXED})
        attempt = self._run(
            PlanningModel(),
            session,
            task="fix add",
            worktree=str(worktree),
        )
        values = attempt.result.values
        self.assertEqual(attempt.result.state, CapabilityResultState.SUCCEEDED)
        self.assertTrue(values["tests_run"])
        self.assertTrue(values["tests_passed"])
        self.assertTrue(values["all_required_verification_passed"])
        # The diff check runs for every job, whatever it changed.
        self.assertEqual(
            tuple(values["commands"][0]["argv"]), ("git", "diff", "--check")
        )
        self.assertIn("diff_check", values["verification"]["ran"])

    def test_a_suite_that_collected_nothing_is_not_a_failed_suite(self) -> None:
        """pytest exit 5 means there was nothing to run, not that it failed.

        A Python change in a worktree holding no tests escalates to the full
        suite, which collects nothing and exits 5. Reading that as a failure
        made such a change permanently uncommittable — the same shape as the
        defect this module's rewrite removes, one class of verification
        standing in for verification itself. Found by CI on PR #54, where a
        fixture repository with no tests failed for exactly this reason.
        """
        from alx.providers.coding_agent import _check_passed

        collected_nothing = CodingCommandRecord(
            ("python", "-m", "pytest", "-q"), 5, "no tests ran", "", False, True
        )
        self.assertTrue(_check_passed(collected_nothing, "pytest_full"))
        # But a *targeted* run collecting nothing is a failure: the basis for
        # running those files instead of the suite is that they cover the
        # change, and collecting nothing proves that basis false. Accepting it
        # would let a job commit having executed no test at all.
        self.assertFalse(_check_passed(collected_nothing, "pytest_targeted"))
        # A genuine test failure is still a failure.
        self.assertFalse(
            _check_passed(
                CodingCommandRecord(
                    ("python", "-m", "pytest", "-q"), 1, "", "", False, True
                ),
                "pytest_full",
            )
        )
        # And exit 5 is forgiven only for a test command; a gate exiting 5 is
        # a gate that failed.
        self.assertFalse(
            _check_passed(
                CodingCommandRecord(
                    ("python", "scripts/check_governance.py"), 5, "", "", False, True
                ),
                "governance_gate",
            )
        )
        # A timeout is never a pass, whatever it exited with.
        self.assertFalse(
            _check_passed(
                CodingCommandRecord(
                    ("python", "-m", "pytest", "-q"), 5, "", "", True, True
                ),
                "pytest_full",
            )
        )

    def test_supplied_test_guidance_cannot_choose_the_verification(self) -> None:
        """Verification is derived from files, never from prose in the request.

        `test_guidance` used to be scanned for command-shaped lines, which made
        the thing being verified a contributor to its own verification policy.
        It is still carried to the session as advice; it no longer reaches the
        executor.
        """
        worktree = _worktree(self.root)
        session = RecordingSession(edits={"app.py": _FIXED})
        attempt = self._run(
            PlanningModel(),
            session,
            task="fix add",
            worktree=str(worktree),
            test_guidance="python -m unittest -q test_app",
        )
        argv_run = [tuple(item["argv"]) for item in attempt.result.values["commands"]]
        self.assertTrue(
            all("unittest" not in argv for argv in argv_run), argv_run
        )

    def test_changed_test_modules_are_preferred_to_the_full_suite(self) -> None:
        """A changed test module is its own verification, not the whole suite."""
        worktree = _worktree(self.root)
        # `test_app.py` alone: a changed test module is its own mapping, so
        # nothing is left uncovered and the suite is not needed.
        policy = required_verification(("test_app.py",), worktree)
        self.assertEqual(
            policy.commands,
            (
                ("git", "diff", "--check"),
                ("python", "-m", "pytest", "-q", "test_app.py"),
            ),
        )
        self.assertNotIn(
            ("python", "-m", "pytest", "-q", "-p", "no:cacheprovider"),
            policy.commands,
        )
        # Adding an unmapped Python file escalates the whole set, rather than
        # letting the mapped file's test speak for the unmapped one.
        mixed = required_verification(("app.py", "test_app.py"), worktree)
        self.assertIn(
            ("python", "-m", "pytest", "-q", "-p", "no:cacheprovider"),
            mixed.commands,
        )

    def test_changed_pytest_module_can_verify_a_successful_job(self) -> None:
        """A newly changed regression test is run through AL/X's executor."""
        worktree = _worktree(self.root)
        session = RecordingSession(
            edits={
                "app.py": _FIXED,
                "test_app.py": (worktree / "test_app.py").read_text(
                    encoding="utf-8"
                ) + "\n# Regression coverage updated by this job.\n",
            }
        )
        attempt = self._run(
            PlanningModel(), session, task="fix add", worktree=str(worktree)
        )
        self.assertEqual(attempt.result.state, CapabilityResultState.SUCCEEDED)
        self.assertTrue(attempt.result.values["tests_passed"])
        # `app.py` changed too and maps to no test in this flat fixture, so the
        # set escalates rather than letting `test_app.py` cover both.
        self.assertEqual(
            tuple(attempt.result.values["commands"][1]["argv"]),
            ("python", "-m", "pytest", "-q", "-p", "no:cacheprovider"),
        )

    def test_a_correction_only_file_selects_the_targeted_verification(self) -> None:
        """A file only the reviewer correction touched still chooses the tests.

        The initial session changes `app.py` alone. The correction cycle adds
        `test_parallel.py`, which never appeared in the original session's file
        set. Targeted verification selects from the job's final reviewed files,
        so the correction-only module is the evidence AL/X actually runs.
        """
        worktree = _worktree(self.root)
        reviewer = PlanningModel(reviews=[
            {"findings": [{
                "severity": "high", "title": "the repair has no regression",
                "evidence": "no test covers the corrected helper",
                "correction": "add a regression module",
            }]},
            {"findings": []},
        ])

        class CorrectingSession(RecordingSession):
            def run_session(self, request, briefing):
                self.calls.append((request, briefing))
                root = Path(request.worktree)
                if len(self.calls) == 1:
                    (root / "app.py").write_text(_FIXED, encoding="utf-8")
                else:
                    (root / "test_parallel.py").write_text(
                        "from app import add\n\n\n"
                        "def test_add():\n"
                        "    assert add(1, 2) == 3\n",
                        encoding="utf-8",
                    )
                return CodingSessionResult(True, "corrected", turns=2)

        session = CorrectingSession()
        attempt = self._run(
            PlanningModel(), session, reviewer=reviewer,
            task="fix add", worktree=str(worktree),
        )
        values = attempt.result.values
        self.assertEqual(attempt.result.state, CapabilityResultState.SUCCEEDED)
        self.assertEqual(len(session.calls), 2)
        # The correction-only file reaches the reviewed/evidence set...
        self.assertIn("test_parallel.py", values["files_changed"])
        # ...and the same set chooses the verification. `app.py` is also in
        # it and maps to nothing here, so the set escalates; what this test
        # holds is that the correction-only file reached the policy at all.
        self.assertEqual(
            tuple(values["commands"][1]["argv"]),
            ("python", "-m", "pytest", "-q", "-p", "no:cacheprovider"),
        )
        self.assertTrue(values["tests_run"])
        self.assertTrue(values["tests_passed"])

    def test_verification_timeout_remains_failed_bounded_evidence(self) -> None:
        """A realistic bound does not turn a genuine timeout into success."""
        from alx.contracts.coding import FULL_SUITE_COMMAND_SECONDS

        worktree = _worktree(self.root)
        session = RecordingSession(edits={"app.py": _FIXED})
        bounds: dict[tuple[str, ...], int] = {}

        def timed_out(argv, *_args, **kwargs):
            bounds[tuple(argv)] = kwargs["timeout_seconds"]
            return CodingCommandRecord(tuple(argv), -1, "partial", "", True, True)

        with patch.object(coding_agent_module, "run_permitted_command", timed_out):
            attempt = self._run(
                PlanningModel(), session, task="fix add", worktree=str(worktree),
            )
        self.assertEqual(attempt.result.state, CapabilityResultState.FAILED)
        self.assertFalse(attempt.result.values["all_required_verification_passed"])
        command = attempt.result.values["commands"][0]
        self.assertTrue(command["timed_out"])
        self.assertEqual(command["stdout"], "partial")
        # The full suite gets its own longer bound; every other check keeps the
        # shared one. `app.py` maps to no test here, so the suite is selected.
        self.assertEqual(
            bounds[("python", "-m", "pytest", "-q", "-p", "no:cacheprovider")],
            FULL_SUITE_COMMAND_SECONDS,
        )
        self.assertEqual(
            bounds[("git", "diff", "--check")],
            DEFAULT_VERIFICATION_COMMAND_SECONDS,
        )

    def test_failing_tests_defeat_a_confident_session_report(self) -> None:
        """A session claiming success cannot outrank a failing suite."""
        worktree = _worktree(self.root)
        session = RecordingSession(
            edits={"app.py": "def add(a, b):\n    return a - b - 1\n"},
            report="all good",
        )
        attempt = self._run(
            PlanningModel(),
            session,
            task="fix add",
            worktree=str(worktree),
            test_guidance="python -m unittest -q test_app",
        )
        self.assertEqual(attempt.result.state, CapabilityResultState.FAILED)
        self.assertFalse(attempt.result.values["tests_passed"])

    def test_verification_refuses_a_command_outside_the_allowlist(self) -> None:
        """14. Guidance naming a forbidden command never runs it."""
        worktree = _worktree(self.root)
        session = RecordingSession(edits={"app.py": _FIXED})
        attempt = self._run(
            PlanningModel(),
            session,
            task="fix add",
            worktree=str(worktree),
            test_guidance="git commit -am done\ngit push origin main",
        )
        for record in attempt.result.values["commands"]:
            argv = tuple(record["argv"])
            self.assertNotIn("commit", argv)
            self.assertNotIn("push", argv)
            self.assertTrue(command_permitted(list(argv), worktree))

    def test_a_session_that_changes_nothing_is_not_a_success(self) -> None:
        """Undeclared issues such as no_files_changed still surface as task_failed."""
        worktree = _worktree(self.root)
        session = RecordingSession(edits={}, report="nothing needed")
        attempt = self._run(
            PlanningModel(), session, task="fix add", worktree=str(worktree)
        )
        self.assertEqual(attempt.result.state, CapabilityResultState.FAILED)
        self.assertEqual(attempt.result.failure["code"], "task_failed")
        self.assertIn("no_files_changed", attempt.result.values["unresolved_issues"])
        self.assertNotIn("no_files_changed", DEFINITION.possible_failure_codes)

    def test_a_session_failure_is_reported_not_swallowed(self) -> None:
        worktree = _worktree(self.root)
        session = RecordingSession(
            raises=CodingError("sandbox_unusable", reason_code="sandbox_not_applied")
        )
        attempt = self._run(
            PlanningModel(), session, task="fix add", worktree=str(worktree)
        )
        self.assertEqual(attempt.result.state, CapabilityResultState.FAILED)
        self.assertEqual(attempt.result.failure["code"], "sandbox_unusable")
        self.assertEqual(
            attempt.result.failure["reason_code"], "sandbox_not_applied"
        )

    def test_outcome_separates_job_changes_from_preexisting_dirt(self) -> None:
        """15. Work already in the tree is not claimed as this job's.

        D-031 strengthened this from a reporting property into a structural
        one. The dirt used to sit in the same directory the job worked in, so
        the job had to distinguish it; now the job is cut from the repository's
        committed HEAD and never sees it at all. Both halves are asserted: the
        dirt stays where it was, and the job's own change is still reported.
        """
        worktree = _worktree(self.root)
        (worktree / "unrelated.py").write_text("already dirty\n", encoding="utf-8")
        session = RecordingSession(edits={"app.py": _FIXED})
        attempt = self._run(
            PlanningModel(), session, task="fix add", worktree=str(worktree)
        )
        self.assertEqual(attempt.result.state, CapabilityResultState.FAILED)
        self.assertEqual(attempt.result.failure["reason_code"], "canonical_checkout_dirty")
        self.assertEqual(session.calls, [])
        self.assertEqual(
            (worktree / "unrelated.py").read_text(), "already dirty\n"
        )

    def test_local_reviewer_does_not_receive_unmodified_preexisting_dirt(self) -> None:
        worktree = _worktree(self.root)
        (worktree / "unrelated.py").write_text("private dirty work\n", encoding="utf-8")
        reviewer = PlanningModel(reviews=[{"findings": []}])
        model = PlanningModel(plan=_plan(
            inspection_targets=["app.py", "unrelated.py"]
        ))
        session = RecordingSession(edits={"app.py": _FIXED})
        attempt = self._run(
            model, session,
            reviewer=reviewer, task="fix add", worktree=str(worktree),
        )
        self.assertEqual(attempt.result.state, CapabilityResultState.FAILED)
        self.assertEqual(attempt.result.failure["reason_code"], "canonical_checkout_dirty")
        self.assertEqual(session.calls, [])
        self.assertEqual(reviewer.requests, [])

    def test_capability_is_unregistered_without_a_session(self) -> None:
        """A plan with nothing to execute it is honest absence, not a failure."""
        self.assertIsNone(
            build_coding_runtime(True, PlanningModel(), lambda: "call-1")
        )

    def test_capability_is_unregistered_for_a_non_repository_checkout(self) -> None:
        """A bad checkout disables coding without taking down the runtime."""
        self.assertIsNone(build_coding_runtime(
            True,
            PlanningModel(),
            lambda: "call-1",
            session=RecordingSession(),
            reviewer=PlanningModel(),
            repository=self.root,
        ))


    def _governed_fixture(self, gate_exit: int) -> Path:
        worktree = _worktree(self.root)
        (worktree / "scripts").mkdir()
        (worktree / "scripts/check_governance.py").write_text(
            f"raise SystemExit({gate_exit})\n", encoding="utf-8"
        )
        (worktree / "governance").mkdir()
        (worktree / "governance/NOTES.md").write_text("notes\n", encoding="utf-8")
        _git(worktree, "add", ".")
        _git(worktree, "commit", "-m", "governed fixture")
        return worktree

    def test_a_check_only_the_request_blocked_is_a_request_conflict(self) -> None:
        """The edit is sound; the request's own blocked paths stopped its gate.

        Found in acceptance on 2026-09-24: Core blocked `scripts/` for a
        documentation job that edited a canonical governance document, so the
        required governance gate could never run. That is recoverable by a
        changed plan, and the failure says so rather than reading as the
        work failing.
        """
        worktree = self._governed_fixture(gate_exit=0)
        attempt = self._run(
            PlanningModel(),
            RecordingSession(edits={"governance/NOTES.md": "notes, clarified\n"}),
            task="clarify the notes",
            worktree=str(worktree),
            blocked_paths=["scripts/"],
        )
        failure = attempt.result.failure
        self.assertEqual(failure["code"], "required_verification_failed")
        self.assertEqual(failure["failure_class"], "request_conflict")
        self.assertEqual(
            list(attempt.result.values["verification"]["failed"]), ["governance_gate"]
        )
        self.assertNotIn(
            "governance_gate", attempt.result.values["verification"]["ran"]
        )

    def test_a_check_that_ran_and_failed_is_an_implementation_failure(self) -> None:
        worktree = self._governed_fixture(gate_exit=1)
        attempt = self._run(
            PlanningModel(),
            RecordingSession(edits={"governance/NOTES.md": "notes, clarified\n"}),
            task="clarify the notes",
            worktree=str(worktree),
        )
        failure = attempt.result.failure
        self.assertEqual(failure["code"], "required_verification_failed")
        self.assertIn("governance_gate", attempt.result.values["verification"]["ran"])
        self.assertNotIn("failure_class", failure)

    def test_a_blocked_check_beside_a_real_failure_is_an_implementation_failure(self) -> None:
        # The request blocked the gate, but the tests the edit owes also ran
        # and failed: the work itself is wrong, whatever the request blocked.
        worktree = self._governed_fixture(gate_exit=0)
        attempt = self._run(
            PlanningModel(),
            RecordingSession(edits={
                "governance/NOTES.md": "notes, clarified\n",
                "app.py": "def add(a, b):\n    return a * b\n",
            }),
            task="clarify the notes",
            worktree=str(worktree),
            blocked_paths=["scripts/"],
        )
        failure = attempt.result.failure
        verification = attempt.result.values["verification"]
        self.assertEqual(failure["code"], "required_verification_failed")
        self.assertIn("governance_gate", verification["failed"])
        self.assertTrue(
            [name for name in verification["ran"] if name in verification["failed"]]
        )
        self.assertNotIn("failure_class", failure)

    def test_a_session_error_cannot_carry_a_class_into_the_failure(self) -> None:
        worktree = self._governed_fixture(gate_exit=1)

        class RaisingSession(RecordingSession):
            def run_session(self, request, briefing):
                raise CodingError(
                    "session_failed", reason_code="claimed",
                    failure_class="request_conflict",
                )

        attempt = self._run(
            PlanningModel(),
            RaisingSession(edits={}),
            task="clarify the notes",
            worktree=str(worktree),
        )
        failure = attempt.result.failure
        self.assertEqual(failure["reason_code"], "claimed")
        self.assertNotIn("failure_class", failure)

    def test_a_session_cannot_classify_its_own_failure(self) -> None:
        worktree = self._governed_fixture(gate_exit=1)

        class ClaimingSession(RecordingSession):
            def run_session(self, request, briefing):
                result = super().run_session(request, briefing)
                return replace(
                    result,
                    diagnostics={
                        **result.diagnostics, "failure_class": "request_conflict"
                    },
                )

        attempt = self._run(
            PlanningModel(),
            ClaimingSession(edits={"governance/NOTES.md": "notes, clarified\n"}),
            task="clarify the notes",
            worktree=str(worktree),
        )
        self.assertNotIn("failure_class", attempt.result.failure)


class SessionLaunchTests(unittest.TestCase):
    """What the native session actually asks the CLI to do."""

    def setUp(self) -> None:
        self.directory = tempfile.TemporaryDirectory()
        self.root = Path(self.directory.name)
        self.addCleanup(self.directory.cleanup)

    def _session(self, **kwargs) -> GrokCodingSession:
        return GrokCodingSession("grok-4.6", 900, **kwargs)

    def test_native_file_tools_are_not_withheld(self) -> None:
        """3. Reading, searching and editing stay native."""
        for tool in NATIVE_TOOLS:
            self.assertNotIn(tool, WITHHELD_TOOLS)
        command = self._session().command(self.root / "p.txt", self.root)
        withheld = command[command.index("--disallowed-tools") + 1].split(",")
        for tool in NATIVE_TOOLS:
            self.assertNotIn(tool, withheld)
        self.assertNotIn("--tools", command)

    def test_headless_file_edits_are_auto_approved_without_bypass(self) -> None:
        """Native edits cannot wait for unavailable headless stdin."""
        command = self._session().command(self.root / "p.txt", self.root)
        self.assertIn("--always-approve", command)
        self.assertNotIn("bypassPermissions", command)
        self.assertNotIn("--permission-mode", command)
        self.assertEqual(
            command[command.index("--sandbox") + 1],
            coding_containment.PROFILE_NAME,
        )
        withheld = command[command.index("--disallowed-tools") + 1].split(",")
        self.assertIn("run_terminal_cmd", withheld)

    def test_the_terminal_is_withheld(self) -> None:
        """4. No native shell in this iteration, by name and by alias."""
        command = self._session().command(self.root / "p.txt", self.root)
        withheld = command[command.index("--disallowed-tools") + 1].split(",")
        for tool in ("run_terminal_cmd", "run_terminal_command", "Bash", "bash"):
            self.assertIn(tool, withheld)

    def test_the_session_is_multi_turn(self) -> None:
        """5. One turn is what broke execution before; it cannot return."""
        command = self._session().command(self.root / "p.txt", self.root)
        turns = int(command[command.index("--max-turns") + 1])
        self.assertGreater(turns, 1)
        with self.assertRaises(ValueError):
            GrokCodingSession("grok-4.6", 900, max_turns=1)

    def test_the_generated_sandbox_profile_is_named_and_used(self) -> None:
        """6. Every job runs under its own generated custom profile."""
        command = self._session().command(self.root / "p.txt", self.root)
        self.assertEqual(
            command[command.index("--sandbox") + 1], coding_containment.PROFILE_NAME
        )
        self.assertEqual(command[command.index("--cwd") + 1], str(self.root))

    def test_metered_credentials_are_stripped_from_the_child(self) -> None:
        """11. An exhausted subscription must not become billed API usage."""
        session = self._session(
            environment={
                "PATH": "/usr/bin",
                "HOME": "/home/x",
                "XAI_API_KEY": "must-not-pass",
                "OPENAI_API_KEY": "must-not-pass",
                "ANTHROPIC_API_KEY": "must-not-pass",
            }
        )
        environment = session.child_environment(self.root)
        for key in ("XAI_API_KEY", "OPENAI_API_KEY", "ANTHROPIC_API_KEY"):
            self.assertNotIn(key, environment)
        self.assertEqual(environment["GROK_HOME"], str(self.root))

    def test_no_provider_fallback_exists_in_the_session(self) -> None:
        """12. The selected provider fails closed; it never substitutes one."""
        source = (
            REPOSITORY_ROOT / "src" / "alx" / "providers" / "coding_session.py"
        ).read_text(encoding="utf-8")
        for forbidden in ("OpenAI", "Anthropic", "httpx", "xai_sdk"):
            self.assertNotIn(forbidden, source)

    def test_a_failed_sandbox_fails_the_job_closed(self) -> None:
        """7. A profile that cannot be applied stops the job."""
        def refuse(*_args, **_kwargs):
            return subprocess.CompletedProcess(
                [], 1, "", "error: could not apply the sandbox profile; "
                "Refusing to start with its protections missing."
            )

        session = self._session(runner=refuse)
        request = CodingRequest(task="t", job_id="job-1", worktree=str(self.root))
        with self.assertRaises(CodingError) as raised:
            session.run_session(request, "briefing")
        self.assertEqual(raised.exception.code, "sandbox_unusable")
        self.assertEqual(
            raised.exception.details["reason_code"], "sandbox_not_applied"
        )

    def test_a_cut_off_session_is_not_reported_as_complete(self) -> None:
        def truncated(*_args, **_kwargs):
            return subprocess.CompletedProcess(
                [], 0,
                json.dumps({"text": "partial", "stopReason": "max_turns"}), "",
            )

        session = self._session(runner=truncated)
        result = session.run_session(
            CodingRequest(task="t", job_id="job-1", worktree=str(self.root)), "briefing"
        )
        self.assertFalse(result.completed)
        self.assertEqual(result.failure_code, "session_failed")

    def test_camelcase_endturn_is_reported_as_complete(self) -> None:
        def completed(*_args, **_kwargs):
            return subprocess.CompletedProcess(
                [], 0,
                json.dumps({"text": "done", "stopReason": "EndTurn"}), "",
            )

        session = self._session(runner=completed)
        result = session.run_session(
            CodingRequest(task="t", job_id="job-1", worktree=str(self.root)), "briefing"
        )
        self.assertTrue(result.completed)
        self.assertEqual(result.failure_code, "")


class SessionTimeoutTests(unittest.TestCase):
    """The session's bound is its own, and exceeding it fails closed."""

    def setUp(self) -> None:
        self.directory = tempfile.TemporaryDirectory()
        self.root = Path(self.directory.name)
        self.addCleanup(self.directory.cleanup)

    def test_planning_and_session_timeouts_are_independent(self) -> None:
        """A planning call answers in seconds; a session needs far longer."""
        from alx.config.settings import (
            DEFAULT_CODING_SESSION_TIMEOUT_SECONDS,
            _coding_settings,
        )

        settings = _coding_settings(
            {
                "ALX_CODING_ENABLED": "true",
                "ALX_CODING_PROVIDER": "grok_subscription",
                "ALX_CODING_MODEL": "grok-4.6",
            }
        )
        self.assertEqual(settings.reasoning.timeout_seconds, 120)
        self.assertEqual(
            settings.session_timeout_seconds,
            DEFAULT_CODING_SESSION_TIMEOUT_SECONDS,
        )
        self.assertGreater(
            settings.session_timeout_seconds, settings.reasoning.timeout_seconds
        )

    def test_each_timeout_is_configured_by_its_own_variable(self) -> None:
        from alx.config.settings import _coding_settings

        settings = _coding_settings(
            {
                "ALX_CODING_ENABLED": "true",
                "ALX_CODING_PROVIDER": "grok_subscription",
                "ALX_CODING_MODEL": "grok-4.6",
                "ALX_CODING_REVIEWER_PROVIDER": "grok_subscription",
                "ALX_CODING_REVIEWER_MODEL": "grok-4.6",
                "ALX_CODING_TIMEOUT_SECONDS": "45",
                "ALX_CODING_SESSION_TIMEOUT_SECONDS": "1800",
            }
        )
        self.assertEqual(settings.reasoning.timeout_seconds, 45)
        self.assertEqual(settings.session_timeout_seconds, 1800)
        # Changing one must not move the other.
        planning_only = _coding_settings(
            {
                "ALX_CODING_ENABLED": "true",
                "ALX_CODING_PROVIDER": "grok_subscription",
                "ALX_CODING_MODEL": "grok-4.6",
                "ALX_CODING_TIMEOUT_SECONDS": "45",
            }
        )
        self.assertEqual(planning_only.reasoning.timeout_seconds, 45)
        self.assertEqual(planning_only.session_timeout_seconds, 1200)

    def test_the_native_session_is_built_with_the_session_timeout(self) -> None:
        """The regression that killed a working session after two minutes."""
        from alx.bootstrap.providers import _build_coding_session
        from alx.config.settings import _coding_settings

        class _Settings:
            def __init__(self, coding):
                self.coding = coding

        settings = _coding_settings(
            {
                "ALX_CODING_ENABLED": "true",
                "ALX_CODING_PROVIDER": "grok_subscription",
                "ALX_CODING_MODEL": "grok-4.6",
                "ALX_CODING_REVIEWER_PROVIDER": "grok_subscription",
                "ALX_CODING_REVIEWER_MODEL": "grok-4.6",
                "ALX_CODING_TIMEOUT_SECONDS": "45",
                "ALX_CODING_SESSION_TIMEOUT_SECONDS": "1500",
            }
        )
        session = _build_coding_session(_Settings(settings))
        self.assertIsNotNone(session)
        self.assertEqual(session._timeout_seconds, 1500)
        self.assertNotEqual(
            session._timeout_seconds, settings.reasoning.timeout_seconds
        )

    def test_a_timed_out_session_fails_closed_with_bounded_evidence(self) -> None:
        """The failure is named, and what was already gathered survives it."""
        def expire(*_args, **kwargs):
            raise subprocess.TimeoutExpired(
                cmd="grok", timeout=kwargs.get("timeout", 1200)
            )

        session = GrokCodingSession("grok-4.6", 1200, runner=expire)
        with self.assertRaises(CodingError) as raised:
            session.run_session(
                CodingRequest(task="t", job_id="job-1", worktree=str(self.root)), "briefing"
            )
        self.assertEqual(raised.exception.code, "session_failed")
        self.assertEqual(
            raised.exception.details["reason_code"], "session_timeout"
        )

    def test_a_timeout_preserves_plan_and_dirty_state_evidence(self) -> None:
        """Evidence accumulated before the timeout still reaches Core."""
        worktree = _worktree(self.root)
        session = RecordingSession(
            raises=CodingError("session_failed", reason_code="session_timeout")
        )
        runtime = build_coding_runtime(
            True, PlanningModel(), lambda: "call-1", session=session,
            reviewer=PlanningModel(),
            repository=worktree,
        )
        attempt = CapabilityBroker(
            CapabilityRegistry(runtime.definitions),
            SafetyGate(runtime.policies),
            runtime.executors,
        ).dispatch(
            CapabilityCall(
                "call-1", RUN_CODING_TASK,
                {"task": "fix add", "repair_branch": "fix/timeout",
                 "commit_message": "fix timeout"},
            ),
            AuthorityContext(
                "friedl", frozenset({CODING_EXECUTE_PERMISSION}), NOW
            ),
        )
        self.assertEqual(attempt.result.state, CapabilityResultState.FAILED)
        self.assertEqual(attempt.result.failure["code"], "session_failed")
        self.assertEqual(
            attempt.result.failure["reason_code"], "session_timeout"
        )
        values = attempt.result.values
        self.assertTrue(values["plan_summary"])
        self.assertEqual(tuple(values["files_changed"]), ())
        self.assertEqual(tuple(values["preexisting_dirty"]), ())
        branch = subprocess.run(
            ["git", "branch", "--show-current"], cwd=worktree, check=True,
            capture_output=True, text=True,
        ).stdout.strip()
        self.assertEqual(branch, "fix/timeout")

    def test_a_timeout_is_not_retried(self) -> None:
        worktree = _worktree(self.root)
        session = RecordingSession(
            raises=CodingError("session_failed", reason_code="session_timeout")
        )
        runtime = build_coding_runtime(
            True, PlanningModel(), lambda: "call-1", session=session,
            reviewer=PlanningModel(),
            repository=worktree,
        )
        CapabilityBroker(
            CapabilityRegistry(runtime.definitions),
            SafetyGate(runtime.policies),
            runtime.executors,
        ).dispatch(
            CapabilityCall(
                "call-1", RUN_CODING_TASK,
                {"task": "fix add", "repair_branch": "fix/timeout",
                 "commit_message": "fix timeout"},
            ),
            AuthorityContext(
                "friedl", frozenset({CODING_EXECUTE_PERMISSION}), NOW
            ),
        )
        self.assertEqual(len(session.calls), 1)


class SandboxProfileTests(unittest.TestCase):
    """The deny list is the containment, so its text is the guarantee."""

    def setUp(self) -> None:
        self.directory = tempfile.TemporaryDirectory()
        self.root = Path(self.directory.name)
        self.addCleanup(self.directory.cleanup)

    def test_environment_and_credential_files_are_denied(self) -> None:
        """8. .env is denied wherever it sits, and so are private keys."""
        worktree = _worktree(self.root)
        profile = coding_containment.render_profile(worktree, ())
        self.assertIn('"**/.env"', profile)
        self.assertIn('"**/*.pem"', profile)
        self.assertIn('"**/*.key"', profile)
        self.assertIn('extends = "strict"', profile)
        self.assertIn(f'read_write = ["{worktree}"]', profile)

    def test_git_metadata_is_denied_in_a_normal_repository(self) -> None:
        """9. A normal repository keeps .git as a directory."""
        worktree = _worktree(self.root)
        entries = coding_containment.deny_entries(worktree, ())
        self.assertIn(str(worktree / ".git"), entries)

    def test_git_metadata_is_denied_through_a_linked_worktree(self) -> None:
        """9. A linked worktree's .git is a file pointing at the real store."""
        source = _worktree(self.root, "source")
        linked = self.root / "linked"
        _git(source, "worktree", "add", str(linked), "-b", "job")
        self.assertTrue((linked / ".git").is_file())
        entries = coding_containment.deny_entries(linked, ())
        self.assertIn(str(linked / ".git"), entries)
        # Denying only the pointer would leave the real object store readable
        # through its target, which the containment experiment demonstrated.
        common = (source / ".git").resolve()
        self.assertTrue(
            any(entry.startswith(str(common)) for entry in entries),
            f"the real git directory must be denied too: {entries}",
        )

    def test_blocked_paths_become_preventative_denies(self) -> None:
        """10. Core's blocked paths reach the kernel, anchored to this tree."""
        worktree = _worktree(self.root)
        entries = coding_containment.deny_entries(
            worktree, ("tests/test_coding_agent.py", "governance")
        )
        self.assertIn(str(worktree / "tests/test_coding_agent.py"), entries)
        self.assertIn(str(worktree / "governance"), entries)

    def test_a_blocked_path_cannot_escape_the_worktree(self) -> None:
        worktree = _worktree(self.root)
        with self.assertRaises(CodingError):
            coding_containment.deny_entries(worktree, ("../outside",))

    def test_an_inexpressible_blocked_path_fails_closed(self) -> None:
        """Brace alternation makes the CLI refuse; refuse here with a reason."""
        worktree = _worktree(self.root)
        with self.assertRaises(CodingError) as raised:
            coding_containment.deny_entries(worktree, ("secrets/*.{pem,key}",))
        self.assertEqual(raised.exception.code, "sandbox_unusable")


class SupersededExecutionPathTests(unittest.TestCase):
    """16. Law 0: the per-step protocol is gone, not hidden behind a flag."""

    def test_no_per_step_execution_protocol_remains(self) -> None:
        source = (
            REPOSITORY_ROOT / "src" / "alx" / "providers" / "coding_agent.py"
        ).read_text(encoding="utf-8")
        for removed in (
            "ANSWER_SCHEMA",
            "OPERATION_CONTRACT",
            "MAX_INVALID_EXECUTION_REPLIES",
            "_execution_protocol_failure",
            "def _act(",
            "def _ask(",
        ):
            self.assertNotIn(removed, source)

    def test_the_agent_cannot_execute_a_model_chosen_operation(self) -> None:
        for removed in ("_act", "_ask"):
            self.assertFalse(
                hasattr(coding_agent_module.CodingAgent, removed),
                f"{removed} would be a second execution path",
            )

    def test_the_transport_no_longer_disables_native_tools(self) -> None:
        source = (
            REPOSITORY_ROOT / "src" / "alx" / "providers" / "grok_subscription.py"
        ).read_text(encoding="utf-8")
        self.assertNotIn("_DISALLOWED_TOOLS", source)
        self.assertNotIn('"--tools"', source)
        self.assertNotIn('"--max-turns"', source)

    def test_the_workspace_can_no_longer_write_repository_files(self) -> None:
        """Editing is the session's; the workspace keeps only path arithmetic."""
        source = (
            REPOSITORY_ROOT / "src" / "alx" / "providers" / "coding_workspace.py"
        ).read_text(encoding="utf-8")
        self.assertNotIn("def write_text", source)
        self.assertNotIn("def read_text", source)


class BlockedPathTests(unittest.TestCase):
    def setUp(self) -> None:
        self.directory = tempfile.TemporaryDirectory()
        self.root = Path(self.directory.name)
        self.addCleanup(self.directory.cleanup)
        repo = self.root / "repo"
        (repo / "tests").mkdir(parents=True)
        (repo / "app.py").write_text("safe\n", encoding="utf-8")
        (repo / "tests" / "test_coding_agent.py").write_text(
            "original\n", encoding="utf-8"
        )
        (repo / "tests" / "test_other.py").write_text("other\n", encoding="utf-8")
        self.repo = repo

    def _workspace(self, blocked: tuple[str, ...]) -> CodingWorkspace:
        return CodingWorkspace(str(self.repo), blocked)

    def test_pytest_cannot_target_a_blocked_path(self) -> None:
        self.assertFalse(
            command_permitted(
                ["python", "-m", "unittest", "tests/test_coding_agent.py"],
                self.repo,
                ("tests/test_coding_agent.py",),
            )
        )
        self.assertTrue(
            command_permitted(
                ["python", "-m", "unittest", "app.py"],
                self.repo,
                ("tests/test_coding_agent.py",),
            )
        )
        self.assertFalse(
            command_permitted(
                ["python", "-m", "unittest", "tests.test_coding_agent"],
                self.repo,
                ("tests/test_coding_agent.py",),
            )
        )


class LiveCoreCatalogueTests(unittest.TestCase):
    """The live composition root is what Core actually sees."""

    def _core_inventory(self, overrides: dict[str, str]) -> dict[str, object]:
        import asyncio
        import tempfile

        from alx.bootstrap import live_voice
        from alx.continuity.due_source import DueCognitionSource
        from alx.core.loop import CoreAgent
        from alx.core.model_reasoner import _catalogue_payload
        from alx.interfaces.server import LiveVoiceServer
        from alx.providers.grok_subscription import GrokSubscriptionReasoningModel
        from alx.providers.openai import OpenAIReasoningModel
        from alx.providers.xai import XAIReasoningModel

        tests_dir = str(REPOSITORY_ROOT / "tests")
        if tests_dir not in sys.path:
            sys.path.insert(0, tests_dir)
        from test_runtime_startup_smoke import BASE_ENVIRONMENT

        captured: dict[str, object] = {}
        environment = dict(BASE_ENVIRONMENT)
        holder = tempfile.TemporaryDirectory()
        self.addCleanup(holder.cleanup)
        environment["ALX_RUNTIME_STORAGE_ROOT"] = holder.name
        environment.update(overrides)

        original_init = CoreAgent.__init__

        def capture(self_agent, *args, **kwargs):
            capabilities = args[3]
            captured["ids"] = tuple(item.capability_id for item in capabilities)
            captured["catalogue"] = _catalogue_payload(capabilities)
            captured["approval_free"] = kwargs.get("approval_free_capabilities")
            original_init(self_agent, *args, **kwargs)

        serving = asyncio.Event()

        def load_environment(_path, inherited=None):
            return dict(environment)

        async def serve_forever(_self):
            serving.set()
            await asyncio.Event().wait()

        async def tick_forever(_self):
            await asyncio.Event().wait()

        def refuse(*_args, **_kwargs):
            raise AssertionError("composition must not call a provider")

        patches = [
            (live_voice, "load_environment", load_environment),
            (LiveVoiceServer, "serve_forever", serve_forever),
            (DueCognitionSource, "run", tick_forever),
            (OpenAIReasoningModel, "complete", refuse),
            (XAIReasoningModel, "complete", refuse),
            (GrokSubscriptionReasoningModel, "complete", refuse),
            (CoreAgent, "__init__", capture),
        ]
        originals = [(target, name, getattr(target, name)) for target, name, _ in patches]
        for target, name, value in patches:
            setattr(target, name, value)

        async def scenario() -> None:
            runtime = asyncio.create_task(live_voice.run(REPOSITORY_ROOT))
            done, _ = await asyncio.wait(
                [runtime, asyncio.create_task(serving.wait())],
                timeout=30,
                return_when=asyncio.FIRST_COMPLETED,
            )
            if runtime in done:
                runtime.result()
                raise AssertionError("run() returned without serving")
            runtime.cancel()
            try:
                await runtime
            except asyncio.CancelledError:
                pass

        try:
            asyncio.run(scenario())
        finally:
            for target, name, value in originals:
                setattr(target, name, value)
        return captured

    def test_enabled_grok_subscription_exposes_run_coding_task_to_core(self) -> None:
        """The live Core catalogue is the inventory Claude sees."""
        captured = self._core_inventory(
            {
                "ALX_CODING_ENABLED": "true",
                "ALX_CODING_PROVIDER": "grok_subscription",
                "ALX_CODING_MODEL": "grok-4.6",
                "ALX_CODING_REVIEWER_PROVIDER": "grok_subscription",
                "ALX_CODING_REVIEWER_MODEL": "grok-4.6",
                # Coding composes only beside the repository authority that
                # can recover its branch.
                "ALX_REPOSITORY_RUNTIME_ENABLED": "true",
                "ALX_REPOSITORY_RUNTIME_ROOT": str(REPOSITORY_ROOT),
                "ALX_REPOSITORY_RUNTIME_IDENTITY": "alx-1977/AL-X",
                "ALX_REPOSITORY_RUNTIME_ORIGIN": "https://github.com/alx-1977/AL-X.git",
            }
        )
        ids = captured["ids"]
        self.assertIn(RUN_CODING_TASK, ids)
        self.assertIn(RUN_CODING_TASK, captured["catalogue"])
        self.assertIn(RUN_CODING_TASK, captured["approval_free"])

    def test_disabled_coding_is_absent_from_the_live_core_catalogue(self) -> None:
        captured = self._core_inventory({"ALX_CODING_ENABLED": "false"})
        self.assertNotIn(RUN_CODING_TASK, captured["ids"])
        self.assertNotIn(RUN_CODING_TASK, captured["catalogue"])


if __name__ == "__main__":
    unittest.main()
