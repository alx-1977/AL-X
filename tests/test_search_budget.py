"""The search spend ledger: two independent ceilings, and honest records.

A flat-fee search is a different economic resource from model cognition, and
the record has to say only true things about it. These tests hold the ledger to
that: no cognition tier, no model, no token counts, its own durable file, and
both the request-count and USD ceilings enforced separately, because a wrong
price must not be able to defeat the count.
"""

from __future__ import annotations

import sqlite3
import sys
import tempfile
import threading
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from alx.observability.search_budget import (
    SQLiteSearchLedger,
    SearchBudget,
    SearchBudgetExceeded,
    SearchLedgerCorrupt,
    SearchReservation,
)


PRICE = 0.005
DAILY_REQUESTS = 30
DAILY_USD = 0.15


class LedgerTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.path = Path(self.directory.name) / "search-spend.sqlite3"

    def ledger(self, **overrides) -> SQLiteSearchLedger:
        values = {
            "daily_usd": DAILY_USD,
            "daily_requests": DAILY_REQUESTS,
            "usd_per_request": PRICE,
        }
        values.update(overrides)
        return SQLiteSearchLedger(self.path, SearchBudget(**values))


class ReservationTests(LedgerTestCase):
    def test_the_first_reservation_succeeds(self) -> None:
        ledger = self.ledger()
        reservation = ledger.reserve("brave")
        self.assertIsInstance(reservation, SearchReservation)
        self.assertEqual(reservation.reserved_usd, PRICE)

    def test_one_reservation_counts_once(self) -> None:
        ledger = self.ledger()
        ledger.reserve("brave")
        self.assertEqual(ledger.committed_requests(), 1)
        self.assertEqual(ledger.remaining_requests(), DAILY_REQUESTS - 1)

    def test_the_exact_flat_price_is_recorded(self) -> None:
        """A flat fee has no variance; the reservation is the price."""
        ledger = self.ledger()
        ledger.reserve("brave")
        self.assertAlmostEqual(ledger.committed_usd(), PRICE, places=9)

    def test_a_blank_provider_is_refused(self) -> None:
        with self.assertRaises(ValueError):
            self.ledger().reserve("   ")


class CountCeilingTests(LedgerTestCase):
    def test_the_thirtieth_request_is_permitted(self) -> None:
        ledger = self.ledger()
        for _ in range(DAILY_REQUESTS):
            ledger.reserve("brave")
        self.assertEqual(ledger.committed_requests(), DAILY_REQUESTS)

    def test_the_thirty_first_request_is_refused(self) -> None:
        ledger = self.ledger()
        for _ in range(DAILY_REQUESTS):
            ledger.reserve("brave")
        with self.assertRaises(SearchBudgetExceeded) as caught:
            ledger.reserve("brave")
        self.assertIn("request ceiling", str(caught.exception))

    def test_a_wrongly_low_price_cannot_defeat_the_count(self) -> None:
        """The two ceilings are independent, and this is why.

        A price recorded far too low would leave the USD ceiling untouched
        after thirty requests. The count still stops there.
        """
        ledger = self.ledger(usd_per_request=0.000001)
        for _ in range(DAILY_REQUESTS):
            ledger.reserve("brave")
        self.assertLess(ledger.committed_usd(), DAILY_USD)
        self.assertGreater(ledger.remaining_usd(), 0.0)
        with self.assertRaises(SearchBudgetExceeded):
            ledger.reserve("brave")


class SpendCeilingTests(LedgerTestCase):
    def test_a_wrongly_high_price_exhausts_usd_before_the_count(self) -> None:
        """And this is why the USD ceiling is not redundant."""
        ledger = self.ledger(usd_per_request=0.05)
        for _ in range(3):
            ledger.reserve("brave")
        self.assertLess(ledger.committed_requests(), DAILY_REQUESTS)
        with self.assertRaises(SearchBudgetExceeded) as caught:
            ledger.reserve("brave")
        self.assertIn("spend ceiling", str(caught.exception))

    def test_the_usd_ceiling_is_never_exceeded(self) -> None:
        ledger = self.ledger(usd_per_request=0.04)
        while True:
            try:
                ledger.reserve("brave")
            except SearchBudgetExceeded:
                break
        self.assertLessEqual(ledger.committed_usd(), DAILY_USD + 1e-9)

    def test_a_zero_allowance_refuses_everything(self) -> None:
        with self.assertRaises(SearchBudgetExceeded):
            self.ledger(daily_requests=0).reserve("brave")


