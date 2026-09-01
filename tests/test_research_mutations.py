"""Adversarial tests for the failures a passing suite previously hid.

Each of these reconstructs a real defect that shipped: a ceiling that could be
exceeded, a tiered model callable outside the ledger, a database that broke on
restart, and mail-derived content readable past its deadline. A test that merely
documented a bypass as "not currently exposed" is not a guard, so these assert
the behaviour instead.
"""

from __future__ import annotations

import sqlite3
import sys
import unittest
from datetime import UTC, datetime, timedelta
from pathlib import Path
from tempfile import TemporaryDirectory

REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPOSITORY_ROOT / "src"))

from alx.contracts import (  # noqa: E402
    Cognition,
    EntryKind,
    EntryProposal,
    ModelCompletion,
    ResearchQuery,
    SpecialistQuestion,
    ThreadProposal,
)
from alx.contracts.mail import MailReference  # noqa: E402
from alx.contracts.provenance import ContentOrigin, RetentionPolicy  # noqa: E402
from alx.observability import ConfiguredPricingWorstCase, pricing  # noqa: E402
from alx.observability.research_budget import (  # noqa: E402
    ResearchBudget,
    ResearchBudgetExceeded,
    SQLiteResearchLedger,
)
from alx.research import SQLiteResearchStore  # noqa: E402
from alx.research.store import EXPIRED_CONTENT  # noqa: E402
from alx.specialists import ModelSpecialist, ResearchSpecialist  # noqa: E402


SCHEMA = {
    "type": "object",
    "properties": {"finding": {"type": "string"}},
    "required": ["finding"],
    "additionalProperties": False,
}
NOW = datetime(2026, 9, 1, 10, 0, tzinfo=UTC)


def question(tier: Cognition = Cognition.JUDGE) -> SpecialistQuestion:
    return SpecialistQuestion("q", "Answer.", "material", SCHEMA, cognition=tier)


class GreedyModel:
    """A provider that reports far more usage than the bound allowed."""

    provider, model = "testvendor", "test-model"

    def __init__(self) -> None:
        self.calls = 0

    def complete(self, request):
        self.calls += 1
        return ModelCompletion(
            self.provider,
            self.model,
            {"finding": "answered"},
            {"input_tokens": 1_000_000, "output_tokens": 1_000_000},
        )


class CeilingCannotBeExceededTest(unittest.TestCase):
    """The reported defect: a 0.02 USD ceiling committed a full dollar."""

    def setUp(self) -> None:
        self._directory = TemporaryDirectory()
        self.path = Path(self._directory.name) / "r.db"
        self._prices = dict(pricing.USD_PER_MILLION)
        pricing.USD_PER_MILLION[("testvendor", "test-model")] = (1.0, 0.1, 1.0)

    def tearDown(self) -> None:
        pricing.USD_PER_MILLION.clear()
        pricing.USD_PER_MILLION.update(self._prices)
        self._directory.cleanup()

    def researcher(self, ledger, max_in, max_out, ceiling):
        model = GreedyModel()
        specialist = ModelSpecialist(model, tiers={Cognition.JUDGE: model})
        return model, ResearchSpecialist(
            specialist, ledger, ConfiguredPricingWorstCase(),
            lambda _t: ("testvendor", "test-model"), max_in, max_out, ceiling,
        )

    def test_a_model_whose_worst_case_exceeds_the_ceiling_never_runs(self) -> None:
        """The exact adversarial case from the review, now refused."""
        ledger = SQLiteResearchLedger(self.path, ResearchBudget(0.02, 0.01))
        # 1000 in + 1000 out at 1.00/1.00 per MTok is 0.002, under the ceiling,
        # but the greedy provider reports a thousand times that. The reservation
        # is the worst case of the bound, so settlement cannot exceed it.
        model, researcher = self.researcher(ledger, 1_000, 1_000, 0.01)
        researcher.answer(question())
        self.assertLessEqual(ledger.committed_usd(), 0.02)

    def test_an_unaffordable_bound_is_refused_before_any_call(self) -> None:
        ledger = SQLiteResearchLedger(self.path, ResearchBudget(0.02, 0.01))
        # 1M in + 1M out at 1.00/1.00 is 2.00, far above the 0.01 ceiling.
        model, researcher = self.researcher(ledger, 1_000_000, 1_000_000, 0.01)
        with self.assertRaises(Exception):
            researcher.answer(question())
        self.assertEqual(model.calls, 0)
        self.assertEqual(ledger.committed_usd(), 0.0)

    def test_an_overrun_stops_all_further_research(self) -> None:
        """A ceiling that has already failed must not keep being spent against."""
        from alx.specialists.research import ResearchCeilingFailed

        ledger = SQLiteResearchLedger(self.path, ResearchBudget(1.0, 0.10))
        reservation = ledger.reserve(
            "judge", "testvendor", "test-model", worst_case_usd=0.01
        )
        ledger.settle(reservation, 0.50)  # the bound did not hold
        self.assertGreater(ledger.overrun_usd(), 0.0)
        _model, researcher = self.researcher(ledger, 1_000, 1_000, 0.10)
        with self.assertRaises(ResearchCeilingFailed):
            researcher.answer(question())


