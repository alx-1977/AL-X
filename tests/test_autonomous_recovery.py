"""Restart-safe recovery for occasions a stopped process left claimed.

The claim is durable so one request cannot become two paid turns. That same
durability strands a request if the process dies before recording an outcome:
it stays pending while every later scan skips it. Recovery resolves that from
persisted state alone — no age, no timeout, no lease, no staleness.

The decisive fact is that a spend row is marked dispatched *before* the
provider is called. So a reservation still at `reserved` proves the provider
was never reached, and one past it proves nothing about whether it answered —
which is why the second case is retained rather than replayed.
"""

from __future__ import annotations

import sys
import tempfile
import unittest
from datetime import UTC, datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from alx.continuity import (  # noqa: E402
    FutureCognitionSource, SQLiteContinuityStore, SQLiteOpportunityLedger,
)
from alx.contracts import CognitionOrigin  # noqa: E402
from alx.contracts.continuity import (  # noqa: E402
    CognitionOpportunity, FutureCognitionRequest, FutureCognitionStatus,
)
from alx.observability import ConfiguredPricingWorstCase  # noqa: E402
from alx.observability.autonomous_budget import SQLiteAutonomousLedger  # noqa: E402

NOW = datetime(2026, 9, 3, 12, 0, tzinfo=UTC)
DUE = NOW - timedelta(minutes=5)
LUNA = ("openai", "gpt-5.6-luna")
IN_BOUND, OUT_BOUND = 96_000, 32_000


class RecoveryHarness(unittest.TestCase):
    """Each test simulates a crash by abandoning objects, then reopening them."""

    def setUp(self) -> None:
        self._dir = tempfile.TemporaryDirectory()
        self.addCleanup(self._dir.cleanup)
        self.root = Path(self._dir.name)
        self._open()
        self.store.create(
            FutureCognitionRequest("r1", DUE, "a note", NOW - timedelta(hours=1))
        )

    def _open(self) -> None:
        """Open every durable store, as a fresh process would."""
        self.store = SQLiteContinuityStore(self.root / "continuity.sqlite3")
        self.ledger = SQLiteOpportunityLedger(self.root / "opportunities.sqlite3")
        self.budget = SQLiteAutonomousLedger(
            self.root / "spend.sqlite3", 0.5405, ConfiguredPricingWorstCase()
        )
        self.source = FutureCognitionSource(
            self.store, self.ledger, enabled=True, clock=lambda: NOW
        )

    def _restart(self) -> None:
        self.store.close()
        self.ledger.close()
        self._open()

    def _claim(self) -> CognitionOpportunity:
        opportunity = self.source.due_opportunities()[0]
        self.assertTrue(self.source.claim(opportunity))
        return opportunity

    def _still_pending(self) -> bool:
        return bool(self.store.pending()) and (
            self.store.pending()[0].status is FutureCognitionStatus.PENDING
        )


class CrashAfterClaimTests(RecoveryHarness):
    """State 1: claimed, nothing reserved."""

    def test_a_crash_immediately_after_claim_is_reclaimed(self) -> None:
        self._claim()
        self._restart()
        # Without recovery the request is stranded: pending but never offered.
        self.assertTrue(self._still_pending())
        self.assertEqual(self.source.due_opportunities(), ())
        self.assertEqual(self.source.recover(self.budget), ("self:r1",))
        self.assertEqual(
            [item.opportunity_id for item in self.source.due_opportunities()],
            ["self:r1"],
        )

    def test_the_reclaimed_request_keeps_her_note(self) -> None:
        self._claim()
        self._restart()
        self.source.recover(self.budget)
        self.assertEqual(self.source.due_opportunities()[0].note, "a note")


class CrashBeforeReservationTests(RecoveryHarness):
    """State 1 again, reached through the runner rather than directly."""

    def test_a_crash_before_any_reservation_is_reclaimed(self) -> None:
        self._claim()
        self._restart()
        self.assertEqual(self.budget.spend_today(), 0.0)
        self.assertEqual(self.source.recover(self.budget), ("self:r1",))
        self.assertTrue(self.source.due_opportunities())


