"""Coding jobs, git, pull requests and reviews as one capability (D-074).

Ten capabilities (run_coding_task, stop_coding_job, repository_operation,
read_pull_request_checks, request_external_review and its beyond-allowance
twin, read_external_review, merge_pull_request, restart_self and
run_sandbox_experiment) became `code`, with an action per job. Each action
keeps its own contract, permission and approval rule; the old capabilities
are removed, not hidden (Law 0).
"""

from __future__ import annotations

import ast
import sys
import unittest
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from alx.bootstrap.code import CodePart, build_code_runtime  # noqa: E402
from alx.capabilities import CapabilityBroker, CapabilityRegistry  # noqa: E402
from alx.contracts import (  # noqa: E402
    CODE, CapabilityCall, CapabilityDefinition, CapabilityResult, CapabilityResultState,
    CodeAction, SideEffect, StructuredSchema, ValueKind, recorded_code_action,
)
from alx.contracts.records import ExecutionOutcome  # noqa: E402
from alx.core.model_reasoner import _one_line  # noqa: E402
from alx.safety import AuthorityContext, SafetyGate  # noqa: E402
from alx.tools.code import CONTRACTS  # noqa: E402
from alx.tools.worker import WORKER_CAPABILITIES  # noqa: E402
from code_actions import (  # noqa: E402
    EXPERIMENT, GIT, MERGE, READ_CHECKS, READ_REVIEW, REQUEST_REVIEW,
    REQUEST_REVIEW_BEYOND_ALLOWANCE, RUN_JOB, STOP_JOB, code_call, definition,
)

NOW = datetime(2026, 11, 11, 19, 30, tzinfo=UTC)
FORMER = (
    "run_coding_task", "stop_coding_job", "repository_operation",
    "read_pull_request_checks", "request_external_review",
    "request_external_review_beyond_allowance", "read_external_review",
    "merge_pull_request", "restart_self", "run_sandbox_experiment",
)
# Each action's own permission, as its composition declares it.
PERMISSION = {
    CodeAction.RUN_JOB: "coding.execute",
    CodeAction.STOP_JOB: "coding.execute",
    CodeAction.GIT: "repository.operate",
    CodeAction.READ_CHECKS: "checks.read",
    CodeAction.REQUEST_REVIEW: "review.request",
    CodeAction.REQUEST_REVIEW_BEYOND_ALLOWANCE: "review.request",
    CodeAction.READ_REVIEW: "review.read",
    CodeAction.MERGE: "repository.merge",
    CodeAction.RESTART: "runtime.restart",
    CodeAction.EXPERIMENT: "sandbox.execute",
}


def _succeeding(action: CodeAction, calls: list):
    """An action executor that records its arguments and returns a valid result."""
    required = CONTRACTS[action].output_schema.required
    values = {field: _sample(CONTRACTS[action].output_schema.properties[field]) for field in required}

    def run(arguments):
        calls.append((action, dict(arguments)))
        return CapabilityResult("call-1", action.value, CapabilityResultState.SUCCEEDED, values,
                                outcome=ExecutionOutcome.AMBIGUOUS)
    return run


def _sample(schema: StructuredSchema):
    return {
        ValueKind.STRING: "x", ValueKind.INTEGER: 1, ValueKind.BOOLEAN: True,
        ValueKind.NUMBER: 1.0, ValueKind.ARRAY: [], ValueKind.OBJECT: {},
    }.get(schema.kind, "x")


def runtime(*actions: CodeAction, calls: list | None = None):
    """`code` over these actions (all when none are named), each with its own permission."""
    calls = [] if calls is None else calls
    chosen = actions or tuple(CodeAction)
    part = CodePart({item: _succeeding(item, calls) for item in chosen},
                    {item: frozenset({PERMISSION[item]}) for item in chosen})
    return build_code_runtime((part,), lambda: "call-1")