class NoDirectTierExecutionTest(unittest.TestCase):
    """Tier models must not be reachable outside the budgeted path."""

    def test_the_composition_root_builds_no_tier_models(self) -> None:
        """Removing them from the runtime is what closes the bypass.

        ModelSpecialist can still hold tiers, because that is how the budgeted
        researcher drives it. What must not exist is a runtime that hands tier
        models to anything else, so the composition root builds none.
        """
        source = (
            REPOSITORY_ROOT / "src" / "alx" / "bootstrap" / "providers.py"
        ).read_text()
        self.assertNotIn("research_tiers", source)
        self.assertNotIn("Cognition", source)
        self.assertNotIn("settings.research", source)

    def test_no_runtime_constructs_a_tiered_specialist(self) -> None:
        for path in (REPOSITORY_ROOT / "src" / "alx" / "bootstrap").rglob("*.py"):
            source = path.read_text()
            self.assertNotIn("tiers=", source, f"{path.name} builds a tiered model")
            self.assertNotIn("ResearchSpecialist", source)

    def test_the_notebook_has_no_production_runtime(self) -> None:
        self.assertFalse(
            (REPOSITORY_ROOT / "src" / "alx" / "bootstrap" / "notebook.py").exists(),
            "a callable notebook runtime exists without recorded authorisation",
        )
        source = (
            REPOSITORY_ROOT / "src" / "alx" / "bootstrap" / "live_voice.py"
        ).read_text()
        self.assertNotIn("notebook", source.lower())


class StaleSchemaRestartTest(unittest.TestCase):
    """A database written before the author column must still open."""

    def test_a_version_one_database_migrates_and_reads(self) -> None:
        with TemporaryDirectory() as directory:
            path = Path(directory) / "old.db"
            legacy = sqlite3.connect(str(path))
            legacy.executescript(
                """
                CREATE TABLE research_threads (thread_id TEXT PRIMARY KEY,
                 question TEXT NOT NULL, interest TEXT NOT NULL,
                 status TEXT NOT NULL, opened_at TEXT NOT NULL,
                 retention_until TEXT NOT NULL, content_origins TEXT,
                 content_recorded_at TEXT, content_expires_at TEXT,
                 mail_references TEXT);
                CREATE TABLE research_entries (entry_id TEXT PRIMARY KEY,
                 thread_id TEXT NOT NULL, kind TEXT NOT NULL);
                CREATE TABLE research_entry_revisions (entry_id TEXT NOT NULL,
                 revision INTEGER NOT NULL, content TEXT NOT NULL, reason TEXT,
                 recorded_at TEXT NOT NULL, source_references TEXT NOT NULL,
                 content_origins TEXT, content_recorded_at TEXT,
                 content_expires_at TEXT, mail_references TEXT,
                 PRIMARY KEY(entry_id, revision));
                CREATE TABLE research_deletions (record_id TEXT PRIMARY KEY,
                 kind TEXT NOT NULL, deleted_at TEXT NOT NULL);
                PRAGMA user_version = 1;
                """
            )
            legacy.execute(
                "INSERT INTO research_threads VALUES "
                "('t','q','i','open',?,?,NULL,NULL,NULL,NULL)",
                (NOW.isoformat(), (NOW + timedelta(days=365)).isoformat()),
            )
            legacy.execute("INSERT INTO research_entries VALUES ('e','t','claim')")
            legacy.execute(
                "INSERT INTO research_entry_revisions VALUES "
                "('e',1,'existing research',NULL,?,'[]',NULL,NULL,NULL,NULL)",
                (NOW.isoformat(),),
            )
            legacy.commit()
            legacy.close()

            store = SQLiteResearchStore(path)
            try:
                entry = store.read_entry("e")
                self.assertEqual(entry.current.content, "existing research")
                # Pre-existing revisions are attributed to AL/X, not to Friedl.
                self.assertEqual(entry.current.author.value, "alx")
            finally:
                store.close()


