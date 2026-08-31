"""The two holes that let a controlled bill test run to 13 reasoning calls.

The guardrail existed but was never armed on the live path, and AL/X was
offered both the composite bill execution and the granular write steps, so a
routine bill could still be reasoned through one API call at a time.
"""

from __future__ import annotations

import ast
import sys
import tempfile
import unittest
from pathlib import Path

REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPOSITORY_ROOT / "src"))

from alx.bootstrap.xero import (  # noqa: E402
    BILL_EXECUTION_CAPABILITIES,
    BILL_TASK_CAPABILITIES,
    RECOVERY_ONLY_CAPABILITIES,
    build_xero_runtime,
)
from alx.config import XeroSettings  # noqa: E402
from alx.observability import XERO_BILL_BUDGET  # noqa: E402
from alx.tools import (  # noqa: E402
    ATTACH_MAIL_DOCUMENT_TO_XERO_BILL,
    CAPTURE_SUPPLIER_INVOICE,
    LIST_XERO_ACCOUNTS,
    LIST_XERO_TAX_RATES,
    SEARCH_XERO_CONTACTS,
    AUTHORISE_XERO_BILL,
    CREATE_XERO_DRAFT_BILL,
    EXECUTE_XERO_BILL,
    FIND_XERO_BILL,
    READ_XERO_BILL,
    UPDATE_XERO_DRAFT_BILL,
)


class FakeMail:
    def read_attachment(self, _reference, _attachment_id):
        raise AssertionError("not used")


def runtime():
    with tempfile.TemporaryDirectory() as directory:
        return build_xero_runtime(
            XeroSettings(
                "id", "secret", "http://localhost/callback", "", 10, 300, True, False
            ),
            Path(directory),
            FakeMail(),
            lambda: "call",
        )


class RoutineCatalogueTests(unittest.TestCase):
    """AL/X plans from `definitions`, so that is what must not offer two paths."""

    def setUp(self) -> None:
        self.runtime = runtime()
        self.offered = {item.capability_id for item in self.runtime.definitions}

    def test_committing_a_bill_offers_exactly_one_capability(self) -> None:
        writes = self.offered & (
            RECOVERY_ONLY_CAPABILITIES | BILL_EXECUTION_CAPABILITIES
        )
        self.assertEqual(writes, {CAPTURE_SUPPLIER_INVOICE})

    def test_the_old_step_by_step_write_path_is_not_offered(self) -> None:
        for capability_id in (
            EXECUTE_XERO_BILL,
            CREATE_XERO_DRAFT_BILL,
            UPDATE_XERO_DRAFT_BILL,
            ATTACH_MAIL_DOCUMENT_TO_XERO_BILL,
            AUTHORISE_XERO_BILL,
        ):
            with self.subTest(capability_id=capability_id):
                self.assertNotIn(capability_id, self.offered)

    def test_reads_remain_available_for_ordinary_questions(self) -> None:
        for capability_id in (FIND_XERO_BILL, READ_XERO_BILL):
            with self.subTest(capability_id=capability_id):
                self.assertIn(capability_id, self.offered)

    def test_granular_steps_stay_dispatchable_for_an_explicit_recovery(self) -> None:
        """Withheld from planning, not deleted: recovery may still need them."""
        recovery = {item.capability_id for item in self.runtime.recovery_definitions}
        self.assertEqual(recovery, set(RECOVERY_ONLY_CAPABILITIES))
        for capability_id in RECOVERY_ONLY_CAPABILITIES:
            with self.subTest(capability_id=capability_id):
                self.assertIn(capability_id, self.runtime.executors)
                self.assertIn(capability_id, self.runtime.policies)

    def test_the_composite_reuses_the_granular_implementation(self) -> None:
        """The building blocks are shared code, not a second implementation."""
        source = (REPOSITORY_ROOT / "src/alx/tools/xero.py").read_text()
        self.assertIn("_draft_payload(arguments, account)", source)
        self.assertIn("_verified_attachment(", source)


class ArmedBudgetTests(unittest.TestCase):
    """The budget must be armed by the live path, not merely defined."""

    def setUp(self) -> None:
        self.source = (
            REPOSITORY_ROOT / "src/alx/bootstrap/live_voice.py"
        ).read_text()
        self.tree = ast.parse(self.source)

    def test_the_runtime_passes_a_budget_check_to_the_core(self) -> None:
        core = [
            node
            for node in ast.walk(self.tree)
            if isinstance(node, ast.Call)
            and isinstance(node.func, ast.Name)
            and node.func.id == "CoreAgent"
        ]
        self.assertEqual(len(core), 1)
        keywords = {item.arg for item in core[0].keywords}
        self.assertIn("budget_check", keywords)

    def test_dispatching_a_bill_execution_arms_the_ceiling(self) -> None:
        """Without this the ceiling never applies to a real bill task."""
        self.assertIn("BILL_TASK_CAPABILITIES", self.source)
        self.assertIn("usage.set_budget(", self.source)
        self.assertIn("XERO_BILL_BUDGET", self.source)

    def test_the_armed_budget_is_the_agreed_shape(self) -> None:
        self.assertEqual(XERO_BILL_BUDGET.expected, 2)
        self.assertEqual(XERO_BILL_BUDGET.warn_above, 2)
        self.assertEqual(XERO_BILL_BUDGET.stop_above, 4)
        self.assertEqual(XERO_BILL_BUDGET.recovery_allowance, 2)
        self.assertEqual(XERO_BILL_BUDGET.recovery_limit, 6)

    def test_any_bill_capability_arms_the_ceiling_not_only_the_commit(self) -> None:
        """Arming on the commit was too late; a task reached seven calls first."""
        self.assertIn(CAPTURE_SUPPLIER_INVOICE, BILL_TASK_CAPABILITIES)
        self.assertIn(EXECUTE_XERO_BILL, BILL_TASK_CAPABILITIES)
        for capability_id in (
            SEARCH_XERO_CONTACTS,
            LIST_XERO_ACCOUNTS,
            LIST_XERO_TAX_RATES,
            FIND_XERO_BILL,
            READ_XERO_BILL,
        ):
            with self.subTest(capability_id=capability_id):
                self.assertIn(capability_id, BILL_TASK_CAPABILITIES)