class SettlementTests(LedgerTestCase):
    def test_settling_confirms_the_flat_price(self) -> None:
        ledger = self.ledger()
        reservation = ledger.reserve("brave")
        self.assertEqual(ledger.settle(reservation), PRICE)
        self.assertAlmostEqual(ledger.committed_usd(), PRICE, places=9)
        self.assertEqual(ledger.committed_requests(), 1)

    def test_abandoning_releases_the_money_and_the_count(self) -> None:
        """A request that never reached the provider cost nothing."""
        ledger = self.ledger()
        reservation = ledger.reserve("brave")
        self.assertEqual(ledger.abandon(reservation, "search_timeout"), 0.0)
        self.assertEqual(ledger.committed_usd(), 0.0)
        self.assertEqual(ledger.committed_requests(), 0)
        self.assertEqual(ledger.remaining_requests(), DAILY_REQUESTS)

    def test_a_reservation_cannot_be_closed_twice(self) -> None:
        """Double settlement would double-count; double release would refund."""
        ledger = self.ledger()
        reservation = ledger.reserve("brave")
        ledger.settle(reservation)
        with self.assertRaises(SearchLedgerCorrupt):
            ledger.settle(reservation)
        with self.assertRaises(SearchLedgerCorrupt):
            ledger.abandon(reservation)

    def test_settling_an_unknown_reservation_fails_closed(self) -> None:
        ledger = self.ledger()
        with self.assertRaises(SearchLedgerCorrupt):
            ledger.settle(SearchReservation("never-recorded", PRICE))

    def test_an_unsettled_reservation_stays_withdrawn(self) -> None:
        """A crash mid-request must not hand the money back."""
        ledger = self.ledger()
        ledger.reserve("brave")
        self.assertEqual(ledger.committed_requests(), 1)
        self.assertAlmostEqual(ledger.committed_usd(), PRICE, places=9)


class RestartTests(LedgerTestCase):
    def test_usage_survives_a_restart(self) -> None:
        first = self.ledger()
        for _ in range(4):
            first.settle(first.reserve("brave"))
        reopened = self.ledger()
        self.assertEqual(reopened.committed_requests(), 4)
        self.assertAlmostEqual(reopened.committed_usd(), 4 * PRICE, places=9)

    def test_a_restart_cannot_hand_back_a_spent_day(self) -> None:
        first = self.ledger()
        for _ in range(DAILY_REQUESTS):
            first.settle(first.reserve("brave"))
        reopened = self.ledger()
        with self.assertRaises(SearchBudgetExceeded):
            reopened.reserve("brave")


