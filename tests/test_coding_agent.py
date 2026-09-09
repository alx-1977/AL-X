"""CA-MVP: Core-delegated coding jobs, bounded and fail-closed, under D-028."""

from __future__ import annotations

import ast
import hashlib
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

from alx.contracts.coding import MAX_FILE_CHARACTERS, CodingError  # noqa: E402
from alx.providers.coding_process import command_permitted  # noqa: E402
from alx.providers.coding_workspace import CodingWorkspace  # noqa: E402
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


def _finish(**changes) -> dict:
    values = {
        "decision": "finish",
        "action_kind": "none",
        "path": "",
        "content": "",
        "command": [],
        "summary": "done",
        "unresolved_issues": [],
        "external_review_recommended": False,
        "status": "succeeded",
    }
    values.update(changes)
    return values


def _act(kind: str, **changes) -> dict:
    values = _finish(decision="act", action_kind=kind, status="succeeded")
    values.update(changes)
    return values


class ScriptedModel:
    def __init__(self, *outputs: dict) -> None:
        self.outputs = list(outputs)
        self.requests = []

    def complete(self, request):
        self.requests.append(request)
        if not self.outputs:
            raise AssertionError("unexpected coding-model call")
        return ModelCompletion("xai", "scripted", self.outputs.pop(0))


class Queued:
    def __init__(self, *decisions, selects: str | None = "goal-1") -> None:
        self.decisions = list(decisions)
        self.contexts = []
        self._selects = selects

    def decide(self, context):
        self.contexts.append(context)
        item = self.decisions.pop(0)
        if self._selects is not None and item.goal_id is None:
            from dataclasses import replace
            item = replace(item, goal_id=self._selects)
        return item