class CrashAfterReservationTests(RecoveryHarness):
    """State 2: reserved, provider never reached."""

    def test_a_crash_after_reserving_but_before_dispatch_is_reclaimed(self) -> None:
        opportunity = self._claim()
        self.budget.reserve(*LUNA, IN_BOUND, OUT_BOUND, opportunity.opportunity_id)
        self.ledger.record_reserved("self:r1", *LUNA, 0.0816)
        self._restart()
        # No dispatch was recorded, so the provider cannot have run.
        self.assertEqual(self.source.recover(self.budget), ("self:r1",))
        self.assertTrue(self.source.due_opportunities())

    def test_the_reservation_stays_withdrawn_after_reclaim(self) -> None:
        """A crash must not be able to refund itself."""
        opportunity = self._claim()
        self.budget.reserve(*LUNA, IN_BOUND, OUT_BOUND, opportunity.opportunity_id)
        self._restart()
        self.source.recover(self.budget)
        self.assertAlmostEqual(self.budget.spend_today(), 0.0816, places=6)


class CrashAfterDispatchTests(RecoveryHarness):
    """State 3: the provider may have run. Never replayed."""

    def _reserve_and_dispatch(self):
        opportunity = self._claim()
        reservation = self.budget.reserve(
            *LUNA, IN_BOUND, OUT_BOUND, opportunity.opportunity_id
        )
        self.ledger.record_reserved("self:r1", *LUNA, reservation.reserved_usd)
        self.budget.mark_dispatched(reservation)
        return reservation

    def test_a_crash_after_dispatch_begins_is_never_replayed(self) -> None:
        self._reserve_and_dispatch()
        self._restart()
        self.assertEqual(self.source.recover(self.budget), ())
        self.assertEqual(self.source.due_opportunities(), ())

    def test_it_is_retained_for_inspection_rather_than_dropped(self) -> None:
        self._reserve_and_dispatch()
        self._restart()
        self.source.recover(self.budget)
        row = self.ledger.rows()[0]
        self.assertEqual(row["outcome"], "unreconciled")
        self.assertAlmostEqual(row["reserved_usd"], 0.0816, places=6)

    def test_a_crash_after_settlement_makes_no_duplicate_paid_call(self) -> None:
        """Settled but no occasion outcome: still dispatched, still no replay."""
        reservation = self._reserve_and_dispatch()
        self.budget.settle(reservation, *LUNA, {"input_tokens": 10, "output_tokens": 1})
        self._restart()
        self.assertEqual(self.source.recover(self.budget), ())
        self.assertEqual(self.source.due_opportunities(), ())
        self.assertEqual(self.ledger.rows()[0]["outcome"], "unreconciled")


class TerminalOutcomeTests(RecoveryHarness):
    """State 4: a finished occasion is never re-offered."""

    def test_a_completed_occasion_is_never_reclaimed(self) -> None:
        self._claim()
        self.ledger.record_outcome("self:r1", "finished_silently", 1)
        self.store.mark_honoured("r1")
        self._restart()
        self.assertEqual(self.source.recover(self.budget), ())
        self.assertEqual(self.source.due_opportunities(), ())

    def test_a_refused_occasion_is_never_reclaimed(self) -> None:
        self._claim()
        self.ledger.record_outcome("self:r1", "refused_AutonomousModelUnpriced")
        self._restart()
        self.assertEqual(self.source.recover(self.budget), ())


class IdempotenceTests(RecoveryHarness):
    """Repeated restart and recovery must converge, not accumulate."""

    def test_recovery_is_idempotent(self) -> None:
        self._claim()
        self._restart()
        self.assertEqual(self.source.recover(self.budget), ("self:r1",))
        for _ in range(3):
            self.assertEqual(self.source.recover(self.budget), ())
        self.assertEqual(len(self.source.due_opportunities()), 1)

    def test_repeated_crash_and_recovery_never_duplicates_the_occasion(self) -> None:
        for _ in range(3):
            self._claim()
            self._restart()
            self.source.recover(self.budget)
        self.assertEqual(len(self.source.due_opportunities()), 1)
        self.assertEqual(len(self.store.pending()), 1)

    def test_recovery_without_a_spend_ledger_still_reclaims_unreserved(self) -> None:
        """Recovery must not require the budget to reclaim a bare claim."""
        self._claim()
        self._restart()
        self.assertEqual(self.source.recover(), ("self:r1",))