class OneCapabilityTests(unittest.TestCase):
    def test_one_capability_carries_every_offered_job(self) -> None:
        composed = runtime()
        self.assertEqual([item.capability_id for item in composed.definitions], [CODE])
        self.assertEqual(set(composed.executors), {CODE})
        self.assertEqual(set(composed.policies), {CODE})
        self.assertEqual(composed.offered, tuple(CodeAction))
        self.assertEqual(composed.permissions, frozenset(PERMISSION.values()))

    def test_parts_compose_and_none_is_skipped(self) -> None:
        jobs = CodePart({CodeAction.RUN_JOB: _succeeding(CodeAction.RUN_JOB, [])},
                        {CodeAction.RUN_JOB: frozenset({"coding.execute"})})
        merge = CodePart({CodeAction.MERGE: _succeeding(CodeAction.MERGE, [])},
                         {CodeAction.MERGE: frozenset({"repository.merge"})})
        composed = build_code_runtime((None, merge, jobs, None), lambda: "c")
        # Offered in the contracts' order, whatever order the parts came in.
        self.assertEqual(composed.offered, (CodeAction.RUN_JOB, CodeAction.MERGE))

    def test_nothing_composed_is_no_capability(self) -> None:
        self.assertIsNone(build_code_runtime((None, None), lambda: "c"))

    def test_an_action_composed_twice_is_refused(self) -> None:
        part = CodePart({CodeAction.GIT: _succeeding(CodeAction.GIT, [])},
                        {CodeAction.GIT: frozenset({"repository.operate"})})
        with self.assertRaises(ValueError):
            build_code_runtime((part, part), lambda: "c")

    def test_an_action_without_its_composition_is_absent_everywhere(self) -> None:
        composed = runtime(CodeAction.RUN_JOB, CodeAction.STOP_JOB, CodeAction.GIT)
        one = composed.definitions[0]
        self.assertNotIn("merge", _one_line(one.purpose))
        self.assertNotIn("[merge]", one.purpose)
        self.assertNotIn("[read_review]", one.purpose)
        # Nothing offered needs approval, so the policy asks for none.
        self.assertFalse(composed.policies[CODE].approval_required)

    def test_the_catalogue_line_names_every_job_within_the_cut(self) -> None:
        line = _one_line(definition().purpose)
        self.assertLessEqual(len(line), 200)
        self.assertFalse(line.endswith("..."))
        for job in ("coding jobs", "git", "checks", "reviews", "merge", "restart", "sandbox"):
            self.assertIn(job, line)

    def test_each_action_lists_its_fields_and_required_ones(self) -> None:
        purpose = definition().purpose
        for action, contract in CONTRACTS.items():
            with self.subTest(action=action.value):
                self.assertIn(f"[{action.value}] fields:", purpose)
                for field in contract.input_schema.required:
                    self.assertIn(field, purpose)

    def test_the_worker_holds_no_code(self) -> None:
        self.assertNotIn(CODE, WORKER_CAPABILITIES)
        self.assertFalse(set(FORMER) & set(WORKER_CAPABILITIES))


class PerActionDeclarationTests(unittest.TestCase):
    """What each job was, it still is, decided by exact values only."""

    def test_each_action_keeps_its_own_effect_class(self) -> None:
        one = definition()
        for action, contract in CONTRACTS.items():
            with self.subTest(action=action.value):
                self.assertIs(one.effect_for({"action": action.value}), contract.side_effect)
        self.assertIs(one.effect_for({"action": READ_CHECKS}), SideEffect.NONE)
        self.assertIs(one.effect_for({"action": READ_REVIEW}), SideEffect.EFFECTFUL)
        self.assertIs(one.effect_for({"action": MERGE}), SideEffect.EFFECTFUL)

    def test_only_git_and_merge_carry_her_wording(self) -> None:
        one = definition()
        self.assertFalse(one.transmits_authored_text)
        for action, contract in CONTRACTS.items():
            with self.subTest(action=action.value):
                self.assertEqual(one.transmits_authored_text_for({"action": action.value}),
                                 action in (CodeAction.GIT, CodeAction.MERGE))
                # The same as when each was its own capability.
                self.assertEqual(one.transmits_authored_text_for({"action": action.value}),
                                 contract.transmits_authored_text)

    def test_only_the_two_reads_are_plan_observations(self) -> None:
        one = definition()
        self.assertFalse(one.plan_observation)
        self.assertTrue(one.offers_plan_observation)
        for action, contract in CONTRACTS.items():
            with self.subTest(action=action.value):
                self.assertEqual(one.plan_observation_for({"action": action.value}),
                                 contract.plan_observation)
        self.assertTrue(one.plan_observation_for({"action": READ_CHECKS}))
        self.assertTrue(one.plan_observation_for({"action": READ_REVIEW}))
        self.assertFalse(one.plan_observation_for({"action": MERGE}))
        self.assertFalse(one.plan_observation_for({}))

    def test_an_action_that_sends_cannot_be_an_observation(self) -> None:
        alternative = (("action", "x"),)
        with self.assertRaises(ValueError):
            replace(definition(), transmits_authored_text_when=(alternative,),
                    plan_observation_when=(alternative,))

    def test_differently_written_declarations_matching_one_call_are_refused(self) -> None:
        """PR #229 review: a git push matches both, though the tuples differ."""
        with self.assertRaises(ValueError):
            replace(definition(),
                    transmits_authored_text_when=((("action", GIT),),),
                    plan_observation_when=((("action", GIT), ("operation", "push")),))
        with self.assertRaises(ValueError):
            replace(definition(),
                    transmits_authored_text_when=((("operation", "push"),),),
                    plan_observation_when=((("action", GIT),),))

    def test_declarations_that_cannot_match_one_call_are_allowed(self) -> None:
        replace(definition(),
                transmits_authored_text_when=((("action", GIT), ("operation", "push")),),
                plan_observation_when=((("action", GIT), ("operation", "status")),))

    def test_a_sandbox_program_stays_out_of_the_goal_audit(self) -> None:
        durable = set(definition().durable_input_fields)
        for field in ("source", "entry_filename", "wall_seconds"):
            self.assertNotIn(field, durable)
        for field in ("action", "experiment_id", "session_id", "task", "repair_branch",
                      "pull_request_number", "head_sha", "operation"):
            self.assertIn(field, durable)

    def test_the_trace_shows_identifiers_only(self) -> None:
        self.assertEqual(set(definition().trace_fields),
                         {"action", "pull_request_number", "operation"})


