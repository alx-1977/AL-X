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
    MAX_FILE_CHARACTERS,
    MAX_STEP_BUDGET,
    MAX_TASK_CHARACTERS,
    CodingError,
)
from alx.providers.coding_process import command_permitted  # noqa: E402
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


class ScriptedModel:
    def __init__(self, *outputs: dict) -> None:
        self.outputs = list(outputs)
        self.requests = []

    def complete(self, request):
        self.requests.append(request)
        if request.output_schema_name == "alx_coding_plan":
            if self.outputs and "problem_understanding" in self.outputs[0]:
                return ModelCompletion("xai", "scripted", self.outputs.pop(0))
            return ModelCompletion("xai", "scripted", _plan())
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

    def test_plan_precedes_execution_and_receives_operation_contract(self) -> None:
        worktree = _worktree(self.root)
        model = ScriptedModel(
            _plan(inspection_targets=["app.py"]),
            _act("write_file", path="app.py", content="def add(a, b):\n    return a + b\n"),
            _act("run_command", command=["python", "-m", "unittest", "-q", "test_app"]),
            _finish(summary="fixed addition"),
        )
        attempt = self._dispatch(
            model,
            {"task": "Fix the addition helper from a normal coding request.",
             "worktree": str(worktree), "blocked_paths": ["private"]},
        )
        self.assertIs(attempt.result.state, CapabilityResultState.SUCCEEDED)
        self.assertEqual(model.requests[0].output_schema_name, "alx_coding_plan")
        plan_material = json.loads(model.requests[0].messages[1].content)
        self.assertIn("write_file", plan_material["operation_contract"])
        self.assertIn("generic shell", plan_material["operation_contract"]["refused"])
        self.assertEqual(plan_material["blocked_paths"], ["private"])
        self.assertIn("app.py", plan_material["worktree_entries"])
        self.assertEqual(model.requests[1].output_schema_name, "alx_coding_decision")
        self.assertTrue(attempt.result.values["plan_summary"])

    def test_blocked_paths_refuse_planning_inspection_before_execution(self) -> None:
        worktree = _worktree(self.root)
        model = ScriptedModel(_plan(inspection_targets=["secret/config.py"]))
        attempt = self._dispatch(
            model,
            {"task": "inspect configuration", "worktree": str(worktree),
             "blocked_paths": ["secret"]},
        )
        self.assertIs(attempt.result.state, CapabilityResultState.FAILED)
        self.assertEqual(attempt.result.failure["reason_code"], "path_not_permitted")
        self.assertEqual(len(model.requests), 1)

    def test_refused_shell_command_can_be_corrected_with_bounded_operation(self) -> None:
        worktree = _worktree(self.root)
        model = ScriptedModel(
            _plan(),
            _act("run_command", command=["sh", "-c", "pytest"]),
            _act("write_file", path="app.py", content="def add(a, b):\n    return a + b\n"),
            _act("run_command", command=["python", "-m", "unittest", "-q", "test_app"]),
            _finish(summary="used the permitted test operation"),
        )
        attempt = self._dispatch(
            model, {"task": "fix addition", "worktree": str(worktree)}
        )
        self.assertIs(attempt.result.state, CapabilityResultState.SUCCEEDED)
        self.assertFalse(attempt.result.values["commands"][0]["permitted"])
        execution_material = json.loads(model.requests[2].messages[1].content)
        self.assertIn("error:command_not_permitted", execution_material["observations"])

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

    def test_preexisting_dirty_blocked_file_is_not_listed_as_job_change(self) -> None:
        """Live CA listed tests/test_coding_agent.py because git status was already dirty."""
        worktree = _worktree(self.root)
        blocked = worktree / "tests" / "test_coding_agent.py"
        blocked.parent.mkdir()
        blocked.write_text("tracked\n", encoding="utf-8")
        _git(worktree, "add", "tests/test_coding_agent.py")
        _git(worktree, "commit", "-m", "blocked fixture")
        blocked.write_text("already-dirty\n", encoding="utf-8")
        model = ScriptedModel(
            _act("write_file", path="helper.py", content="ok\n"),
            _finish(summary="wrote helper", status="succeeded"),
        )
        attempt = self._dispatch(
            model,
            {
                "task": "add helper",
                "worktree": str(worktree),
                "blocked_paths": ["tests/test_coding_agent.py"],
            },
        )
        values = attempt.result.values
        self.assertIn("helper.py", values["files_changed"])
        self.assertNotIn("tests/test_coding_agent.py", values["files_changed"])
        self.assertIn("tests/test_coding_agent.py", values["preexisting_dirty"])
        self.assertEqual(blocked.read_text(encoding="utf-8"), "already-dirty\n")

    def test_provider_failed_keeps_evidence_and_cli_reason(self) -> None:
        worktree = _worktree(self.root)
        blocked = worktree / "tests" / "test_coding_agent.py"
        blocked.parent.mkdir()
        blocked.write_text("tracked\n", encoding="utf-8")
        _git(worktree, "add", "tests/test_coding_agent.py")
        _git(worktree, "commit", "-m", "blocked fixture")
        blocked.write_text("already-dirty\n", encoding="utf-8")

        class Boom:
            def complete(self, request):
                raise ProviderError(
                    "grok_subscription",
                    "cli_failed",
                    {"exit_status": 1, "stderr_characters": 40, "stdout_characters": 0},
                )

        attempt = self._dispatch(
            Boom(),
            {
                "task": "fix continuation",
                "worktree": str(worktree),
                "blocked_paths": ["tests/test_coding_agent.py"],
            },
        )
        failure = attempt.result.failure
        self.assertEqual(failure["code"], "provider_failed")
        self.assertEqual(failure["reason_code"], "cli_failed")
        self.assertEqual(failure["exit_status"], 1)
        self.assertEqual(failure["stderr_characters"], 40)
        self.assertNotIn("tests/test_coding_agent.py", attempt.result.values["files_changed"])
        self.assertIn(
            "tests/test_coding_agent.py",
            attempt.result.values["preexisting_dirty"],
        )
        self.assertFalse(attempt.result.values["tests_run"])

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
        missing = self.root / "missing"
        attempt = self._dispatch(
            model,
            {"task": "fix add", "worktree": str(missing)},
        )
        self.assertIs(attempt.result.state, CapabilityResultState.FAILED)
        failure = attempt.result.failure
        self.assertEqual(failure["code"], "worktree_unusable")
        self.assertEqual(failure["reason_code"], "missing")
        self.assertIn("resolved", failure)

    def test_live_github_repo_name_is_not_a_worktree(self) -> None:
        """call-coding-agent-defect-fix-retry3 sent worktree 'AL-X', not the repo path."""
        empty = self.root / "empty-cwd"
        empty.mkdir()
        model = ScriptedModel(_finish())
        previous = Path.cwd()
        try:
            os.chdir(empty)
            attempt = self._dispatch(
                model,
                {
                    "task": "Investigate and fix the email-continuation defect.",
                    "worktree": "AL-X",
                    "blocked_paths": ["tests/test_coding_agent.py"],
                    "step_budget": 32,
                },
            )
        finally:
            os.chdir(previous)
        failure = attempt.result.failure
        self.assertEqual(failure["code"], "worktree_unusable")
        self.assertEqual(failure["reason_code"], "missing")
        self.assertEqual(failure["received"], "AL-X")
        self.assertTrue(str(failure["resolved"]).endswith("AL-X"))
        self.assertFalse((empty / "AL-X").is_dir())
        self.assertFalse(model.requests)
        self.assertEqual(attempt.result.values, {})

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
            "        if part == \"..\":\n"
            "            if not parts:\n"
            "                raise CodingError(\"path_outside_worktree\")\n"
            "            parts.pop()\n"
            "            continue\n",
            "        parts.append(part)\n",
        )
        self.assertNotIn('if part == ".."', mutated)
        mutated = mutated.replace(
            "            raise CodingError(\"path_outside_worktree\") from error\n",
            "            pass\n",
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

    def test_live_step_budget_sixty_names_the_invalid_field(self) -> None:
        """call-ca-retry-2/3/4: Core sent step_budget 60; max is 32."""
        worktree = _worktree(self.root)
        task = (
            "Investigate and fix the defect that let AL/X's Core agent loop "
            "stop working on an active multi-step goal after completing only "
            "one step, while the goal remained active, and then report that "
            "work was still in progress when nothing was actually running. "
            "Do not modify tests/test_coding_agent.py."
        )
        attempt = self._dispatch(
            ScriptedModel(),
            {
                "task": task,
                "worktree": str(worktree),
                "step_budget": 60,
            },
        )
        failure = attempt.result.failure
        self.assertEqual(failure["code"], "arguments_unusable")
        self.assertEqual(failure["invalid_field"], "step_budget")
        self.assertEqual(failure["reason_code"], "out_of_range")
        self.assertEqual(failure["received"], 60)
        self.assertIn(str(MAX_STEP_BUDGET), failure["detail"])
        self.assertNotIn(task, str(dict(failure)))
        self.assertEqual(attempt.result.values, {})

    def test_live_required_fields_only_still_run(self) -> None:
        """call-ca-retry-1 shape: task and worktree alone must not be unusable."""
        worktree = _worktree(self.root)
        attempt = self._dispatch(
            ScriptedModel(_finish(summary="blocked", status="blocked")),
            {
                "task": "Diagnose the goal-continuation defect.",
                "worktree": str(worktree),
            },
        )
        self.assertNotEqual(
            attempt.result.failure.get("code") if attempt.result.failure else None,
            "arguments_unusable",
        )

    def test_oversized_task_reports_length_not_content(self) -> None:
        worktree = _worktree(self.root)
        task = "x" * (MAX_TASK_CHARACTERS + 1)
        attempt = self._dispatch(
            ScriptedModel(),
            {"task": task, "worktree": str(worktree)},
        )
        failure = attempt.result.failure
        self.assertEqual(failure["code"], "arguments_unusable")
        self.assertEqual(failure["invalid_field"], "task")
        self.assertEqual(failure["reason_code"], "too_long")
        self.assertEqual(failure["received_length"], MAX_TASK_CHARACTERS + 1)
        self.assertNotIn(task, str(dict(failure)))


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

    def test_blocked_test_file_cannot_be_written(self) -> None:
        workspace = self._workspace(("tests/test_coding_agent.py",))
        with self.assertRaises(CodingError) as raised:
            workspace.write_text("tests/test_coding_agent.py", "changed\n")
        self.assertEqual(raised.exception.code, "path_not_permitted")
        self.assertEqual(
            (self.repo / "tests" / "test_coding_agent.py").read_text(encoding="utf-8"),
            "original\n",
        )

    def test_blocked_test_file_cannot_be_read(self) -> None:
        workspace = self._workspace(("tests/test_coding_agent.py",))
        with self.assertRaises(CodingError) as raised:
            workspace.read_text("tests/test_coding_agent.py")
        self.assertEqual(raised.exception.code, "path_not_permitted")

    def test_blocking_tests_directory_blocks_descendants(self) -> None:
        workspace = self._workspace(("tests",))
        with self.assertRaises(CodingError) as raised:
            workspace.read_text("tests/test_other.py")
        self.assertEqual(raised.exception.code, "path_not_permitted")
        with self.assertRaises(CodingError):
            workspace.write_text("tests/new.py", "nope\n")
        with self.assertRaises(CodingError):
            workspace.list_dir("tests")

    def test_parent_traversal_cannot_bypass_a_blocked_file(self) -> None:
        workspace = self._workspace(("tests/test_coding_agent.py",))
        with self.assertRaises(CodingError) as raised:
            workspace.write_text(
                "tests/../tests/test_coding_agent.py", "changed\n"
            )
        self.assertEqual(raised.exception.code, "path_not_permitted")
        self.assertEqual(
            (self.repo / "tests" / "test_coding_agent.py").read_text(encoding="utf-8"),
            "original\n",
        )

    def test_symlink_to_a_blocked_file_is_refused(self) -> None:
        alias = self.repo / "alias.py"
        alias.symlink_to(self.repo / "tests" / "test_coding_agent.py")
        workspace = self._workspace(("tests/test_coding_agent.py",))
        with self.assertRaises(CodingError) as raised:
            workspace.read_text("alias.py")
        self.assertEqual(raised.exception.code, "path_not_permitted")
        with self.assertRaises(CodingError):
            workspace.write_text("alias.py", "changed\n")

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

    def test_ordinary_unrelated_files_remain_accessible(self) -> None:
        workspace = self._workspace(("tests/test_coding_agent.py",))
        self.assertEqual(workspace.read_text("app.py"), "safe\n")
        self.assertEqual(workspace.write_text("app.py", "still-safe\n"), "app.py")
        self.assertEqual(workspace.read_text("tests/test_other.py"), "other\n")

    def test_env_and_git_blocks_remain_independent(self) -> None:
        workspace = self._workspace(("tests/test_coding_agent.py",))
        (self.repo / ".env").write_text("secret=1\n", encoding="utf-8")
        with self.assertRaises(CodingError) as raised:
            workspace.read_text(".env")
        self.assertEqual(raised.exception.code, "path_not_permitted")

    def test_capability_refuses_a_write_to_the_blocked_file(self) -> None:
        model = ScriptedModel(
            _act(
                "write_file",
                path="tests/test_coding_agent.py",
                content="changed\n",
            ),
            _finish(summary="stopped", status="blocked"),
        )
        runtime = build_coding_runtime(True, model, lambda: "call-1")
        broker = CapabilityBroker(
            CapabilityRegistry(runtime.definitions),
            SafetyGate(runtime.policies),
            runtime.executors,
        )
        attempt = broker.dispatch(
            CapabilityCall(
                "call-1",
                RUN_CODING_TASK,
                {
                    "task": "fix continuation",
                    "worktree": str(self.repo),
                    "blocked_paths": ["tests/test_coding_agent.py"],
                },
            ),
            AuthorityContext(
                "friedl",
                frozenset({CODING_EXECUTE_PERMISSION}),
                NOW,
            ),
        )
        self.assertIs(attempt.result.state, CapabilityResultState.FAILED)
        self.assertEqual(
            (self.repo / "tests" / "test_coding_agent.py").read_text(encoding="utf-8"),
            "original\n",
        )
        self.assertNotIn(
            "tests/test_coding_agent.py",
            attempt.result.values.get("files_changed") or [],
        )

    def test_mutation_dropping_scope_checks_is_detected(self) -> None:
        source = (
            PRODUCTION_ROOT / "providers" / "coding_workspace.py"
        ).read_text(encoding="utf-8")
        mutated = source.replace(
            "        if self._blocked_path(resolved) or self._scope_blocked(relative, resolved):\n"
            "            raise CodingError(\"path_not_permitted\")\n",
            "        return\n",
            1,
        )
        self.assertNotEqual(source, mutated)
        namespace: dict[str, object] = {"__name__": "mutation"}
        exec(compile(mutated, "coding_workspace.py", "exec"), namespace)
        workspace = namespace["CodingWorkspace"](
            str(self.repo), ("tests/test_coding_agent.py",)
        )
        workspace.write_text("tests/test_coding_agent.py", "leaked\n")
        self.assertEqual(
            (self.repo / "tests" / "test_coding_agent.py").read_text(encoding="utf-8"),
            "leaked\n",
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
