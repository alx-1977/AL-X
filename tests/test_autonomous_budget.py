"""The hard daily ceiling on autonomous Core cognition.

This is D-024's emergency fuse. It bounds money, not initiative: nothing here
counts how often AL/X thinks or decides a thought was not worth having. The
tests pin the properties that make the ceiling honest — it is withdrawn before
dispatch, it fails closed on anything it cannot price, unmeasured usage keeps
the full withdrawal, and a restart cannot hand her a fresh day.
"""

from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from alx.observability import ConfiguredPricingWorstCase  # noqa: E402
from alx.observability.autonomous_budget import (  # noqa: E402
    AutonomousBoundMissing,
    AutonomousBudgetExceeded,
    AutonomousModelUnpriced,
    AutonomousReservation,
    SQLiteAutonomousLedger,
)

# D-024a: R10/day at the recorded R18.5/USD assumption.
DAILY = 0.5405
LUNA = ("openai", "gpt-5.6-luna")
IN_BOUND, OUT_BOUND = 32_000, 32_000


def _measured(output_tokens: int = 6_000) -> dict[str, int]:
    return {
        "input_tokens": 14_000,
        "cached_tokens": 12_000,
        "output_tokens": output_tokens,
        "reasoning_tokens": 5_200,
        "cache_write_tokens": 0,
    }


class AutonomousBudgetTests(unittest.TestCase):
    def setUp(self) -> None:
        self._dir = tempfile.TemporaryDirectory()
        self.addCleanup(self._dir.cleanup)
        self.path = Path(self._dir.name) / "autonomous.sqlite3"
        self.ledger = self._ledger()

    def _ledger(self) -> SQLiteAutonomousLedger:
        return SQLiteAutonomousLedger(self.path, DAILY, ConfiguredPricingWorstCase())

    def _reserve(self) -> AutonomousReservation:
        return self.ledger.reserve(*LUNA, IN_BOUND, OUT_BOUND)

    def test_the_worst_case_is_the_approved_figure(self) -> None:
        """D-024a records $0.0528 per autonomous turn at 32k/32k."""
        self.assertAlmostEqual(
            self.ledger.worst_case_usd(*LUNA, IN_BOUND, OUT_BOUND), 0.0528, places=6
        )

    def test_the_daily_ceiling_admits_ten_worst_case_turns(self) -> None:
        for index in range(10):
            with self.subTest(turn=index):
                self._reserve()
        with self.assertRaises(AutonomousBudgetExceeded):
            self._reserve()

    def test_an_unpriced_model_fails_closed_before_dispatch(self) -> None:
        """A call we cannot price is not a free call."""
        with self.assertRaises(AutonomousModelUnpriced):
            self.ledger.reserve("openai", "gpt-5.6-nonesuch", IN_BOUND, OUT_BOUND)
        self.assertEqual(self.ledger.spend_today(), 0.0)

    def test_a_turn_without_a_finite_output_bound_is_refused(self) -> None:
        """Without a bound there is no worst case, so no honest reservation."""
        for bound in (None, 0, -1):
            with self.subTest(bound=bound):
                with self.assertRaises(AutonomousBoundMissing):
                    self.ledger.reserve(*LUNA, IN_BOUND, bound)
        self.assertEqual(self.ledger.spend_today(), 0.0)

    def test_measured_usage_reconciles_and_returns_the_difference(self) -> None:
        reservation = self._reserve()
        self.assertAlmostEqual(self.ledger.spend_today(), 0.0528, places=6)
        actual = self.ledger.settle(reservation, *LUNA, _measured())
        self.assertLess(actual, reservation.reserved_usd)
        self.assertAlmostEqual(self.ledger.spend_today(), actual, places=6)

    def test_missing_usage_retains_the_full_reservation(self) -> None:
        """Silence from a provider is not a free turn."""
        reservation = self._reserve()
        actual = self.ledger.settle(reservation, *LUNA, {})
        self.assertEqual(actual, reservation.reserved_usd)
        self.assertAlmostEqual(self.ledger.spend_today(), 0.0528, places=6)

    def test_malformed_usage_retains_the_full_reservation(self) -> None:
        reservation = self._reserve()
        for usage in (None, "nonsense", {"input_tokens": -1}, {"input_tokens": True}):
            with self.subTest(usage=usage):
                self.assertEqual(
                    self.ledger.settle(reservation, *LUNA, usage),
                    reservation.reserved_usd,
                )

    def test_an_unreconciled_reservation_survives_restart(self) -> None:
        """A crash mid-call must not hand back the money it withdrew."""
        self._reserve()
        reopened = self._ledger()
        self.assertAlmostEqual(reopened.spend_today(), 0.0528, places=6)

    def test_a_restart_cannot_hand_back_a_fresh_day(self) -> None:
        for _ in range(10):
            self._reserve()
        reopened = self._ledger()
        with self.assertRaises(AutonomousBudgetExceeded):
            reopened.reserve(*LUNA, IN_BOUND, OUT_BOUND)

    def test_the_ledger_never_raises_its_own_ceiling(self) -> None:
        """No path may increase the budget; exhaustion is final for the day."""
        for _ in range(10):
            self._reserve()
        for _ in range(3):
            with self.assertRaises(AutonomousBudgetExceeded):
                self._reserve()
        self.assertLessEqual(self.ledger.spend_today(), DAILY + 1e-9)

    def test_a_refusal_never_selects_a_cheaper_model_or_bound(self) -> None:
        """A ceiling that quietly buys something lesser is not a ceiling."""
        for _ in range(10):
            self._reserve()
        with self.assertRaises(AutonomousBudgetExceeded) as caught:
            self._reserve()
        self.assertAlmostEqual(caught.exception.required_usd, 0.0528, places=6)
        self.assertEqual(self.ledger.spend_today(), 10 * 0.0528)

    def test_an_exhausted_day_leaves_no_partial_withdrawal(self) -> None:
        for _ in range(10):
            self._reserve()
        before = self.ledger.spend_today()
        with self.assertRaises(AutonomousBudgetExceeded):
            self._reserve()
        self.assertEqual(self.ledger.spend_today(), before)

    def test_an_unconfigured_runtime_cannot_spend(self) -> None:
        """Zero is the default, so autonomous cognition is off until funded."""
        ledger = SQLiteAutonomousLedger(
            Path(self._dir.name) / "zero.sqlite3", 0.0, ConfiguredPricingWorstCase()
        )
        with self.assertRaises(AutonomousBudgetExceeded):
            ledger.reserve(*LUNA, IN_BOUND, OUT_BOUND)


class SeparateCeilingTests(unittest.TestCase):
    """D-023 research spend and D-024 cognition spend never touch each other."""

    def test_autonomous_spend_does_not_consume_the_research_ledger(self) -> None:
        from alx.observability import ResearchBudget, SQLiteResearchLedger

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            research = SQLiteResearchLedger(
                root / "research.sqlite3", ResearchBudget(1.0, 0.007)
            )
            autonomous = SQLiteAutonomousLedger(
                root / "autonomous.sqlite3", DAILY, ConfiguredPricingWorstCase()
            )
            autonomous.reserve(*LUNA, IN_BOUND, OUT_BOUND)
            self.assertEqual(research.remaining_usd(), 1.0)
            self.assertLess(autonomous.remaining_usd(), DAILY)


if __name__ == "__main__":
    unittest.main()