class CodingAgentTests(unittest.TestCase):
    def setUp(self) -> None:
        self.directory = tempfile.TemporaryDirectory()
        self.root = Path(self.directory.name)
        self.addCleanup(self.directory.cleanup)

    def _runtime(self, model) -> object:
        runtime = build_coding_runtime(True, model, lambda: "call-1")
        self.assertIsNotNone(runtime)
        return runtime

    def _broker(self, runtime):
        return CapabilityBroker(
            CapabilityRegistry(runtime.definitions),
            SafetyGate(runtime.policies),
            runtime.executors,
        )

    def _authority(self, permissions=None) -> AuthorityContext:
        return AuthorityContext(
            "friedl",
            permissions or frozenset({CODING_EXECUTE_PERMISSION}),
            NOW,
        )

    def _dispatch(self, model, arguments, permissions=None):
        runtime = self._runtime(model)
        return self._broker(runtime).dispatch(
            CapabilityCall("call-1", RUN_CODING_TASK, arguments),
            self._authority(permissions),
        )

    def test_core_dispatches_the_coding_capability_as_an_ordinary_call(self) -> None:
        """A. No special Friedl middleware command: Core selects the catalogue entry."""
        worktree = _worktree(self.root)
        model = ScriptedModel(
            _act("write_file", path="app.py", content="def add(a, b):\n    return a + b\n"),
            _act("run_command", command=["python", "-m", "unittest", "-q", "test_app"]),
            _finish(summary="fixed addition"),
        )
        runtime = self._runtime(model)
        broker = self._broker(runtime)
        store = SQLiteGoalStore(self.root / "goals.sqlite3")
        self.addCleanup(store.close)
        store.create(
            GoalState(
                "goal-1",
                Objective("turn:turn-1", "Fix the test"),
                success_criteria=(SuccessCriterion("c1", "tests pass"),),
            ),
            "conversation-1",
            RETENTION,
        )
        call = CapabilityCall(
            "call-1",
            RUN_CODING_TASK,
            {"task": "make add correct", "worktree": str(worktree)},
        )

        def dispatch(proposed, state):
            return broker.dispatch(proposed, self._authority())

        from dataclasses import replace
        reasoner = Queued(
            AgentDecision(call=call),
            AgentDecision(response="The coding job finished."),
        )
        agent = CoreAgent(
            store,
            reasoner,
            dispatch,
            runtime.definitions,
            clock=lambda: NOW,
            approval_free_capabilities=frozenset({RUN_CODING_TASK}),
        )
        outcome = agent.process(
            ConversationSnapshot(
                "conversation-1",
                (
                    ConversationTurn(
                        "conversation-1",
                        "turn-1",
                        ConversationOrigin.TYPED,
                        "the addition helper is wrong",
                        NOW,
                        "friedl",
                    ),
                ),
                1,
                RETENTION,
            ),
            RETENTION,
            3,
        )
        self.assertEqual(outcome.state, CoreState.RESPONDED)
        attempt = outcome.snapshot.state.attempts[0]
        self.assertEqual(attempt.call.capability_id, RUN_CODING_TASK)
        self.assertIs(attempt.disposition, CapabilityAttemptDisposition.EXECUTED)
        self.assertIs(attempt.result.state, CapabilityResultState.SUCCEEDED)
        self.assertEqual(reasoner.contexts[0].capabilities[0].capability_id, RUN_CODING_TASK)

    def test_failed_coding_job_can_be_cited_as_goal_evidence(self) -> None:
        """Live CA regression: FAILED run_coding_task is still attempt: evidence."""
        worktree = _worktree(self.root)
        model = ScriptedModel(
            _act("run_command", command=["python", "-m", "unittest", "-q", "test_app"]),
            _finish(summary="tests still fail", status="failed"),
        )
        runtime = self._runtime(model)
        broker = self._broker(runtime)
        store = SQLiteGoalStore(self.root / "goals.sqlite3")
        self.addCleanup(store.close)
        store.create(
            GoalState(
                "goal-1",
                Objective("turn:turn-1", "Fix the test"),
                success_criteria=(SuccessCriterion("c1", "tests pass"),),
            ),
            "conversation-1",
            RETENTION,
        )
        call = CapabilityCall(
            "call-1",
            RUN_CODING_TASK,
            {"task": "make add correct", "worktree": str(worktree)},
        )
        evidence = Evidence(
            "ev-attempt02-result",
            "coding_job",
            supports=("c1",),
            source_references=("attempt:call-1",),
        )
        reasoner = Queued(
            AgentDecision(call=call),
            AgentDecision(
                response="The coding job ran; tests still fail.",
                goal_proposal=GoalProposal(
                    GoalMutationKind.UPDATE,
                    new_evidence=(evidence,),
                ),
            ),
        )
        agent = CoreAgent(
            store,
            reasoner,
            lambda proposed, state: broker.dispatch(proposed, self._authority()),
            runtime.definitions,
            clock=lambda: NOW,
            approval_free_capabilities=frozenset({RUN_CODING_TASK}),
        )
        outcome = agent.process(
            ConversationSnapshot(
                "conversation-1",
                (
                    ConversationTurn(
                        "conversation-1",
                        "turn-1",
                        ConversationOrigin.TYPED,
                        "fix the tests",
                        NOW,
                        "friedl",
                    ),
                ),
                1,
                RETENTION,
            ),
            RETENTION,
            5,
        )
        self.assertEqual(outcome.state, CoreState.RESPONDED)
        self.assertNotEqual(outcome.reason, "goal_proposal_rejected")
        self.assertNotEqual(outcome.reason, "goal_proposal_invalid")
        attempt = outcome.snapshot.state.attempts[0]
        self.assertEqual(attempt.call.capability_id, RUN_CODING_TASK)
        self.assertIs(attempt.result.state, CapabilityResultState.FAILED)
        self.assertEqual(outcome.snapshot.state.evidence, (evidence,))
        self.assertIs(outcome.snapshot.state.status, GoalStatus.ACTIVE)

    def test_edits_stay_inside_the_assigned_worktree(self) -> None:
        """B. A fixture job cannot modify a sibling tree."""
        assigned = _worktree(self.root, "assigned")
        other = _worktree(self.root, "other")
        original = (other / "app.py").read_text(encoding="utf-8")
        model = ScriptedModel(
            _act(
                "write_file",
                path="../other/app.py",
                content="escaped",
            ),
            _act("write_file", path="app.py", content="def add(a, b):\n    return a + b\n"),
            _finish(summary="stayed inside"),
        )
        attempt = self._dispatch(
            model,
            {"task": "fix add", "worktree": str(assigned)},
        )
        self.assertIs(attempt.result.state, CapabilityResultState.SUCCEEDED)
        self.assertEqual((other / "app.py").read_text(encoding="utf-8"), original)
        self.assertIn("return a + b", (assigned / "app.py").read_text(encoding="utf-8"))

    def test_runs_a_relevant_test_and_returns_the_result(self) -> None:
        """C. The job runs the relevant unit tests and reports the result."""
        worktree = _worktree(self.root)
        model = ScriptedModel(
            _act("write_file", path="app.py", content="def add(a, b):\n    return a + b\n"),
            _act("run_command", command=["python", "-m", "unittest", "-q", "test_app"]),
            _finish(summary="tests passed"),
        )
        attempt = self._dispatch(
            model,
            {
                "task": "fix add",
                "worktree": str(worktree),
                "test_guidance": "python -m unittest -q test_app",
            },
        )
        self.assertTrue(attempt.result.values["tests_run"])
        self.assertTrue(attempt.result.values["tests_passed"])
        commands = attempt.result.values["commands"]
        self.assertTrue(
            any(list(item["argv"][:3]) == ["python", "-m", "unittest"] for item in commands)
        )
        self.assertEqual(commands[-1]["exit_status"], 0)

    def test_core_receives_structured_evidence(self) -> None:
        """D. Files, commands, tests and git evidence are in the result."""
        worktree = _worktree(self.root)
        model = ScriptedModel(
            _act("write_file", path="app.py", content="def add(a, b):\n    return a + b\n"),
            _act("run_command", command=["python", "-m", "unittest", "-q", "test_app"]),
            _finish(summary="addition now returns the sum"),
        )
        attempt = self._dispatch(
            model, {"task": "fix add", "worktree": str(worktree)}
        )
        values = attempt.result.values
        self.assertEqual(values["status"], "succeeded")
        self.assertIn("app.py", values["files_changed"])
        self.assertTrue(values["commands"])
        self.assertIn("app.py", values["git_status"])
        self.assertIn("return a + b", values["git_diff"])
        self.assertEqual(
            values["diff_digest"],
            hashlib.sha256(values["git_diff"].encode("utf-8")).hexdigest(),
        )
        self.assertNotIn("return a + b", repr(attempt.result.durable_values))

    def test_cannot_push_merge_deploy_or_request_review(self) -> None:
        """E. Forbidden actions are refused and do not happen."""
        worktree = _worktree(self.root)
        forbidden = (
            ["git", "push"],
            ["git", "merge", "main"],
            ["git", "commit", "-am", "x"],
            ["gh", "pr", "create"],
            ["curl", "https://example.invalid/review"],
        )
        for argv in forbidden:
            with self.subTest(argv=argv):
                self.assertFalse(command_permitted(argv))
        model = ScriptedModel(
            _act("run_command", command=["git", "push"]),
            _act("run_command", command=["git", "merge", "other"]),
            _finish(status="failed", summary="could not push", unresolved_issues=["push refused"]),
        )
        attempt = self._dispatch(
            model, {"task": "ship it", "worktree": str(worktree)}
        )
        self.assertIs(attempt.result.state, CapabilityResultState.FAILED)
        self.assertEqual(attempt.result.failure["code"], "task_failed")
        self.assertFalse(all(item["permitted"] for item in attempt.result.values["commands"]))
        source = (PRODUCTION_ROOT / "providers" / "coding_agent.py").read_text()
        self.assertNotIn("request_external_review", source)
        self.assertNotIn("merge_pull_request", source)
        self.assertNotIn("run_sandbox_experiment", source)

    def test_a_failed_job_does_not_claim_success(self) -> None:
        """F. Failed tests return structured failure, not succeeded."""
        worktree = _worktree(self.root)
        model = ScriptedModel(
            _act("run_command", command=["python", "-m", "unittest", "-q", "test_app"]),
            _finish(status="succeeded", summary="all good"),
        )
        attempt = self._dispatch(
            model, {"task": "fix add", "worktree": str(worktree)}
        )
        self.assertIs(attempt.result.state, CapabilityResultState.FAILED)
        self.assertEqual(attempt.result.values["status"], "failed")
        self.assertIs(attempt.result.values["tests_passed"], False)
        self.assertEqual(attempt.result.failure["code"], "task_failed")

    def test_without_permission_nothing_runs(self) -> None:
        worktree = _worktree(self.root)
        model = ScriptedModel(_finish())
        attempt = self._dispatch(
            model,
            {"task": "fix add", "worktree": str(worktree)},
            permissions=frozenset({"sandbox.execute"}),
        )
        self.assertIs(attempt.disposition, CapabilityAttemptDisposition.REJECTED)
        self.assertEqual(attempt.reason_code, "permission_missing")
        self.assertFalse(model.requests)

    def test_unusable_worktree_fails_closed(self) -> None:
        model = ScriptedModel(_finish())
        attempt = self._dispatch(
            model,
            {"task": "fix add", "worktree": str(self.root / "missing")},
        )
        self.assertIs(attempt.result.state, CapabilityResultState.FAILED)
        self.assertEqual(attempt.result.failure["code"], "worktree_unusable")

    def test_coding_requests_are_not_core_or_subscription_calls(self) -> None:
        worktree = _worktree(self.root)
        model = ScriptedModel(_finish(summary="nothing to do", status="blocked"))
        self._dispatch(model, {"task": "inspect", "worktree": str(worktree)})
        self.assertEqual(model.requests[0].kind, "coding")

    def test_capability_is_absent_when_disabled(self) -> None:
        self.assertIsNone(build_coding_runtime(False, ScriptedModel(), lambda: "c"))

    def test_mutation_removing_the_git_allowlist_is_detected(self) -> None:
        """G. The push/merge refusal is load-bearing."""
        source = CODING_PROCESS.read_text(encoding="utf-8")
        mutated = source.replace(
            "        if not rest or rest[0] not in _GIT_INSPECT:\n"
            "            return False\n"
            "        allowed = _GIT_FLAGS[rest[0]]\n"
            "        return all(item in allowed for item in rest[1:])",
            "        return True",
        )
        self.assertNotEqual(source, mutated)
        namespace: dict[str, object] = {}
        exec(compile(mutated, str(CODING_PROCESS), "exec"), namespace)
        permitted = namespace["command_permitted"]
        self.assertTrue(permitted(["git", "push"]))
        self.assertFalse(command_permitted(["git", "push"]))

    def test_mutation_removing_the_worktree_bound_is_detected(self) -> None:
        workspace_path = PRODUCTION_ROOT / "providers" / "coding_workspace.py"
        source = workspace_path.read_text(encoding="utf-8")
        mutated = source.replace(
            "            raise CodingError(\"path_outside_worktree\") from error\n",
            "            pass\n",
            1,
        )
        self.assertNotEqual(source, mutated)
        namespace: dict[str, object] = {"__name__": "mutation"}
        exec(compile(mutated, str(workspace_path), "exec"), namespace)
        workspace = namespace["CodingWorkspace"](str(_worktree(self.root)))
        escaped = workspace.resolve("../secret.txt")
        self.assertFalse(str(escaped).startswith(str(workspace.root)))

    def test_git_cannot_write_or_point_outside_the_worktree(self) -> None:
        worktree = _worktree(self.root)
        attacks = (
            ["git", "diff", "--output=../escaped"],
            ["git", "diff", "--output", "../escaped"],
            ["git", "status", "--work-tree=/tmp"],
            ["git", "log", "--git-dir=/tmp/other.git"],
        )
        for argv in attacks:
            with self.subTest(argv=argv):
                self.assertFalse(command_permitted(argv, worktree))

    def test_pytest_cannot_load_plugins_or_escape_with_parent_paths(self) -> None:
        worktree = _worktree(self.root)
        self.assertTrue(
            command_permitted(
                ["python", "-m", "unittest", "-q", "test_app"], worktree
            )
        )
        self.assertFalse(
            command_permitted(
                ["python", "-m", "pytest", "-p", "evilplugin"], worktree
            )
        )
        self.assertFalse(
            command_permitted(
                ["python", "-m", "pytest", "pkg/../../outside"], worktree
            )
        )
        self.assertFalse(
            command_permitted(
                ["python", "-m", "pytest", "--rootdir", "/tmp"], worktree
            )
        )
        self.assertFalse(
            command_permitted(
                ["python", "-m", "pytest", "-c", "pytest.ini"], worktree
            )
        )
        self.assertFalse(
            command_permitted(
                ["python", "-m", "pytest", "--pyargs", "os"], worktree
            )
        )

    def test_mutation_allowing_git_output_is_detected(self) -> None:
        worktree = _worktree(self.root)
        source = CODING_PROCESS.read_text(encoding="utf-8")
        mutated = source.replace(
            "        allowed = _GIT_FLAGS[rest[0]]\n"
            "        return all(item in allowed for item in rest[1:])",
            "        return True",
        )
        self.assertNotEqual(source, mutated)
        namespace: dict[str, object] = {}
        exec(compile(mutated, str(CODING_PROCESS), "exec"), namespace)
        permitted = namespace["command_permitted"]
        self.assertTrue(
            permitted(["git", "diff", "--output=../escaped"], worktree)
        )
        self.assertFalse(
            command_permitted(["git", "diff", "--output=../escaped"], worktree)
        )

    def test_provider_failure_keeps_accumulated_evidence(self) -> None:
        worktree = _worktree(self.root)

        class FailAfterWrite(ScriptedModel):
            def complete(self, request):
                if self.outputs:
                    return super().complete(request)
                raise RuntimeError("provider down")

        model = FailAfterWrite(
            _act("write_file", path="app.py", content="def add(a, b):\n    return a + b\n"),
        )
        attempt = self._dispatch(
            model, {"task": "fix add", "worktree": str(worktree)}
        )
        self.assertIs(attempt.result.state, CapabilityResultState.FAILED)
        self.assertEqual(attempt.result.failure["code"], "provider_failed")
        self.assertIn("app.py", attempt.result.values["files_changed"])
        self.assertIn("app.py", attempt.result.values["git_status"])
        self.assertTrue(attempt.result.values["git_diff"])

    def test_a_later_passing_test_cannot_hide_an_earlier_failure(self) -> None:
        worktree = _worktree(self.root)
        (worktree / "test_ok.py").write_text(
            "import unittest\n\n\nclass OkTests(unittest.TestCase):\n"
            "    def test_ok(self):\n        self.assertTrue(True)\n",
            encoding="utf-8",
        )
        model = ScriptedModel(
            _act("run_command", command=["python", "-m", "unittest", "-q", "test_app"]),
            _act("run_command", command=["python", "-m", "unittest", "-q", "test_ok"]),
            _finish(status="succeeded", summary="narrow tests passed"),
        )
        attempt = self._dispatch(
            model, {"task": "fix add", "worktree": str(worktree)}
        )
        self.assertIs(attempt.result.state, CapabilityResultState.FAILED)
        self.assertIs(attempt.result.values["tests_passed"], False)

    def test_nested_env_and_git_paths_cannot_be_written(self) -> None:
        worktree = _worktree(self.root)
        workspace = CodingWorkspace(str(worktree))
        with self.assertRaises(CodingError) as raised:
            workspace.write_text("service/.env", "SECRET=1\n")
        self.assertEqual(raised.exception.code, "path_not_permitted")
        with self.assertRaises(CodingError) as raised:
            workspace.write_text(".ENV", "SECRET=1\n")
        self.assertEqual(raised.exception.code, "path_not_permitted")
        with self.assertRaises(CodingError) as raised:
            workspace.write_text("vendor/.git/config", "[core]\n")
        self.assertEqual(raised.exception.code, "path_not_permitted")
        self.assertFalse((worktree / "service" / ".env").exists())

    def test_oversized_reads_are_refused_instead_of_truncated(self) -> None:
        worktree = _worktree(self.root)
        huge = worktree / "huge.py"
        huge.write_text("x" * (MAX_FILE_CHARACTERS + 1), encoding="utf-8")
        workspace = CodingWorkspace(str(worktree))
        with self.assertRaises(CodingError) as raised:
            workspace.read_text("huge.py")
        self.assertEqual(raised.exception.code, "file_too_large")
        self.assertEqual(len(huge.read_text(encoding="utf-8")), MAX_FILE_CHARACTERS + 1)

    def test_command_cap_is_not_reported_as_step_budget(self) -> None:
        worktree = _worktree(self.root)
        model = ScriptedModel(
            _act("run_command", command=["git", "status", "--porcelain"]),
            _act("run_command", command=["git", "diff"]),
            _finish(summary="should not be asked"),
        )
        with patch.object(coding_agent_module, "MAX_REPORTED_COMMANDS", 1):
            attempt = self._dispatch(
                model, {"task": "inspect", "worktree": str(worktree), "step_budget": 8}
            )
        self.assertEqual(
            attempt.result.failure["code"], "command_budget_exhausted"
        )
        self.assertIn(
            "command_budget_exhausted", attempt.result.values["unresolved_issues"]
        )

    def test_one_coding_process_site(self) -> None:
        source = CODING_PROCESS.read_text(encoding="utf-8")
        tree = ast.parse(source)
        process_attrs = [
            node.attr
            for node in ast.walk(tree)
            if isinstance(node, ast.Attribute)
            and isinstance(node.value, ast.Name)
            and node.value.id == "subprocess"
        ]
        self.assertEqual(
            [name for name in process_attrs if name == "run"],
            ["run"],
        )
        self.assertNotIn("Popen", process_attrs)
        self.assertIn("shell=False", source)

    def test_coding_and_sandbox_stay_separate_outcomes(self) -> None:
        for path in PRODUCTION_ROOT.rglob("*sandbox*.py"):
            text = path.read_text(encoding="utf-8")
            self.assertNotIn("run_coding_task", text)
            self.assertNotIn("coding.execute", text)
        agent = (PRODUCTION_ROOT / "providers" / "coding_agent.py").read_text()
        self.assertNotIn("run_sandbox_experiment", agent)
        self.assertNotIn("sandbox.execute", agent)


if __name__ == "__main__":
    unittest.main()
