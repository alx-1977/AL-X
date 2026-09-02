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

    def test_selecting_another_capability_neither_refreshes_nor_revokes_recovery(
        self,
    ) -> None:
        """Recovery belongs to the window, not to a capability call.

        Clearing it here dropped a recovering task back to the base ceiling,
        which is what left a stopped conversation unable to continue: every
        later turn re-raised against the spent window. Granting a fresh one
        would be the opposite fault. The allowance is bounded and counted once.
        """
        self.recorder.set_budget("task-1", XERO_BILL_BUDGET)
        for _ in range(XERO_BILL_BUDGET.stop_above):
            self.recorder.record("task-1", call())
        self.recorder.enter_recovery("task-1")

        # Reaching for another bill capability mid-recovery changes nothing.
        self.recorder.set_budget("task-1", XERO_BILL_BUDGET)
        self.recorder.check("task-1")
        self.assertEqual(
            self.recorder.task("task-1")["budget_state"], "recovering"
        )

        # The allowance is still bounded, and re-declaring buys nothing more.
        for _ in range(XERO_BILL_BUDGET.recovery_allowance):
            self.recorder.record("task-1", call())
        self.recorder.enter_recovery("task-1")
        self.recorder.set_budget("task-1", XERO_BILL_BUDGET)
        with self.assertRaises(BudgetExceeded):
            self.recorder.check("task-1")

    def test_a_completed_task_opens_a_new_window(self) -> None:
        """Settling is the one thing that does clear recovery."""
        self.recorder.set_budget("task-1", XERO_BILL_BUDGET)
        for _ in range(XERO_BILL_BUDGET.recovery_limit):
            self.recorder.record("task-1", call())
        self.recorder.enter_recovery("task-1")
        self.recorder.settle("task-1")
        self.recorder.set_budget("task-1", XERO_BILL_BUDGET)
        self.recorder.check("task-1")
        self.assertEqual(self.recorder.task("task-1")["budget_state"], "expected")

    def test_budget_rejects_an_incoherent_configuration(self) -> None:
        for expected, warn, stop in ((0, 1, 2), (3, 2, 4), (2, 5, 4)):
            with self.subTest(expected=expected, warn=warn, stop=stop):
                with self.assertRaises(ValueError):
                    ExecutionBudget(expected, warn, stop)


