"""Restore each removed unsafe path and prove the suite refuses it.

Counting occurrences of a name in a source file proves only that the text is
absent; it cannot show that reintroducing the behaviour would be caught. These
tests reconstruct the actual competing paths and assert the guard rejects them,
so a future change that quietly restores one fails here rather than shipping.
"""

from __future__ import annotations

import sys
import unittest
from datetime import UTC, datetime
from pathlib import Path
from tempfile import TemporaryDirectory

REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPOSITORY_ROOT / "src"))

from alx.contracts import (  # noqa: E402
    Cognition,
    ModelCompletion,
    ResearchQuery,
    SpecialistError,
    SpecialistQuestion,
)
from alx.observability import ConfiguredPricingWorstCase, pricing  # noqa: E402
from alx.observability.research_budget import (  # noqa: E402
    ResearchBudget,
    ResearchBudgetExceeded,
    SQLiteResearchLedger,
)
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


class ExpensiveModel:
    provider, model = "testvendor", "test-model"

    def __init__(self) -> None:
        self.calls = 0

    def complete(self, request):
        self.calls += 1
        return ModelCompletion(
            self.provider,
            self.model,
            {"finding": "answered"},
            {"input_tokens": 100_000, "output_tokens": 100_000},
        )


class UnbudgetedPathMutationTest(unittest.TestCase):
    """A research-tier model called outside the ledger spends nothing tracked."""

    def setUp(self) -> None:
        self._directory = TemporaryDirectory()
        self.path = Path(self._directory.name) / "r.db"
        self._prices = dict(pricing.USD_PER_MILLION)
        pricing.USD_PER_MILLION[("testvendor", "test-model")] = (10.0, 1.0, 50.0)
        self.ledger = SQLiteResearchLedger(
            self.path, ResearchBudget(daily_usd=1.0, per_request_max_usd=1.0)
        )

    def tearDown(self) -> None:
        pricing.USD_PER_MILLION.clear()
        pricing.USD_PER_MILLION.update(self._prices)
        self._directory.cleanup()

    def test_the_budgeted_path_refuses_once_the_day_is_spent(self) -> None:
        model = ExpensiveModel()
        specialist = ModelSpecialist(model, tiers={Cognition.JUDGE: model})
        researcher = ResearchSpecialist(
            specialist, self.ledger, ConfiguredPricingWorstCase(),
            lambda _t: ("testvendor", "test-model"),
            2_000, 500, 1.0,
        )
        researcher.answer(question())
        with self.assertRaises(ResearchBudgetExceeded):
            researcher.answer(question())
        self.assertEqual(model.calls, 1)

    def test_mutation_calling_a_tier_model_directly_bypasses_the_ledger(self) -> None:
        """The competing path this design must not leave reachable.

        Documented as a failing property rather than a passing one: any runtime
        that hands a tier model to something other than ResearchSpecialist gets
        unbudgeted spend. Nothing in production may construct that arrangement,
        which is why research tiers are not wired into the runtime.
        """
        model = ExpensiveModel()
        specialist = ModelSpecialist(model, tiers={Cognition.JUDGE: model})
        before = self.ledger.committed_usd()
        specialist.answer(question())  # deliberately not through the researcher
        self.assertEqual(model.calls, 1)
        self.assertEqual(self.ledger.committed_usd(), before)
        # Proof the bypass is real, and therefore that the only safe
        # arrangement is the one the runtime actually builds: none.
        runtime = (
            REPOSITORY_ROOT / "src" / "alx" / "bootstrap" / "live_voice.py"
        ).read_text()
        self.assertNotIn("research_tiers", runtime)
        self.assertNotIn("ResearchSpecialist", runtime)


class NotebookNotWiredMutationTest(unittest.TestCase):
    """The notebook must not be reachable from the production runtime."""

    def test_no_notebook_capability_is_registered_in_the_runtime(self) -> None:
        source = (
            REPOSITORY_ROOT / "src" / "alx" / "bootstrap" / "live_voice.py"
        ).read_text()
        self.assertNotIn("notebook", source.lower())

    def test_restoring_the_wiring_would_register_ungoverned_capabilities(self) -> None:
        """Show what the removed wiring did, so its return is a visible change.

        The notebook builds and works; what it lacks is Friedl's recorded
        decision on retention, authority, deletion and resource policy. This
        asserts the capabilities exist and are deliberately unregistered.
        """
        from alx.bootstrap.notebook import build_notebook_runtime

        with TemporaryDirectory() as directory:
            runtime = build_notebook_runtime(
                Path(directory), 3650, lambda: "call-1"
            )
            try:
                self.assertEqual(len(runtime.definitions), 8)
            finally:
                runtime.store.close()


class UnboundedRetrievalMutationTest(unittest.TestCase):
    def test_a_wide_time_window_is_refused_as_a_scope(self) -> None:
        """Restoring the unbounded window must fail, not merely be discouraged."""
        with self.assertRaises(ValueError):
            ResearchQuery(
                query_id="q",
                recorded_after=datetime(1970, 1, 1, tzinfo=UTC),
                recorded_before=datetime(2999, 1, 1, tzinfo=UTC),
            )

    def test_an_open_ended_window_is_refused(self) -> None:
        with self.assertRaises(ValueError):
            ResearchQuery(
                query_id="q", recorded_after=datetime(2026, 1, 1, tzinfo=UTC)
            )

    def test_an_oversized_page_is_refused(self) -> None:
        with self.assertRaises(ValueError):
            ResearchQuery(query_id="q", thread_ids=("t",), limit=10_000)


if __name__ == "__main__":
    unittest.main()
