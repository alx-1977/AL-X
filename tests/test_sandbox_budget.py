"""D-027 daily fuses: two independent ceilings that survive restart.

The two ceilings fail differently and must both hold alone. Many quick runs
exhaust the count while barely touching the seconds; one long run exhausts the
seconds while the count is almost untouched. A ledger enforcing only one would
appear to work until the other kind of overrun arrived.

Neither ceiling is memory containment, and one test records that in the suite
so the claim cannot drift back in.
"""

from __future__ import annotations

import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path

REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPOSITORY_ROOT / "src"))

from alx.observability.sandbox_ledger import (  # noqa: E402
    SQLiteSandboxLedger,
    SandboxBudget,
    SandboxBudgetExceeded,
    SandboxLedgerCorrupt,
)


class SandboxLedgerTest(unittest.TestCase):
    def setUp(self) -> None:
        self.directory = tempfile.TemporaryDirectory()
        self.path = Path(self.directory.name) / "runs.sqlite3"
        self.addCleanup(self.directory.cleanup)

    def _ledger(self, runs: int = 20, seconds: int = 300) -> SQLiteSandboxLedger:
        return SQLiteSandboxLedger(self.path, SandboxBudget(runs, seconds))

    def test_the_run_ceiling_holds_on_its_own(self) -> None:
        """Many one-second runs: the count runs out long before the seconds."""
        ledger = self._ledger(runs=3, seconds=100_000)
        for _ in range(3):
            ledger.settle(ledger.reserve(1), 1.0)
        with self.assertRaises(SandboxBudgetExceeded) as caught:
            ledger.reserve(1)
        self.assertIn("run ceiling", caught.exception.reason)

    def test_the_wall_time_ceiling_holds_on_its_own(self) -> None:
        """Few long runs: the seconds run out long before the count."""
        ledger = self._ledger(runs=1_000, seconds=60)
        ledger.settle(ledger.reserve(60), 60.0)
        with self.assertRaises(SandboxBudgetExceeded) as caught:
            ledger.reserve(30)
        self.assertIn("wall-time ceiling", caught.exception.reason)

    def test_time_is_withdrawn_before_a_run_and_settled_after(self) -> None:
        ledger = self._ledger()
        reservation = ledger.reserve(30)
        # The full permitted time is committed while the run is in flight, so a
        # crash leaves the day over-recorded rather than unrecorded.
        self.assertEqual(ledger.committed_seconds(), 30.0)
        ledger.settle(reservation, 2.5)
        self.assertEqual(ledger.committed_seconds(), 2.5)
        self.assertEqual(ledger.committed_runs(), 1)

    def test_settling_can_never_record_more_than_was_reserved(self) -> None:
        ledger = self._ledger()
        reservation = ledger.reserve(5)
        recorded = ledger.settle(reservation, 900.0)
        self.assertEqual(recorded, 5.0)

    def test_an_abandoned_reservation_is_released(self) -> None:
        ledger = self._ledger()
        reservation = ledger.reserve(30)
        ledger.abandon(reservation, "run_failed")
        self.assertEqual(ledger.committed_runs(), 0)
        self.assertEqual(ledger.committed_seconds(), 0.0)

    def test_a_reservation_in_flight_cannot_be_spent_twice(self) -> None:
        ledger = self._ledger(runs=2, seconds=100)
        ledger.reserve(30)
        ledger.reserve(30)
        with self.assertRaises(SandboxBudgetExceeded):
            ledger.reserve(30)

    def test_the_ceiling_survives_a_restart(self) -> None:
        first = self._ledger(runs=2, seconds=100)
        first.settle(first.reserve(10), 10.0)
        second = self._ledger(runs=2, seconds=100)
        self.assertEqual(second.committed_runs(), 1)
        self.assertEqual(second.remaining_runs(), 1)

    def test_a_corrupt_ledger_fails_closed(self) -> None:
        """A ceiling nobody can measure must stop execution, not permit it."""
        ledger = self._ledger()
        ledger.settle(ledger.reserve(1), 1.0)
        self.path.write_bytes(b"this is not a database")
        with self.assertRaises((SandboxLedgerCorrupt, sqlite3.DatabaseError)):
            broken = SQLiteSandboxLedger(self.path, SandboxBudget())
            broken.reserve(1)

    def test_negative_totals_are_treated_as_corruption(self) -> None:
        ledger = self._ledger()
        ledger.settle(ledger.reserve(5), 5.0)
        database = sqlite3.connect(self.path)
        database.execute("UPDATE sandbox_runs SET actual_seconds = -100")
        database.commit()
        database.close()
        with self.assertRaises(SandboxLedgerCorrupt):
            ledger.committed_seconds()

    def test_the_ledger_never_raises_its_own_ceiling(self) -> None:
        ledger = self._ledger(runs=1, seconds=10)
        ledger.settle(ledger.reserve(10), 10.0)
        for _ in range(3):
            with self.assertRaises(SandboxBudgetExceeded):
                ledger.reserve(1)
        self.assertEqual(ledger.remaining_runs(), 0)

    def test_the_recorded_ceilings_match_the_decision(self) -> None:
        """D-027 records 20 runs and 300 wall seconds per day."""
        from alx.contracts.sandbox import DAILY_RUNS, DAILY_WALL_SECONDS
        from alx.observability import sandbox_ledger

        self.assertEqual(DAILY_RUNS, 20)
        self.assertEqual(DAILY_WALL_SECONDS, 300)
        # The leaf boundaries duplicate these as literals because neither may
        # import contracts. They must not drift apart.
        self.assertEqual(sandbox_ledger.DAILY_RUNS, DAILY_RUNS)
        self.assertEqual(sandbox_ledger.DAILY_WALL_SECONDS, DAILY_WALL_SECONDS)

        from alx.config import settings

        self.assertEqual(settings._SANDBOX_DAILY_RUNS, DAILY_RUNS)
        self.assertEqual(settings._SANDBOX_DAILY_WALL_SECONDS, DAILY_WALL_SECONDS)

    def test_the_fuses_are_not_described_as_memory_containment(self) -> None:
        """D-027 forbids presenting these ceilings as memory protection."""
        source = Path(
            REPOSITORY_ROOT / "src/alx/observability/sandbox_ledger.py"
        ).read_text()
        self.assertIn("Neither ceiling is memory containment", source)


if __name__ == "__main__":
    unittest.main()