class BudgetDeadlockTests(unittest.TestCase):
    """A ceiling that stops a task must not strand the conversation.

    Live behaviour: a background mail event drove a DHL task to the ceiling,
    and the first thing Friedl said afterwards failed with `budget_exceeded`
    before AL/X had announced the waiting mail. The transport recovered and
    kept listening, but every later turn re-raised against the same exhausted
    window, so the conversation could never continue. The stop was correct;
    never converting it into the configured bounded allowance was not.
    """

    def setUp(self) -> None:
        self._directory = tempfile.TemporaryDirectory()
        self.addCleanup(self._directory.cleanup)
        self.recorder = SQLiteUsageRecorder(
            Path(self._directory.name) / "reasoning-usage.sqlite3"
        )

    def budget_check(self, task_id: str) -> None:
        """The live seam: a stop declares recovery, exactly as live_voice does."""
        try:
            self.recorder.check(task_id)
        except BudgetExceeded:
            self.recorder.enter_recovery(task_id)
            raise

    def spend(self, task_id: str, calls: int) -> None:
        for _ in range(calls):
            self.recorder.record(task_id, call())

    def turn(self, task_id: str) -> bool:
        """One reasoning turn. True if it was allowed to proceed."""
        try:
            self.budget_check(task_id)
        except BudgetExceeded:
            return False
        self.recorder.record(task_id, call())
        return True

    def test_the_first_turn_after_a_stop_is_not_permanently_blocked(self) -> None:
        """Requirement 7: a mail event at the ceiling must not strand speech."""
        self.recorder.set_budget("conversation-1", XERO_BILL_BUDGET)
        self.spend("conversation-1", XERO_BILL_BUDGET.stop_above)

        # The stop itself still refuses this turn and checkpoints.
        self.assertFalse(self.turn("conversation-1"))
        # But the conversation is now recovering, not deadlocked.
        self.assertEqual(
            self.recorder.task("conversation-1")["budget_state"], "recovering"
        )
        self.assertTrue(
            self.turn("conversation-1"),
            "the next spoken turn must not fail against the same spent window",
        )

    def test_recovery_grants_exactly_the_configured_allowance(self) -> None:
        """Requirement 4: bounded, and not one call more."""
        self.recorder.set_budget("conversation-1", XERO_BILL_BUDGET)
        self.spend("conversation-1", XERO_BILL_BUDGET.stop_above)
        self.assertFalse(self.turn("conversation-1"))

        allowed = 0
        for _ in range(10):
            if not self.turn("conversation-1"):
                break
            allowed += 1
        self.assertEqual(allowed, XERO_BILL_BUDGET.recovery_allowance)

    def test_recovery_cannot_be_refreshed_by_another_bill_capability(self) -> None:
        """Requirement 5: no unlimited loop, however capabilities are chosen."""
        self.recorder.set_budget("conversation-1", XERO_BILL_BUDGET)
        self.spend("conversation-1", XERO_BILL_BUDGET.stop_above)
        self.assertFalse(self.turn("conversation-1"))

        allowed = 0
        for _ in range(12):
            # Re-arming and re-declaring recovery must both buy nothing.
            self.recorder.set_budget("conversation-1", XERO_BILL_BUDGET)
            self.recorder.enter_recovery("conversation-1")
            if not self.turn("conversation-1"):
                break
            allowed += 1
        self.assertEqual(allowed, XERO_BILL_BUDGET.recovery_allowance)

    def test_a_stop_is_still_a_stop_for_a_runaway_task(self) -> None:
        """Requirement 3: the ceiling is not removed or raised."""
        self.recorder.set_budget("conversation-1", XERO_BILL_BUDGET)
        spent = 0
        refusals = 0
        for _ in range(30):
            if self.turn("conversation-1"):
                spent += 1
                continue
            refusals += 1
            if refusals > 1:
                break
        # The base ceiling stops it, then the bounded allowance, and no more.
        self.assertLessEqual(spent, XERO_BILL_BUDGET.recovery_limit)
        self.assertEqual(
            self.recorder.task("conversation-1")["budget_state"], "stopped"
        )
        # Once the allowance is gone the task stays stopped for good.
        self.assertFalse(self.turn("conversation-1"))

    def test_a_completed_capability_lets_alx_report_during_recovery(self) -> None:
        """Requirement 6: finishing the work must not silence the report."""
        self.recorder.set_budget("conversation-1", XERO_BILL_BUDGET)
        self.spend("conversation-1", XERO_BILL_BUDGET.recovery_limit)
        self.recorder.enter_recovery("conversation-1")
        self.assertFalse(self.turn("conversation-1"))

        # The capability then succeeds, so its window closes.
        self.recorder.settle("conversation-1")
        self.assertTrue(
            self.turn("conversation-1"),
            "a completed bill must still be able to say what it did",
        )

    def test_a_settled_task_reports_before_any_new_window_opens(self) -> None:
        """Requirement: settle() alone must unblock the reporting call."""
        self.recorder.set_budget("conversation-1", XERO_BILL_BUDGET)
        self.spend("conversation-1", XERO_BILL_BUDGET.stop_above)
        self.recorder.settle("conversation-1")
        self.recorder.check("conversation-1")