class ApprovalTests(unittest.TestCase):
    def test_only_a_review_past_the_allowance_needs_his_approval(self) -> None:
        composed = runtime()
        gate = SafetyGate(composed.policies)
        context = AuthorityContext("friedl", composed.permissions, NOW)
        for action in composed.offered:
            with self.subTest(action=action.value):
                decision = gate.evaluate(code_call("c", action.value, {}), context)
                expected = ("approval_required"
                            if action is CodeAction.REQUEST_REVIEW_BEYOND_ALLOWANCE else "allowed")
                self.assertEqual(decision.state.value, expected)

    def test_a_missing_permission_still_refuses(self) -> None:
        composed = runtime()
        held = composed.permissions - {"repository.merge"}
        decision = SafetyGate(composed.policies).evaluate(
            code_call("c", MERGE, {}), AuthorityContext("friedl", held, NOW))
        self.assertNotEqual(decision.state.value, "allowed")


class DispatchTests(unittest.TestCase):
    def broker(self, composed):
        return CapabilityBroker(CapabilityRegistry(composed.definitions),
                                SafetyGate(composed.policies), composed.executors)

    def test_an_unoffered_action_runs_nothing_and_names_the_offered(self) -> None:
        calls: list = []
        composed = runtime(CodeAction.RUN_JOB, calls=calls)
        result = composed.executors[CODE]({"action": MERGE, "pull_request_number": 1,
                                           "head_sha": "a" * 40})
        self.assertEqual(result.failure["code"], "arguments_unusable")
        self.assertIn("run_job", result.failure["reason"])
        self.assertNotIn("merge", result.failure["reason"])
        self.assertEqual(calls, [])

    def test_a_field_of_another_action_is_refused(self) -> None:
        calls: list = []
        composed = runtime(calls=calls)
        attempt = self.broker(composed).dispatch(
            code_call("call-1", READ_CHECKS, {"pull_request_number": 1, "head_sha": "a",
                                              "task": "fix it"}),
            AuthorityContext("friedl", composed.permissions, NOW))
        self.assertEqual(attempt.result.failure["code"], "arguments_unusable")
        self.assertIn("read_checks does not take task", attempt.result.failure["reason"])
        self.assertEqual(calls, [])

    def test_a_missing_required_field_is_named(self) -> None:
        composed = runtime()
        result = composed.executors[CODE]({"action": MERGE, "pull_request_number": 1})
        self.assertEqual(result.failure["code"], "arguments_unusable")
        self.assertIn("merge requires head_sha", result.failure["reason"])

    def test_the_action_gets_its_own_fields_and_its_result_passes_through(self) -> None:
        calls: list = []
        composed = runtime(calls=calls)
        attempt = self.broker(composed).dispatch(
            code_call("call-1", EXPERIMENT, {"experiment_id": "e", "session_id": "s",
                                             "source": "print(1)"}),
            AuthorityContext("friedl", composed.permissions, NOW))
        self.assertIs(attempt.result.state, CapabilityResultState.SUCCEEDED)
        self.assertEqual(attempt.result.capability_id, CODE)
        # Unwrapped: the action's own outcome survives the merge.
        self.assertIs(attempt.result.outcome, ExecutionOutcome.AMBIGUOUS)
        self.assertEqual(calls, [(CodeAction.EXPERIMENT,
                                  {"experiment_id": "e", "session_id": "s", "source": "print(1)"})])

    def test_a_result_outside_its_own_contract_is_refused(self) -> None:
        def wrong(_arguments):
            return CapabilityResult("c", STOP_JOB, CapabilityResultState.SUCCEEDED,
                                    {"merged": True})
        composed = build_code_runtime(
            (CodePart({CodeAction.STOP_JOB: wrong},
                      {CodeAction.STOP_JOB: frozenset({"coding.execute"})}),),
            lambda: "c")
        result = composed.executors[CODE]({"action": STOP_JOB})
        self.assertEqual(result.failure["code"], "result_output_invalid")