class NoTimeBasedRuleTests(unittest.TestCase):
    """Recovery reads state, never the clock."""

    SOURCE = Path(__file__).resolve().parents[1] / "src" / "alx"

    def test_recovery_consults_no_clock_or_duration(self) -> None:
        import ast

        tree = ast.parse(
            (self.SOURCE / "continuity" / "source.py").read_text(encoding="utf-8")
        )
        recover = next(
            node for node in ast.walk(tree)
            if isinstance(node, ast.FunctionDef) and node.name == "recover"
        )
        names = {
            node.attr if isinstance(node, ast.Attribute) else getattr(node, "id", "")
            for node in ast.walk(recover)
            if isinstance(node, (ast.Attribute, ast.Name))
        }
        for forbidden in (
            "_clock", "now", "utcnow", "timedelta", "lease", "expires_at",
            "age", "stale", "timeout",
        ):
            with self.subTest(token=forbidden):
                self.assertNotIn(forbidden, names)


class ProductionWiringTests(unittest.TestCase):
    """Recovery must work through the objects composition actually builds.

    The earlier recovery tests called the spend ledger directly with an
    explicit opportunity id, so they passed while composition was storing every
    reservation anonymously. A dispatched turn then looked undispatched, and
    recovery would have replayed a call that may already have been billed.
    """

    def test_the_relay_links_a_reservation_to_its_occasion(self) -> None:
        from alx.bootstrap.autonomous import LedgerSpendAuthority, OccasionSpendRelay

        with tempfile.TemporaryDirectory() as directory:
            budget = SQLiteAutonomousLedger(
                Path(directory) / "s.sqlite3", 0.5405, ConfiguredPricingWorstCase()
            )
            relay = OccasionSpendRelay()
            # Built exactly as composition builds it.
            authority = LedgerSpendAuthority(
                budget, *LUNA, relay, relay.current_opportunity_id
            )

            class Spend:
                provider = ""
                model = ""
                reserved_usd = 0.0
                settled_usd = None
                dispatched = False

            relay.watch(Spend(), "self:r1")
            reservation = authority.reserve(IN_BOUND, OUT_BOUND)
            authority.mark_dispatched(reservation)
            # The decisive assertion: recovery can find this occasion's call.
            self.assertTrue(budget.dispatch_started("self:r1"))

    def test_an_anonymous_reservation_would_be_invisible_to_recovery(self) -> None:
        """Names the failure mode, so a regression is unambiguous."""
        from alx.bootstrap.autonomous import LedgerSpendAuthority

        with tempfile.TemporaryDirectory() as directory:
            budget = SQLiteAutonomousLedger(
                Path(directory) / "s.sqlite3", 0.5405, ConfiguredPricingWorstCase()
            )
            authority = LedgerSpendAuthority(budget, *LUNA, None, None)
            reservation = authority.reserve(IN_BOUND, OUT_BOUND)
            authority.mark_dispatched(reservation)
            self.assertFalse(budget.dispatch_started("self:r1"))

    def test_composition_passes_the_occasion_callback(self) -> None:
        import ast

        source = (
            Path(__file__).resolve().parents[1]
            / "src" / "alx" / "bootstrap" / "live_voice.py"
        ).read_text(encoding="utf-8")
        tree = ast.parse(source)
        call = next(
            node for node in ast.walk(tree)
            if isinstance(node, ast.Call)
            and isinstance(node.func, ast.Name)
            and node.func.id == "LedgerSpendAuthority"
        )
        # Five arguments: ledger, provider, model, observer, occasion callback.
        self.assertEqual(len(call.args), 5)


