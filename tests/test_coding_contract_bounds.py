"""The bounds the coding runtime enforces must be visible before dispatch.

The live DHL job of 2026-09-11 spent two of its five attempts discovering
constraints by rejection, one of them a worktree name ("AL-X") that was not a
path on disk. The capability purpose is the one authoritative model-visible
text rendered per capability, so such facts are stated there.

step_budget was removed on 2026-10-24: it was validated and advertised, but
nothing read it once the native agent ran its own tool loop. A new call that
supplies it is now refused by the schema rather than silently ignored.
"""

from __future__ import annotations

import json
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from alx.core.model_reasoner import _catalogue_payload  # noqa: E402
from alx.tools.coding import DEFINITION, build_coding_executors  # noqa: E402


def catalogue_entry() -> dict:
    payload = json.loads(_catalogue_payload((DEFINITION,)))
    return payload["capabilities"][0]


class TheStepBudgetIsGone(unittest.TestCase):
    def test_no_step_budget_is_advertised_validated_or_carried(self) -> None:
        from alx.contracts import coding as contract
        from alx.contracts.coding import CodingRequest
        from alx.tools import coding

        entry = catalogue_entry()
        self.assertNotIn("step_budget", entry["purpose"])
        self.assertNotIn("step_budget", entry["input_schema"]["properties"])
        self.assertNotIn("step_budget", DEFINITION.durable_input_fields)
        self.assertFalse(hasattr(coding, "_optional_step_budget"))
        self.assertFalse(hasattr(contract, "MAX_STEP_BUDGET"))
        self.assertFalse(hasattr(contract, "DEFAULT_STEP_BUDGET"))
        self.assertFalse(hasattr(CodingRequest("x", "job-1"), "step_budget"))

    def test_a_supplied_step_budget_is_refused_by_the_schema(self) -> None:
        call = {"task": "x", "repair_branch": "fix/x", "commit_message": "x"}
        self.assertTrue(DEFINITION.input_schema.accepts(call))
        self.assertFalse(DEFINITION.input_schema.accepts({**call, "step_budget": 8}))


class TheCheckoutIsNotSomethingCoreNames(unittest.TestCase):

    def test_the_catalogue_offers_no_path_at_all(self) -> None:
        purpose = catalogue_entry()["purpose"]
        self.assertNotIn("filesystem path to an existing directory", purpose)
        self.assertNotIn('"."', purpose)

    def test_the_catalogue_says_the_checkout_is_configured(self) -> None:
        purpose = catalogue_entry()["purpose"]
        self.assertIn("configured canonical checkout", purpose)
        self.assertIn("No repository path is accepted", purpose)

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


class ExistingValidCallsAreUnchanged(unittest.TestCase):
    def test_the_input_schema_carries_no_path(self) -> None:
        """D-031 removed the path; resume may supply only its durable job ID.

        `worktree` is gone rather than optional. An optional path field would
        still be a path a model could supply, which is the thing that made the
        live checkout reachable.
        """
        entry = catalogue_entry()
        schema = entry["input_schema"]
        self.assertEqual(schema.get("required", []), [])
        self.assertNotIn("worktree", schema["properties"])

    def test_a_new_call_still_requires_its_task(self) -> None:
        result = build_coding_executors(
            lambda request: None, lambda: "call-1"
        )["run_coding_task"]({"repair_branch": "fix/new", "commit_message": "fix"})
        self.assertEqual(result.failure["code"], "arguments_unusable")
        self.assertEqual(result.failure["invalid_field"], "task")


if __name__ == "__main__":
    unittest.main()
