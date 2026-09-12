"""The bounds the coding runtime enforces must be visible before dispatch.

The live DHL job of 2026-09-11 spent two of its five attempts discovering
constraints by rejection:

    call-dhl-diag-001  arguments_unusable   step_budget=40, maximum is 32
    call-dhl-diag-002  worktree_unusable    "AL-X" resolved to a path that
                                            does not exist

Both refusals were correct and carried precise diagnostics, and the Core
corrected itself. But neither constraint was visible in the capability
catalogue beforehand. StructuredSchema describes kinds, not numeric ranges or
path semantics, so step_budget reached the Core as {"kind": "integer"} with no
ceiling and worktree as {"kind": "string"} with nothing saying it is a path on
disk rather than a project name.

The capability purpose is the one authoritative model-visible text rendered per
capability, so both facts are stated there. MAX_STEP_BUDGET is interpolated
rather than written out, so the number the Core is told cannot drift from the
one the executor applies.

Nothing about validation changes. These tests prove the constraints are
visible, that the stated ceiling is the enforced ceiling, and that invalid
values still fail closed.
"""

from __future__ import annotations

import json
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from alx.contracts.coding import MAX_STEP_BUDGET  # noqa: E402
from alx.core.model_reasoner import _catalogue_payload  # noqa: E402
from alx.tools.coding import DEFINITION, build_coding_executors  # noqa: E402


def catalogue_entry() -> dict:
    payload = json.loads(_catalogue_payload((DEFINITION,)))
    return payload["capabilities"][0]


class TheStepBudgetCeilingIsVisible(unittest.TestCase):
    def test_the_catalogue_states_the_maximum_step_budget(self) -> None:
        purpose = catalogue_entry()["purpose"]
        self.assertIn(f"from 1 to {MAX_STEP_BUDGET}", purpose)
        self.assertIn("step_budget is optional", purpose)

    def test_the_stated_ceiling_is_the_enforced_ceiling(self) -> None:
        """Interpolated, so prose cannot drift from the executor's rule."""
        self.assertEqual(MAX_STEP_BUDGET, 32)
        self.assertIn(str(MAX_STEP_BUDGET), catalogue_entry()["purpose"])
        # The same constant the validator applies.
        import inspect
        from alx.tools import coding

        source = inspect.getsource(coding._optional_step_budget)
        self.assertIn("MAX_STEP_BUDGET", source)


class TheWorktreeIsDescribedAsAPath(unittest.TestCase):
    """There is no canonical worktree identifier, and none is invented.

    D-028 assigns a worktree per job, so the runtime's only rule is that the
    value resolves to a directory that exists. The catalogue therefore states
    what the field is rather than naming one repository.
    """

    def test_the_catalogue_says_worktree_is_an_existing_path(self) -> None:
        purpose = catalogue_entry()["purpose"]
        self.assertIn("filesystem path to an existing directory", purpose)
        self.assertIn("not a project or repository name", purpose)

    def test_the_catalogue_names_the_relative_root(self) -> None:
        """The value that actually works, stated rather than guessed."""
        self.assertIn(
            'so "." is the repository the runtime is running in',
            catalogue_entry()["purpose"],
        )

    def test_no_repository_name_is_hard_coded_as_canonical(self) -> None:
        """No alias, no fuzzy match, no single blessed identifier."""
        purpose = catalogue_entry()["purpose"]
        for name in ("AL_X", "AL-X"):
            self.assertNotIn(name, purpose)


class InvalidValuesStillFailClosed(unittest.TestCase):
    """Visibility changes nothing about enforcement."""

    def _run(self, arguments: dict):
        executors = build_coding_executors(lambda request: None, lambda: "call-1")
        return executors["run_coding_task"](arguments)

    def test_a_step_budget_over_the_maximum_is_still_refused(self) -> None:
        result = self._run({
            "task": "x", "worktree": ".", "step_budget": MAX_STEP_BUDGET + 1,
        })
        self.assertEqual(result.state.value, "failed")
        self.assertEqual(result.failure["code"], "arguments_unusable")
        self.assertEqual(result.failure["invalid_field"], "step_budget")

    def test_a_step_budget_of_zero_is_still_refused(self) -> None:
        result = self._run({"task": "x", "worktree": ".", "step_budget": 0})
        self.assertEqual(result.state.value, "failed")
        self.assertEqual(result.failure["invalid_field"], "step_budget")

    def test_a_worktree_that_does_not_exist_is_still_refused(self) -> None:
        """The live "AL-X" case: a name, not a path that exists.

        Checked where the workspace is built, which is the runtime boundary
        that actually opens the directory.
        """
        from alx.contracts import CodingError
        from alx.providers.coding_workspace import CodingWorkspace

        with self.assertRaises(CodingError) as caught:
            CodingWorkspace("AL-X")
        self.assertEqual(caught.exception.code, "worktree_unusable")
        self.assertEqual(caught.exception.details["reason_code"], "missing")

    def test_the_maximum_step_budget_itself_is_accepted(self) -> None:
        """The boundary the catalogue states is usable, not off by one."""
        from alx.contracts.coding import CodingRequest

        request = CodingRequest("x", ".", step_budget=MAX_STEP_BUDGET)
        self.assertEqual(request.step_budget, MAX_STEP_BUDGET)


class ExistingValidCallsAreUnchanged(unittest.TestCase):
    def test_the_input_schema_is_untouched(self) -> None:
        """Only the purpose text changed; the contract shape did not."""
        entry = catalogue_entry()
        schema = entry["input_schema"]
        self.assertEqual(sorted(schema["required"]), ["task", "worktree"])
        self.assertIn("step_budget", schema["properties"])
        self.assertEqual(schema["properties"]["step_budget"]["kind"], "integer")
        self.assertEqual(schema["properties"]["worktree"]["kind"], "string")

    def test_a_call_without_a_step_budget_keeps_the_default(self) -> None:
        """Omitting it is valid and unchanged: the default still applies."""
        from alx.contracts.coding import DEFAULT_STEP_BUDGET, CodingRequest

        request = CodingRequest("x", ".")
        self.assertEqual(request.step_budget, DEFAULT_STEP_BUDGET)
        self.assertLessEqual(request.step_budget, MAX_STEP_BUDGET)


if __name__ == "__main__":
    unittest.main()