class ConcurrencyTests(LedgerTestCase):
    def test_concurrent_reservations_cannot_exceed_the_count(self) -> None:
        """The check and the withdrawal are one transaction, or they race."""
        ledger = self.ledger(daily_requests=10)
        granted: list[SearchReservation] = []
        refused: list[Exception] = []
        lock = threading.Lock()

        def attempt() -> None:
            try:
                reservation = ledger.reserve("brave")
            except SearchBudgetExceeded as error:
                with lock:
                    refused.append(error)
            else:
                with lock:
                    granted.append(reservation)

        threads = [threading.Thread(target=attempt) for _ in range(25)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()

        self.assertEqual(len(granted), 10)
        self.assertEqual(len(refused), 15)
        self.assertEqual(ledger.committed_requests(), 10)

    def test_concurrent_reservations_cannot_exceed_the_usd_ceiling(self) -> None:
        ledger = self.ledger(daily_usd=0.05, daily_requests=1000)
        results: list[bool] = []
        lock = threading.Lock()

        def attempt() -> None:
            try:
                ledger.reserve("brave")
            except SearchBudgetExceeded:
                ok = False
            else:
                ok = True
            with lock:
                results.append(ok)

        threads = [threading.Thread(target=attempt) for _ in range(30)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()

        self.assertEqual(sum(results), 10)
        self.assertLessEqual(ledger.committed_usd(), 0.05 + 1e-9)


class HonestRecordTests(LedgerTestCase):
    """The row must say only true things about a flat-fee search."""

    def columns(self) -> set[str]:
        self.ledger().reserve("brave")
        database = sqlite3.connect(str(self.path))
        try:
            return {
                str(row[1])
                for row in database.execute("PRAGMA table_info(search_spend)")
            }
        finally:
            database.close()

    def test_no_cognition_tier_or_model_is_recorded(self) -> None:
        """A search has neither. Writing one would be a false record."""
        columns = self.columns()
        for absent in ("tier", "model", "kind"):
            self.assertNotIn(absent, columns)

    def test_no_token_counts_are_recorded(self) -> None:
        columns = self.columns()
        for absent in ("input_tokens", "cached_tokens", "output_tokens",
                       "reasoning_tokens"):
            self.assertNotIn(absent, columns)

    def test_every_column_is_meaningful_for_a_search(self) -> None:
        self.assertEqual(
            self.columns(),
            {"reservation_id", "day", "opened_at", "reserved_usd", "actual_usd",
             "settled_at", "provider", "outcome", "failure_code"},
        )

    def test_the_provider_is_recorded_as_given(self) -> None:
        ledger = self.ledger()
        ledger.settle(ledger.reserve("brave"))
        database = sqlite3.connect(str(self.path))
        try:
            row = database.execute(
                "SELECT provider, outcome, actual_usd FROM search_spend"
            ).fetchone()
        finally:
            database.close()
        self.assertEqual(row[0], "brave")
        self.assertEqual(row[1], "settled")
        self.assertAlmostEqual(row[2], PRICE, places=9)


class SeparationTests(LedgerTestCase):
    """Search spend never touches research or autonomous accounting."""

    def test_search_uses_its_own_durable_file(self) -> None:
        self.ledger().reserve("brave")
        self.assertTrue(self.path.exists())
        self.assertEqual(self.path.name, "search-spend.sqlite3")

    def test_no_research_table_is_created(self) -> None:
        self.ledger().reserve("brave")
        database = sqlite3.connect(str(self.path))
        try:
            tables = {
                str(row[0])
                for row in database.execute(
                    "SELECT name FROM sqlite_master WHERE type='table'"
                )
            }
        finally:
            database.close()
        self.assertNotIn("research_spend", tables)
        self.assertIn("search_spend", tables)

    def test_a_research_ledger_sees_no_search_spend(self) -> None:
        from alx.observability import ResearchBudget, SQLiteResearchLedger

        research_path = self.path.parent / "research-spend.sqlite3"
        research = SQLiteResearchLedger(
            research_path, ResearchBudget(daily_usd=1.0, per_request_max_usd=0.007)
        )
        ledger = self.ledger()
        for _ in range(5):
            ledger.settle(ledger.reserve("brave"))
        self.assertEqual(research.committed_usd(), 0.0)
        self.assertAlmostEqual(ledger.committed_usd(), 5 * PRICE, places=9)

    def test_the_search_ledger_does_not_use_model_pricing(self) -> None:
        source = (
            Path(__file__).resolve().parents[1]
            / "src/alx/observability/search_budget.py"
        ).read_text()
        for absent in ("ModelPrice", "USD_PER_MILLION", "price_of", "cost_usd",
                       "worst_case_usd", "tokens"):
            self.assertNotIn(absent, source)


class BudgetValidationTests(unittest.TestCase):
    def test_a_negative_or_absurd_budget_is_refused(self) -> None:
        for values in (
            {"daily_usd": -1.0, "daily_requests": 30, "usd_per_request": 0.005},
            {"daily_usd": 0.15, "daily_requests": -1, "usd_per_request": 0.005},
            {"daily_usd": 0.15, "daily_requests": 30, "usd_per_request": 0.0},
            {"daily_usd": 0.15, "daily_requests": 30, "usd_per_request": -0.005},
            {"daily_usd": float("inf"), "daily_requests": 30, "usd_per_request": 0.005},
        ):
            with self.subTest(values=values):
                with self.assertRaises((ValueError, TypeError)):
                    SearchBudget(**values)

    def test_a_boolean_request_count_is_refused(self) -> None:
        with self.assertRaises(TypeError):
            SearchBudget(daily_usd=0.15, daily_requests=True, usd_per_request=0.005)


class CorruptionTests(LedgerTestCase):
    def test_impossible_totals_fail_closed(self) -> None:
        """A ledger that cannot be trusted stops search rather than guessing."""
        ledger = self.ledger()
        ledger.reserve("brave")
        database = sqlite3.connect(str(self.path))
        try:
            database.execute("UPDATE search_spend SET reserved_usd = -99")
            database.commit()
        finally:
            database.close()
        with self.assertRaises(SearchLedgerCorrupt):
            ledger.reserve("brave")

    def test_an_unreadable_ledger_fails_closed(self) -> None:
        self.path.write_bytes(b"this is not a database")
        with self.assertRaises(SearchLedgerCorrupt):
            self.ledger()


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
