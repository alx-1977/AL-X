from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from alx.observability import (  # noqa: E402
    XERO_BILL_BUDGET,
    BudgetExceeded,
    ExecutionBudget,
    SQLiteUsageRecorder,
)
from alx.observability.usage import USD_PER_MILLION  # noqa: E402


def call(**overrides) -> dict:
    values = {
        "code": "reasoning.completed",
        "model": "gpt-5.6-sol",
        "reasoning_effort": "medium",
        "service_tier": "default",
        "input_tokens": 27943,
        "cached_tokens": 5968,
        "cache_write_tokens": 0,
        "output_tokens": 6448,
        "reasoning_tokens": 6024,
        "duration_ms": 5298,
    }
    values.update(overrides)
    return values


class UsageRecordingTests(unittest.TestCase):
    def setUp(self) -> None:
        self._directory = tempfile.TemporaryDirectory()
        self.addCleanup(self._directory.cleanup)
        self.recorder = SQLiteUsageRecorder(
            Path(self._directory.name) / "reasoning-usage.sqlite3"
        )

    def test_every_measured_field_survives_the_process(self) -> None:
        """Usage was previously streamed to a panel and lost."""
        self.recorder.record("task-1", call())
        reopened = SQLiteUsageRecorder(
            Path(self._directory.name) / "reasoning-usage.sqlite3"
        )
        rollup = reopened.task("task-1")
        self.assertEqual(rollup["calls"], 1)
        self.assertEqual(rollup["input_tokens"], 27943)
        self.assertEqual(rollup["cached_tokens"], 5968)
        self.assertEqual(rollup["output_tokens"], 6448)
        self.assertEqual(rollup["reasoning_tokens"], 6024)
        self.assertEqual(rollup["models"], ("gpt-5.6-sol",))

    def test_rollup_totals_every_call_in_the_task(self) -> None:
        for _ in range(3):
            self.recorder.record("task-1", call())
        self.recorder.record("task-2", call())
        rollup = self.recorder.task("task-1")
        self.assertEqual(rollup["calls"], 3)
        self.assertEqual(rollup["input_tokens"], 27943 * 3)
        self.assertEqual(rollup["reasoning_tokens"], 6024 * 3)

    def test_unknown_pricing_reports_no_cost_rather_than_a_guess(self) -> None:
        self.recorder.record("task-1", call())
        self.assertIsNone(self.recorder.task("task-1")["estimated_usd"])

    def test_cost_is_computed_when_pricing_is_known(self) -> None:
        USD_PER_MILLION["test-model"] = (1.0, 0.1, 10.0)
        self.addCleanup(USD_PER_MILLION.pop, "test-model", None)
        self.recorder.record(
            "task-1",
            call(
                model="test-model",
                input_tokens=1_000_000,
                cached_tokens=0,
                output_tokens=100_000,
            ),
        )
        # 1M uncached at $1 + 100k output at $10/M = $1.00 + $1.00
        self.assertAlmostEqual(
            self.recorder.task("task-1")["estimated_usd"], 2.0, places=4
        )

    def test_non_reasoning_telemetry_is_ignored(self) -> None:
        self.recorder.record("task-1", {"code": "speech.completed"})
        self.assertEqual(self.recorder.task("task-1")["calls"], 0)