class ExpiredContentUnreadableTest(unittest.TestCase):
    """Mail-derived content must not outlive D-013 inside a long-lived thread."""

    def setUp(self) -> None:
        self._directory = TemporaryDirectory()
        self.path = Path(self._directory.name) / "r.db"

    def tearDown(self) -> None:
        self._directory.cleanup()

    def test_expired_revision_content_is_not_returned_by_a_read(self) -> None:
        policy = RetentionPolicy()
        mail = policy.direct_mail(NOW, (MailReference("INBOX", "1", "42"),))
        derived = policy.derive(ContentOrigin.ALX, NOW, (mail,))
        # The thread is kept for a year; the quotation for thirty days.
        long_after = NOW + timedelta(days=200)
        store = SQLiteResearchStore(self.path, clock=lambda: long_after)
        try:
            store.open_thread(
                ThreadProposal("t-1", "Q?", "Because.", NOW),
                NOW + timedelta(days=365),
            )
            store.record_entry(
                EntryProposal(
                    "e-1", "t-1", EntryKind.CLAIM, "QUOTED-FROM-MAIL", NOW,
                    provenance=derived,
                )
            )
            self.assertEqual(store.read_entry("e-1").current.content, EXPIRED_CONTENT)
        finally:
            store.close()

    def test_purging_removes_expired_content_but_keeps_the_record(self) -> None:
        policy = RetentionPolicy()
        mail = policy.direct_mail(NOW, (MailReference("INBOX", "1", "42"),))
        derived = policy.derive(ContentOrigin.ALX, NOW, (mail,))
        store = SQLiteResearchStore(self.path, clock=lambda: NOW)
        try:
            store.open_thread(
                ThreadProposal("t-1", "Q?", "Because.", NOW),
                NOW + timedelta(days=365),
            )
            store.record_entry(
                EntryProposal(
                    "e-1", "t-1", EntryKind.CLAIM, "QUOTED-FROM-MAIL", NOW,
                    provenance=derived,
                )
            )
            purged = store.purge_expired_content(NOW + timedelta(days=40))
            self.assertEqual(purged, ("e-1:1",))
            row = store._connection.execute(
                "SELECT content FROM research_entry_revisions WHERE entry_id = 'e-1'"
            ).fetchone()
            self.assertEqual(row["content"], EXPIRED_CONTENT)
            # The entry itself survives; only the quoted material is gone.
            self.assertEqual(store.read_entry("e-1").entry_id, "e-1")
        finally:
            store.close()

    def test_research_of_her_own_is_not_expired(self) -> None:
        far_future = NOW + timedelta(days=5000)
        store = SQLiteResearchStore(self.path, clock=lambda: far_future)
        try:
            store.open_thread(
                ThreadProposal("t-1", "Q?", "Because.", NOW),
                NOW + timedelta(days=9999),
            )
            store.record_entry(
                EntryProposal("e-1", "t-1", EntryKind.DOUBT, "HER OWN DOUBT", NOW)
            )
            self.assertEqual(store.read_entry("e-1").current.content, "HER OWN DOUBT")
        finally:
            store.close()


class RetrievalBoundsMutationTest(unittest.TestCase):
    def test_a_wide_time_window_is_refused_as_a_scope(self) -> None:
        with self.assertRaises(ValueError):
            ResearchQuery(
                query_id="q",
                recorded_after=datetime(1970, 1, 1, tzinfo=UTC),
                recorded_before=datetime(2999, 1, 1, tzinfo=UTC),
            )

    def test_an_open_ended_window_is_refused(self) -> None:
        with self.assertRaises(ValueError):
            ResearchQuery(query_id="q", recorded_after=NOW)

    def test_an_oversized_page_is_refused(self) -> None:
        with self.assertRaises(ValueError):
            ResearchQuery(query_id="q", thread_ids=("t",), limit=10_000)


if __name__ == "__main__":
    unittest.main()
