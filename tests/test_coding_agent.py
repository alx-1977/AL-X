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
from datetime import UTC, datetime
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
    DEFAULT_VERIFICATION_COMMAND_SECONDS,
    MAX_STEP_BUDGET,
    MAX_TASK_CHARACTERS,
    CodingCommandRecord,
    CodingError,
    CodingRequest,
    CodingSessionResult,
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
from alx.tools.coding import RUN_CODING_TASK  # noqa: E402


NOW = datetime(2026, 9, 9, tzinfo=UTC)
RETENTION = datetime(2027, 9, 9, tzinfo=UTC)
PRODUCTION_ROOT = REPOSITORY_ROOT / "src" / "alx"
CODING_PROCESS = PRODUCTION_ROOT / "providers" / "coding_process.py"


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
    _git(root, "init")
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

    def __init__(self, plan: dict | None = None, error: Exception | None = None) -> None:
        self._plan = plan if plan is not None else _plan()
        self._error = error
        self.requests = []

    def complete(self, request):
        self.requests.append(request)
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

    def _run(self, model, session, **arguments):
        runtime = build_coding_runtime(
            True, model, lambda: "call-1", session=session
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
                order.append("plan")
                return super().complete(request)

        class OrderedSession(RecordingSession):
            def run_session(self, request, briefing):
                order.append("session")
                return super().run_session(request, briefing)

        worktree = _worktree(self.root)
        session = OrderedSession(edits={"app.py": _FIXED})
        self._run(
            OrderedModel(), session, task="fix add", worktree=str(worktree)
        )
        self.assertEqual(order, ["plan", "session"])

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

    def test_the_session_receives_the_real_worktree_and_the_plan(self) -> None:
        """2. Grok's cwd is the assigned worktree, not a synthetic path."""
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
        self.assertEqual(Path(request.worktree), worktree)
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
        """13. Tests are run by AL/X after the session, through the allowlist."""
        worktree = _worktree(self.root)
        session = RecordingSession(edits={"app.py": _FIXED})
        attempt = self._run(
            PlanningModel(),
            session,
            task="fix add",
            worktree=str(worktree),
            test_guidance="python -m unittest -q test_app",
        )
        values = attempt.result.values
        self.assertTrue(values["tests_run"])
        self.assertTrue(values["tests_passed"])
        self.assertEqual(attempt.result.state, CapabilityResultState.SUCCEEDED)
        argv = tuple(values["commands"][0]["argv"])
        self.assertEqual(argv[:3], ("python", "-m", "unittest"))

    def test_changed_test_modules_are_preferred_to_the_full_suite(self) -> None:
        """The native session's own regression is the first test evidence."""
        worktree = _worktree(self.root)
        agent = coding_agent_module.CodingAgent(PlanningModel(), RecordingSession())
        commands = agent._verification_commands(
            CodingRequest(task="fix add", worktree=str(worktree)),
            _plan(),
            ("app.py", "test_app.py"),
        )
        self.assertEqual(
            commands,
            (("python", "-m", "pytest", "-q", "test_app.py"),),
        )
        self.assertNotIn(
            ("python", "-m", "pytest", "-q", "-p", "no:cacheprovider"),
            commands,
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
        self.assertEqual(
            tuple(attempt.result.values["commands"][0]["argv"]),
            ("python", "-m", "pytest", "-q", "test_app.py"),
        )

    def test_verification_timeout_remains_failed_bounded_evidence(self) -> None:
        """A realistic bound does not turn a genuine timeout into success."""
        worktree = _worktree(self.root)
        session = RecordingSession(edits={"app.py": _FIXED})

        def timed_out(argv, *_args, **kwargs):
            self.assertEqual(
                kwargs["timeout_seconds"], DEFAULT_VERIFICATION_COMMAND_SECONDS
            )
            return CodingCommandRecord(tuple(argv), -1, "partial", "", True, True)

        with patch.object(coding_agent_module, "run_permitted_command", timed_out):
            attempt = self._run(
                PlanningModel(), session, task="fix add", worktree=str(worktree),
                test_guidance="python -m unittest -q test_app",
            )
        self.assertEqual(attempt.result.state, CapabilityResultState.FAILED)
        self.assertFalse(attempt.result.values["tests_passed"])
        command = attempt.result.values["commands"][0]
        self.assertTrue(command["timed_out"])
        self.assertEqual(command["stdout"], "partial")

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
        worktree = _worktree(self.root)
        session = RecordingSession(edits={}, report="nothing needed")
        attempt = self._run(
            PlanningModel(), session, task="fix add", worktree=str(worktree)
        )
        self.assertEqual(attempt.result.state, CapabilityResultState.FAILED)
        self.assertIn("no_files_changed", attempt.result.values["unresolved_issues"])

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
        """15. Work already in the tree is not claimed as this job's."""
        worktree = _worktree(self.root)
        (worktree / "unrelated.py").write_text("already dirty\n", encoding="utf-8")
        session = RecordingSession(edits={"app.py": _FIXED})
        attempt = self._run(
            PlanningModel(), session, task="fix add", worktree=str(worktree)
        )
        values = attempt.result.values
        self.assertIn("unrelated.py", values["preexisting_dirty"])
        self.assertNotIn("unrelated.py", values["files_changed"])
        self.assertIn("app.py", values["files_changed"])

    def test_capability_is_unregistered_without_a_session(self) -> None:
        """A plan with nothing to execute it is honest absence, not a failure."""
        self.assertIsNone(
            build_coding_runtime(True, PlanningModel(), lambda: "call-1")
        )


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
        request = CodingRequest(task="t", worktree=str(self.root))
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
            CodingRequest(task="t", worktree=str(self.root)), "briefing"
        )
        self.assertFalse(result.completed)
        self.assertEqual(result.failure_code, "session_failed")


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
                CodingRequest(task="t", worktree=str(self.root)), "briefing"
            )
        self.assertEqual(raised.exception.code, "session_failed")
        self.assertEqual(
            raised.exception.details["reason_code"], "session_timeout"
        )

    def test_a_timeout_preserves_plan_and_dirty_state_evidence(self) -> None:
        """Evidence accumulated before the timeout still reaches Core."""
        worktree = _worktree(self.root)
        (worktree / "already.py").write_text("dirty\n", encoding="utf-8")
        session = RecordingSession(
            raises=CodingError("session_failed", reason_code="session_timeout")
        )
        runtime = build_coding_runtime(
            True, PlanningModel(), lambda: "call-1", session=session
        )
        attempt = CapabilityBroker(
            CapabilityRegistry(runtime.definitions),
            SafetyGate(runtime.policies),
            runtime.executors,
        ).dispatch(
            CapabilityCall(
                "call-1", RUN_CODING_TASK,
                {"task": "fix add", "worktree": str(worktree)},
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
        self.assertIn("already.py", values["preexisting_dirty"])
        self.assertTrue(values["plan_summary"])
        self.assertEqual(tuple(values["files_changed"]), ())

    def test_a_timeout_is_not_retried(self) -> None:
        worktree = _worktree(self.root)
        session = RecordingSession(
            raises=CodingError("session_failed", reason_code="session_timeout")
        )
        runtime = build_coding_runtime(
            True, PlanningModel(), lambda: "call-1", session=session
        )
        CapabilityBroker(
            CapabilityRegistry(runtime.definitions),
            SafetyGate(runtime.policies),
            runtime.executors,
        ).dispatch(
            CapabilityCall(
                "call-1", RUN_CODING_TASK,
                {"task": "fix add", "worktree": str(worktree)},
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
        module = coding_agent_module
        agent = module.CodingAgent(PlanningModel(), RecordingSession())
        for removed in ("_act", "_ask"):
            self.assertFalse(
                hasattr(agent, removed),
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