class ExecutionBudgetTests(unittest.TestCase):
    def setUp(self) -> None:
        self._directory = tempfile.TemporaryDirectory()
        self.addCleanup(self._directory.cleanup)
        self.recorder = SQLiteUsageRecorder(
            Path(self._directory.name) / "reasoning-usage.sqlite3"
        )

    def test_a_routine_bill_expects_two_calls(self) -> None:
        self.assertEqual(XERO_BILL_BUDGET.expected, 2)
        self.assertEqual(XERO_BILL_BUDGET.warn_above, 2)
        self.assertEqual(XERO_BILL_BUDGET.stop_above, 4)

    def test_budget_states_progress_from_expected_to_stopped(self) -> None:
        self.recorder.set_budget("task-1", XERO_BILL_BUDGET)
        observed = []
        for _ in range(6):
            self.recorder.record("task-1", call())
            observed.append(self.recorder.task("task-1")["budget_state"])
        self.assertEqual(
            observed,
            ["expected", "expected", "warning", "warning", "stopped", "stopped"],
        )

    def test_the_ceiling_stops_further_reasoning_rather_than_flagging_it(self) -> None:
        """A flag that lets the loop keep spending is not a guardrail."""
        self.recorder.set_budget("task-1", XERO_BILL_BUDGET)
        for _ in range(4):
            self.recorder.check("task-1")
            self.recorder.record("task-1", call())
        with self.assertRaises(BudgetExceeded) as captured:
            self.recorder.check("task-1")
        self.assertEqual(captured.exception.calls, 4)
        self.assertEqual(captured.exception.limit, 4)

    def test_earlier_conversation_history_is_not_charged_to_the_bill(self) -> None:
        """A conversation is not a task.

        A live bill was stopped at "26 reasoning calls exceeds limit 4" when
        only five belonged to it; the rest was hours of unrelated conversation
        on the same id. The ceiling counts from where the budget armed.
        """
        for _ in range(21):
            self.recorder.record("conversation-1", call())
        self.recorder.set_budget("conversation-1", XERO_BILL_BUDGET)

        # 21 unrelated calls stay out; the last one is the call that reached
        # for the capability, and that first step belongs to the bill task.
        self.assertEqual(self.recorder.task("conversation-1")["calls"], 21)
        self.assertEqual(self.recorder.task("conversation-1")["budgeted_calls"], 1)
        self.assertEqual(
            self.recorder.task("conversation-1")["budget_state"], "expected"
        )

        for _ in range(XERO_BILL_BUDGET.stop_above - 1):
            self.recorder.check("conversation-1")
            self.recorder.record("conversation-1", call())
        with self.assertRaises(BudgetExceeded) as captured:
            self.recorder.check("conversation-1")
        self.assertEqual(
            captured.exception.calls,
            XERO_BILL_BUDGET.stop_above,
            "the ceiling must count the task's own calls, not the conversation's",
        )

    def test_rearming_keeps_the_original_window(self) -> None:
        """Reaching for a second bill capability must not reset the ceiling."""
        self.recorder.set_budget("task-1", XERO_BILL_BUDGET)
        for _ in range(3):
            self.recorder.record("task-1", call())
        self.recorder.set_budget("task-1", XERO_BILL_BUDGET)
        self.assertEqual(self.recorder.task("task-1")["budgeted_calls"], 3)

    def test_the_full_task_total_is_still_reported(self) -> None:
        """Windowing the ceiling must not hide what the task actually spent."""
        for _ in range(5):
            self.recorder.record("task-1", call())
        self.recorder.set_budget("task-1", XERO_BILL_BUDGET)
        self.recorder.record("task-1", call())
        rollup = self.recorder.task("task-1")
        self.assertEqual(rollup["calls"], 6)
        # The deciding call plus the one after it.
        self.assertEqual(rollup["budgeted_calls"], 2)
        self.assertEqual(rollup["input_tokens"], 27943 * 6)

    def test_a_second_bill_gets_its_own_ceiling(self) -> None:
        """Two invoices in one conversation shared a single ceiling.

        The second bill was refused for the first one's calls, so processing a
        run of invoices stopped after the first.
        """
        self.recorder.set_budget("conversation-1", XERO_BILL_BUDGET)
        for _ in range(XERO_BILL_BUDGET.stop_above):
            self.recorder.check("conversation-1")
            self.recorder.record("conversation-1", call())
        with self.assertRaises(BudgetExceeded):
            self.recorder.check("conversation-1")

        # The first bill is done; the next one starts its own window.
        self.recorder.settle("conversation-1")
        self.recorder.set_budget("conversation-1", XERO_BILL_BUDGET)
        self.recorder.check("conversation-1")
        self.assertLessEqual(
            self.recorder.task("conversation-1")["budgeted_calls"],
            1,
            "a new bill must not inherit the previous bill's spend",
        )
        self.assertEqual(self.recorder.task("conversation-1")["calls"], 4)

    def test_settling_does_not_reset_an_unfinished_bill(self) -> None:
        """Only completed work closes a window."""
        self.recorder.set_budget("task-1", XERO_BILL_BUDGET)
        for _ in range(3):
            self.recorder.record("task-1", call())
        self.recorder.set_budget("task-1", XERO_BILL_BUDGET)
        self.assertEqual(self.recorder.task("task-1")["budgeted_calls"], 3)

    def test_an_unbudgeted_task_is_never_stopped(self) -> None:
        for _ in range(20):
            self.recorder.record("task-1", call())
        self.recorder.check("task-1")
        self.assertEqual(self.recorder.task("task-1")["budget_state"], "unbudgeted")

    def test_declared_recovery_extends_the_ceiling_but_does_not_remove_it(self) -> None:
        """Ambiguity handling is real work, but no path reasons without a ceiling."""
        self.recorder.set_budget("task-1", XERO_BILL_BUDGET)
        for _ in range(4):
            self.recorder.record("task-1", call())
        with self.assertRaises(BudgetExceeded):
            self.recorder.check("task-1")

        self.recorder.enter_recovery("task-1")
        self.recorder.check("task-1")
        self.recorder.record("task-1", call())
        self.assertEqual(self.recorder.task("task-1")["budget_state"], "recovering")
        for _ in range(XERO_BILL_BUDGET.recovery_allowance - 1):
            self.recorder.check("task-1")
            self.recorder.record("task-1", call())

        # The recovery allowance is now spent; reasoning stops again.
        with self.assertRaises(BudgetExceeded) as captured:
            self.recorder.check("task-1")
        self.assertEqual(captured.exception.limit, XERO_BILL_BUDGET.recovery_limit)
        self.assertEqual(self.recorder.task("task-1")["budget_state"], "stopped")

    def test_no_budget_allows_an_unlimited_recovery(self) -> None:
        for budget in (XERO_BILL_BUDGET, ExecutionBudget(1, 1, 1)):
            with self.subTest(budget=budget):
                self.assertGreater(budget.recovery_allowance, 0)
                self.assertGreater(budget.recovery_limit, budget.stop_above)

    def test_a_new_budget_clears_a_previous_recovery(self) -> None:
        self.recorder.set_budget("task-1", XERO_BILL_BUDGET)
        self.recorder.enter_recovery("task-1")
        self.recorder.set_budget("task-1", XERO_BILL_BUDGET)
        for _ in range(5):
            self.recorder.record("task-1", call())
        with self.assertRaises(BudgetExceeded):
            self.recorder.check("task-1")

    def test_budget_rejects_an_incoherent_configuration(self) -> None:
        for expected, warn, stop in ((0, 1, 2), (3, 2, 4), (2, 5, 4)):
            with self.subTest(expected=expected, warn=warn, stop=stop):
                with self.assertRaises(ValueError):
                    ExecutionBudget(expected, warn, stop)