class BillSettlementWiringTests(unittest.TestCase):
    """Whatever arms the ceiling must be able to close it again."""

    def test_every_committing_capability_can_settle_its_window(self) -> None:
        """Requirement 1: a DHL import armed a ceiling it could never settle."""
        from alx.bootstrap.xero import (
            BILL_EXECUTION_CAPABILITIES,
            BILL_TASK_CAPABILITIES,
        )
        from alx.tools import CAPTURE_SUPPLIER_INVOICE, PROCESS_DHL_IMPORT

        self.assertIn(CAPTURE_SUPPLIER_INVOICE, BILL_EXECUTION_CAPABILITIES)
        self.assertIn(PROCESS_DHL_IMPORT, BILL_EXECUTION_CAPABILITIES)
        self.assertTrue(BILL_EXECUTION_CAPABILITIES <= BILL_TASK_CAPABILITIES)

    def test_only_a_completed_result_settles(self) -> None:
        """Requirement 2: a refusal must not falsely close the window."""
        import ast

        source = (
            Path(__file__).resolve().parents[1] / "src/alx/bootstrap/live_voice.py"
        ).read_text()
        self.assertIn("and _completed(attempt)", source)

        namespace: dict = {}
        for node in ast.parse(source).body:
            if isinstance(node, ast.FunctionDef) and node.name == "_completed":
                exec(compile(ast.Module([node], []), "<ast>", "exec"), namespace)
        completed = namespace["_completed"]

        class Attempt:
            def __init__(self, values):
                self.result = type("R", (), {"values": values})()

        # A finished DHL import settles.
        self.assertTrue(completed(Attempt({"completed": True, "stage": "dhl_invoice"})))
        # A returned stage, a refusal and a failure do not.
        self.assertFalse(
            completed(Attempt({"completed": False, "returned_for": "no_matching_draft"}))
        )
        self.assertFalse(
            completed(
                Attempt({"completed": False, "returned_for": "customs_evidence_missing"})
            )
        )
        self.assertFalse(completed(Attempt({})))
        self.assertFalse(completed(type("A", (), {"result": None})()))

    def test_the_live_budget_check_declares_recovery_on_a_stop(self) -> None:
        """Requirement: the checkpoint must open the recovery allowance."""
        source = (
            Path(__file__).resolve().parents[1] / "src/alx/bootstrap/live_voice.py"
        ).read_text()
        self.assertIn("except BudgetExceeded:", source)
        self.assertIn("usage.enter_recovery(conversation_id)", source)


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

            def list_unfinished(self, _conversation_id):
                return ()

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

        outcome = agent.process(Conversation(), _future(), step_budget=25)
        self.assertIs(outcome.state, CoreState.CHECKPOINTED)
        self.assertEqual(outcome.reason, "budget_exceeded")
        self.assertEqual(calls, [])

    def test_the_transport_keeps_listening_after_a_budget_stop(self) -> None:
        from alx.interfaces.server import RECOVERABLE_TRANSPORT_REASONS

        self.assertIn("budget_exceeded", RECOVERABLE_TRANSPORT_REASONS)