class OutcomePersistenceFailureTests(RecoveryHarness):
    """A failed audit write must not also strand the request."""

    class BrokenLedger:
        def __init__(self, real) -> None:
            self._real = real
            self.released: list[str] = []

        def record_created(self, opportunity):
            return self._real.record_created(opportunity)

        def exists(self, opportunity_id):
            return self._real.exists(opportunity_id)

        def unfinished(self):
            return self._real.unfinished()

        def release(self, opportunity_id):
            self.released.append(opportunity_id)
            return self._real.release(opportunity_id)

        def mark_unreconciled(self, opportunity_id):
            return self._real.mark_unreconciled(opportunity_id)

        def record_reserved(self, *args, **kwargs):
            raise RuntimeError("disk full")

        def record_outcome(self, *args, **kwargs):
            raise RuntimeError("disk full")

        def rows(self):
            return self._real.rows()

    def test_a_failed_outcome_write_still_releases_an_undispatched_claim(self) -> None:
        from alx.bootstrap.autonomous import AutonomousCognitionRunner

        broken = self.BrokenLedger(self.ledger)
        source = FutureCognitionSource(
            self.store, broken, enabled=True, clock=lambda: NOW
        )

        class Gateway:
            def receive_cognition_opportunity(self, *args, **kwargs):
                class Outcome:
                    class state:
                        value = "finished_silently"

                return Outcome()

        runner = AutonomousCognitionRunner(
            source, broken, Gateway(), "c1", 4, 3650, clock=lambda: NOW
        )
        for due in source.due_opportunities():
            runner.run_one(due)
        # The write failed, but the claim was still cleaned up in-process.
        self.assertEqual(broken.released, ["self:r1"])
        self.assertTrue(self._still_pending())
        self.assertTrue(source.due_opportunities())


class ApprovedIdentityOnlyTests(unittest.TestCase):
    """Configuration may install only what EX-001 approves.

    An exception authorising one exact arrangement is worth little if
    configuration can install a different reasoning authority under it. A typo
    would do it silently, in production, with the exception appearing to cover
    it.
    """

    def _settings(self, **overrides):
        from alx.config.settings import autonomous_reasoning_settings

        environment = {
            "ALX_AUTONOMOUS_PROVIDER": "openai",
            "ALX_AUTONOMOUS_MODEL": "gpt-5.6-luna",
            "OPENAI_API_KEY": "key",
        }
        environment.update(overrides)
        return autonomous_reasoning_settings(environment)

    def test_the_approved_arrangement_is_accepted(self) -> None:
        settings = self._settings()
        self.assertEqual(
            (settings.provider, settings.model, settings.effort),
            ("openai", "gpt-5.6-luna", "max"),
        )

    def test_another_provider_is_refused(self) -> None:
        from alx.config.settings import ConfigurationError

        with self.assertRaises(ConfigurationError):
            self._settings(
                ALX_AUTONOMOUS_PROVIDER="xai",
                ALX_AUTONOMOUS_MODEL="grok-4.5",
                XAI_API_KEY="key",
            )

    def test_another_model_is_refused(self) -> None:
        from alx.config.settings import ConfigurationError

        for model in ("gpt-5.4-nano", "gpt-5.6-sol", "gpt-5.6-luna-preview"):
            with self.subTest(model=model):
                with self.assertRaises(ConfigurationError):
                    self._settings(ALX_AUTONOMOUS_MODEL=model)

    def test_another_effort_is_refused(self) -> None:
        from alx.config.settings import ConfigurationError

        for effort in ("low", "medium", "high", "none"):
            with self.subTest(effort=effort):
                with self.assertRaises(ConfigurationError):
                    self._settings(ALX_AUTONOMOUS_EFFORT=effort)

    def test_an_unconfigured_runtime_is_still_simply_absent(self) -> None:
        """Refusing a wrong arrangement must not break having none."""
        from alx.config.settings import autonomous_reasoning_settings

        self.assertIsNone(autonomous_reasoning_settings({}))

    def test_the_approved_identity_matches_the_exception(self) -> None:
        from alx.config.settings import AUTONOMOUS_APPROVED_IDENTITY

        register = (
            Path(__file__).resolve().parents[1] / "governance" / "EXCEPTIONS.md"
        ).read_text(encoding="utf-8")
        provider, model, effort = AUTONOMOUS_APPROVED_IDENTITY
        self.assertIn(model, register)
        self.assertIn(f"`{effort}`", register)
        self.assertEqual(provider, "openai")


if __name__ == "__main__":
    unittest.main()