class SavedGoalTests(unittest.TestCase):
    """A goal saved before D-074 still counts its coding history; nothing calls the old ids."""

    def test_former_and_current_forms_are_recognised(self) -> None:
        self.assertIs(recorded_code_action("run_coding_task", {}), CodeAction.RUN_JOB)
        self.assertIs(recorded_code_action("merge_pull_request", {}), CodeAction.MERGE)
        self.assertIs(recorded_code_action(CODE, {"action": RUN_JOB}), CodeAction.RUN_JOB)
        self.assertIsNone(recorded_code_action(CODE, {"action": "deploy"}))
        self.assertIsNone(recorded_code_action("research", {"action": RUN_JOB}))
        for name in FORMER:
            self.assertIsNotNone(recorded_code_action(name, {}), name)

    def test_a_former_id_is_not_callable(self) -> None:
        composed = runtime()
        attempt = CapabilityBroker(
            CapabilityRegistry(composed.definitions), SafetyGate(composed.policies),
            composed.executors,
        ).dispatch(CapabilityCall("call-1", "merge_pull_request",
                                  {"pull_request_number": 1, "head_sha": "a" * 40}),
                   AuthorityContext("friedl", composed.permissions, NOW))
        self.assertFalse(attempt.implementation_invoked)


class OnePathTests(unittest.TestCase):
    """Law 0: the ten former capabilities are gone, not hidden."""

    def test_no_former_id_remains_in_production_code(self) -> None:
        # The one place that reads goals saved before D-074 names them.
        allowed = ROOT / "src/alx/contracts/code.py"
        for path in sorted((ROOT / "src" / "alx").rglob("*.py")):
            if path == allowed:
                continue
            text = path.read_text()
            for name in FORMER:
                self.assertNotIn(f'"{name}"', text, f"{name} in {path}")

    def test_start_up_composes_code_once(self) -> None:
        source = (ROOT / "src/alx/bootstrap/live_voice.py").read_text()
        calls = [node.func.id for node in ast.walk(ast.parse(source))
                 if isinstance(node, ast.Call) and isinstance(node.func, ast.Name)]
        self.assertEqual(calls.count("build_code_runtime"), 1)
        for part in ("coding_runtime", "repository_runtime", "checks_runtime", "review_runtime",
                     "merge_runtime", "sandbox_runtime"):
            self.assertNotIn(f"register({part}.definitions", source)
            self.assertNotIn(f"{part}.executors", source)

    def test_no_part_runtime_carries_a_definition(self) -> None:
        for module in ("coding", "repository", "repository_authority", "pull_request_checks",
                       "review", "sandbox"):
            source = (ROOT / "src/alx/bootstrap" / f"{module}.py").read_text()
            with self.subTest(module=module):
                self.assertNotIn("definitions:", source)
                self.assertNotIn("AuthorityPolicy(", source)

    def test_restoring_a_former_capability_is_caught(self) -> None:
        registry = CapabilityRegistry((definition(),))
        registry.register(replace(CONTRACTS[CodeAction.MERGE], capability_id="merge_pull_request"))
        identifiers = {item.capability_id for item in registry.list_definitions()}
        with self.assertRaises(AssertionError):
            self.assertEqual(identifiers, {CODE})

    def test_no_action_contract_is_registered_on_its_own(self) -> None:
        registered = {item.capability_id for item in runtime().definitions}
        self.assertEqual(registered, {CODE})
        self.assertFalse(registered & {action.value for action in CONTRACTS})
        self.assertFalse(registered & set(FORMER))
        self.assertTrue(all(isinstance(item, CapabilityDefinition)
                            for item in CONTRACTS.values()))


if __name__ == "__main__":
    unittest.main()