class ArmedBudgetBehaviourTests(unittest.TestCase):
    """Exercise the wiring the runtime uses, rather than trusting the source."""

    def test_a_bill_dispatch_arms_the_ceiling_and_then_stops_reasoning(self) -> None:
        from alx.observability import BudgetExceeded, SQLiteUsageRecorder

        with tempfile.TemporaryDirectory() as directory:
            usage = SQLiteUsageRecorder(Path(directory) / "usage.sqlite3")
            current_conversation_id = [""]

            def budget_check(conversation_id: str) -> None:
                current_conversation_id[0] = conversation_id
                usage.check(conversation_id)

            def dispatch(capability_id: str) -> None:
                if capability_id in BILL_TASK_CAPABILITIES:
                    usage.set_budget(current_conversation_id[0], XERO_BILL_BUDGET)

            call = {
                "code": "reasoning.completed",
                "provider": "xai",
                "model": "grok-4.5",
                "input_tokens": 55000,
                "output_tokens": 800,
                "reasoning_tokens": 1400,
            }

            # Before a bill is committed the conversation is unbudgeted.
            budget_check("conversation-1")
            usage.record("conversation-1", call)
            self.assertEqual(
                usage.task("conversation-1")["budget_state"], "unbudgeted"
            )

            # Committing a bill declares the task routine.
            dispatch(CAPTURE_SUPPLIER_INVOICE)
            self.assertEqual(
                usage.task("conversation-1")["budget_state"], "expected"
            )

            # Reasoning past the ceiling is refused rather than recorded.
            for _ in range(3):
                budget_check("conversation-1")
                usage.record("conversation-1", call)
            with self.assertRaises(BudgetExceeded):
                budget_check("conversation-1")
            self.assertEqual(usage.task("conversation-1")["calls"], 4)

    def test_a_bill_task_cannot_reach_five_calls_unbudgeted(self) -> None:
        """The failure this fixes: seven calls spent before the ceiling armed.

        Whichever bill capability AL/X reaches for first arms the ceiling, and
        the calls already spent on the task still count against it, so a bill
        task can never run away before the budget notices.
        """
        from alx.observability import BudgetExceeded, SQLiteUsageRecorder

        for first_capability in sorted(BILL_TASK_CAPABILITIES):
            with self.subTest(first_capability=first_capability):
                with tempfile.TemporaryDirectory() as directory:
                    usage = SQLiteUsageRecorder(Path(directory) / "usage.sqlite3")
                    current = [""]

                    def budget_check(conversation_id: str) -> None:
                        current[0] = conversation_id
                        usage.check(conversation_id)

                    def dispatch(capability_id: str) -> None:
                        if capability_id in BILL_TASK_CAPABILITIES:
                            usage.set_budget(current[0], XERO_BILL_BUDGET)

                    call = {"code": "reasoning.completed", "model": "grok-4.5"}

                    # AL/X reasons, then reaches for a bill capability.
                    budget_check("conversation-1")
                    usage.record("conversation-1", call)
                    dispatch(first_capability)

                    spent = 1
                    stopped_at = None
                    for _ in range(10):
                        try:
                            budget_check("conversation-1")
                        except BudgetExceeded as error:
                            stopped_at = error.calls
                            break
                        usage.record("conversation-1", call)
                        spent += 1

                    self.assertIsNotNone(
                        stopped_at, "a bill task ran without ever being stopped"
                    )
                    self.assertLessEqual(
                        spent,
                        XERO_BILL_BUDGET.stop_above,
                        "a bill task spent more than the ceiling allows",
                    )

    def test_a_conversation_without_a_bill_is_never_capped(self) -> None:
        from alx.observability import SQLiteUsageRecorder

        with tempfile.TemporaryDirectory() as directory:
            usage = SQLiteUsageRecorder(Path(directory) / "usage.sqlite3")
            call = {"code": "reasoning.completed", "model": "grok-4.5"}
            for _ in range(12):
                usage.check("chat-1")
                usage.record("chat-1", call)
            self.assertEqual(usage.task("chat-1")["budget_state"], "unbudgeted")


if __name__ == "__main__":
    unittest.main()