class CoreTelemetryPersistenceTests(unittest.TestCase):
    """A Core call persists the provider's actual usage through the one path.

    The live usage record held zero tokens for every reasoning-heavy Core call
    on grok while short calls were measured. The adapter normalises the
    provider's report once; the sink is the recorder; nothing else parses
    usage. These drive that path with the shapes the providers actually send.
    """

    def setUp(self) -> None:
        self._directory = tempfile.TemporaryDirectory()
        self.addCleanup(self._directory.cleanup)
        self.path = Path(self._directory.name) / "reasoning-usage.sqlite3"
        self.recorder = SQLiteUsageRecorder(self.path)

    def _row(self) -> dict:
        import sqlite3

        database = sqlite3.connect(str(self.path))
        database.row_factory = sqlite3.Row
        try:
            row = database.execute(
                "SELECT * FROM reasoning_calls ORDER BY id DESC LIMIT 1"
            ).fetchone()
        finally:
            database.close()
        self.assertIsNotNone(row)
        return dict(row)

    @staticmethod
    def _request():
        from alx.contracts import ModelMessage, ModelRequest, ModelRole

        return ModelRequest(
            (ModelMessage(ModelRole.USER, "question"),),
            "result", {"type": "object"}, "conversation-1",
        )

    def test_the_xai_streaming_shape_is_persisted_with_every_field(self) -> None:
        """The exact live shape: 253 answered, 841 thought, 10,496 cached."""
        import json

        import httpx

        from alx.providers import XAIReasoningModel

        def respond(request: httpx.Request) -> httpx.Response:
            events = (
                {"model": "grok-4.5", "choices": [{"delta": {"content": "{\"response\":"}}]},
                {
                    "model": "grok-4.5",
                    "choices": [{"delta": {"content": "\"hello\"}"}}],
                    "usage": {
                        "prompt_tokens": 17805,
                        "completion_tokens": 253,
                        "total_tokens": 18899,
                        "prompt_tokens_details": {"cached_tokens": 10496},
                        "completion_tokens_details": {"reasoning_tokens": 841},
                    },
                },
            )
            body = "".join(f"data: {json.dumps(event)}\n\n" for event in events)
            return httpx.Response(200, text=body + "data: [DONE]\n\n")

        adapter = XAIReasoningModel(
            "grok-4.5", "secret", "https://model.example", 10,
            httpx.Client(transport=httpx.MockTransport(respond)),
            streaming=True, telemetry_sink=self.recorder.sink,
        )
        adapter.complete(self._request())

        row = self._row()
        self.assertEqual(row["task_id"], "conversation-1")
        self.assertEqual(row["provider"], "xai")
        self.assertEqual(row["model"], "grok-4.5")
        self.assertEqual(row["kind"], "core")
        self.assertEqual(row["input_tokens"], 17805)
        self.assertEqual(row["cached_tokens"], 10496)
        self.assertEqual(row["cache_write_tokens"], 0)
        self.assertEqual(row["output_tokens"], 1094)
        self.assertEqual(row["reasoning_tokens"], 841)
        self.assertGreaterEqual(row["duration_ms"], 0)
        self.assertEqual(row["outcome"], "succeeded")
        rollup = self.recorder.task("conversation-1")
        self.assertEqual(rollup["calls"], 1)
        self.assertEqual(rollup["providers"], ("xai",))

    def test_the_openai_streaming_shape_is_persisted_with_every_field(self) -> None:
        import json

        import httpx

        from alx.providers import OpenAIReasoningModel

        def respond(request: httpx.Request) -> httpx.Response:
            events = (
                {"type": "response.output_text.delta", "delta": "{\"response\":"},
                {"type": "response.output_text.delta", "delta": "\"hello\"}"},
                {
                    "type": "response.completed",
                    "response": {
                        "model": "gpt-5.4-2026-03-17",
                        "output": [],
                        "usage": {
                            "input_tokens": 10000,
                            "output_tokens": 2000,
                            "total_tokens": 12000,
                            "input_tokens_details": {
                                "cached_tokens": 8000, "cache_write_tokens": 512,
                            },
                            "output_tokens_details": {"reasoning_tokens": 1500},
                        },
                    },
                },
            )
            body = "".join(f"data: {json.dumps(event)}\n\n" for event in events)
            return httpx.Response(200, text=body)

        adapter = OpenAIReasoningModel(
            "gpt-5.4", "secret", "https://model.example", 10,
            httpx.Client(transport=httpx.MockTransport(respond)),
            streaming=True, reasoning_effort="high",
            telemetry_sink=self.recorder.sink,
        )
        adapter.complete(self._request())

        row = self._row()
        self.assertEqual(row["provider"], "openai")
        self.assertEqual(row["model"], "gpt-5.4-2026-03-17")
        self.assertEqual(row["reasoning_effort"], "high")
        self.assertEqual(row["input_tokens"], 10000)
        self.assertEqual(row["cached_tokens"], 8000)
        self.assertEqual(row["cache_write_tokens"], 512)
        self.assertEqual(row["output_tokens"], 2000)
        self.assertEqual(row["reasoning_tokens"], 1500)
        self.assertGreaterEqual(row["duration_ms"], 0)


def _future():
    from datetime import UTC, datetime, timedelta

    return datetime.now(UTC) + timedelta(days=1)


if __name__ == "__main__":
    unittest.main()