class CoreBudgetIntegrationTests(unittest.TestCase):
    def test_the_core_stops_reasoning_when_the_budget_check_raises(self) -> None:
        """The Core must checkpoint, not keep calling the model."""
        from alx.core import CoreAgent, CoreState

        calls = []

        class RefusingReasoner:
            def decide(self, _context):
                calls.append(1)
                raise AssertionError("the model must not be called past the ceiling")

        class Store:
            def load(self, _goal_id):
                return None

        def check(_task_id):
            raise BudgetExceeded("task-1", 5, 4)

        agent = CoreAgent(
            Store(), RefusingReasoner(), lambda _c, _s: None, (),
            budget_check=check,
        )

        class Conversation:
            conversation_id = "task-1"
            turns = ()
            events = ()

        outcome = agent.process(
            Conversation(), None, _future(), step_budget=25
        )
        self.assertIs(outcome.state, CoreState.CHECKPOINTED)
        self.assertEqual(outcome.reason, "budget_exceeded")
        self.assertEqual(calls, [])

    def test_the_transport_keeps_listening_after_a_budget_stop(self) -> None:
        from alx.interfaces.server import RECOVERABLE_TRANSPORT_REASONS

        self.assertIn("budget_exceeded", RECOVERABLE_TRANSPORT_REASONS)


def _future():
    from datetime import UTC, datetime, timedelta

    return datetime.now(UTC) + timedelta(days=1)


if __name__ == "__main__":
    unittest.main()
