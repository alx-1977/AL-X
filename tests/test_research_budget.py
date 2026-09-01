"""Autonomous research cannot cross Friedl's hard spending boundary.

The dangerous call is the last one. Output and reasoning tokens are what make a
research question expensive and they are unknown until the model has answered,
so a runtime that checked the budget beforehand and recorded the cost afterwards
would let one final expensive call land outside the ceiling. These tests prove
the reservation closes that gap, and that exhaustion stops research rather than
quietly buying something cheaper.
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPOSITORY_ROOT / "src"))

from alx.contracts import (  # noqa: E402
    Cognition,
    ModelCompletion,
    SpecialistError,
    SpecialistQuestion,
)
from alx.observability import pricing  # noqa: E402
from alx.contracts import ResearchModelUnpriced  # noqa: E402
from alx.observability import ConfiguredPricing  # noqa: E402
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


def question(cognition: Cognition = Cognition.SURVEY) -> SpecialistQuestion:
    return SpecialistQuestion(
        question_id="research-question",
        instruction="Answer the question from the material.",
        material="Some bounded research material.",
        answer_schema=SCHEMA,
        cognition=cognition,
    )


class RecordingModel:
    """A model that returns a fixed answer and a controllable usage report."""

    def __init__(self, provider: str, model: str, usage: dict | None = None) -> None:
        self.provider = provider
        self.model = model
        self._usage = usage if usage is not None else {}
        self.calls = 0

    def complete(self, request):
        self.calls += 1
        return ModelCompletion(
            self.provider, self.model, {"finding": "answered"}, self._usage
        )


class FailingModel:
    def __init__(self) -> None:
        self.calls = 0

    def complete(self, request):
        self.calls += 1
        raise RuntimeError("provider unavailable")


class ResearchBudgetTest(unittest.TestCase):
    def setUp(self) -> None:
        self._directory = TemporaryDirectory()
        self.path = Path(self._directory.name) / "research.db"
        self._prices = dict(pricing.USD_PER_MILLION)
        # A deliberately expensive rate so a single call's real cost is easy to
        # distinguish from its reservation.
        pricing.USD_PER_MILLION[("testvendor", "test-model")] = (10.0, 1.0, 50.0)

    def tearDown(self) -> None:
        pricing.USD_PER_MILLION.clear()
        pricing.USD_PER_MILLION.update(self._prices)
        self._directory.cleanup()

    def ledger(self, daily: float, per_request: float) -> SQLiteResearchLedger:
        return SQLiteResearchLedger(
            self.path, ResearchBudget(daily_usd=daily, per_request_max_usd=per_request)
        )

    def researcher(self, ledger, model, tier=Cognition.SURVEY):
        specialist = ModelSpecialist(model, tiers={tier: model})
        return (
            ResearchSpecialist(
                specialist,
                ledger,
                ConfiguredPricing(),
                lambda _tier: (model.provider, model.model),
            ),
            specialist,
        )

    def test_reservation_holds_the_full_maximum_while_the_call_runs(self) -> None:
        """Mid-flight, the whole per-request maximum is already withdrawn."""
        ledger = self.ledger(daily=10.0, per_request=2.0)
        reservation = ledger.reserve("survey", "testvendor", "test-model")
        self.assertEqual(ledger.committed_usd(), 2.0)
        self.assertEqual(ledger.remaining_usd(), 8.0)
        self.assertEqual(reservation.reserved_usd, 2.0)

    def test_unused_reservation_returns_to_the_day(self) -> None:
        ledger = self.ledger(daily=10.0, per_request=2.0)
        reservation = ledger.reserve("survey", "testvendor", "test-model")
        settled = ledger.settle(reservation, 0.25)
        self.assertEqual(settled, 0.25)
        self.assertEqual(ledger.committed_usd(), 0.25)
        self.assertEqual(ledger.remaining_usd(), 9.75)

    def test_daily_ceiling_cannot_be_exceeded_by_a_final_expensive_call(self) -> None:
        """The whole point: the last call cannot overshoot the boundary.

        Five requests at a two-dollar maximum exactly consume a ten-dollar day.
        The sixth is refused before dispatch, whatever it might have cost.
        """
        ledger = self.ledger(daily=10.0, per_request=2.0)
        for _ in range(5):
            ledger.reserve("judge", "testvendor", "test-model")
        self.assertEqual(ledger.remaining_usd(), 0.0)
        with self.assertRaises(ResearchBudgetExceeded):
            ledger.reserve("judge", "testvendor", "test-model")

    def test_committed_spend_never_exceeds_the_daily_budget(self) -> None:
        """Reserve until refused, settling each at the maximum, and check."""
        ledger = self.ledger(daily=5.0, per_request=1.0)
        reservations = []
        while True:
            try:
                reservations.append(ledger.reserve("judge", "testvendor", "test-model"))
            except ResearchBudgetExceeded:
                break
        for reservation in reservations:
            ledger.settle(reservation, 999.0)  # each tries to overspend
        self.assertLessEqual(ledger.committed_usd(), 5.0)
        self.assertEqual(ledger.remaining_usd(), 0.0)

    def test_a_call_costing_more_than_its_reservation_settles_at_the_maximum(
        self,
    ) -> None:
        ledger = self.ledger(daily=10.0, per_request=2.0)
        reservation = ledger.reserve("judge", "testvendor", "test-model")
        settled = ledger.settle(reservation, 7.5)
        self.assertEqual(settled, 2.0)
        self.assertEqual(ledger.committed_usd(), 2.0)

    def test_exhaustion_stops_research_and_does_not_downgrade_the_tier(self) -> None:
        """No JUDGE to COMPARE to SURVEY slide when money runs short."""
        ledger = self.ledger(daily=2.0, per_request=2.0)
        judge = RecordingModel("testvendor", "test-model", {"output_tokens": 10})
        survey = RecordingModel("testvendor", "test-model", {"output_tokens": 10})
        specialist = ModelSpecialist(
            survey, tiers={Cognition.SURVEY: survey, Cognition.JUDGE: judge}
        )
        researcher = ResearchSpecialist(
            specialist, ledger, ConfiguredPricing(), lambda _tier: ("testvendor", "test-model")
        )
        researcher.answer(question(Cognition.JUDGE))
        self.assertEqual(judge.calls, 1)
        with self.assertRaises(ResearchBudgetExceeded):
            researcher.answer(question(Cognition.JUDGE))
        # Refused outright: the cheaper tier was never asked to stand in.
        self.assertEqual(judge.calls, 1)
        self.assertEqual(survey.calls, 0)

    def test_exhaustion_does_not_fall_back_to_another_provider(self) -> None:
        ledger = self.ledger(daily=1.0, per_request=1.0)
        primary = RecordingModel("testvendor", "test-model", {"output_tokens": 5})
        other = RecordingModel("othervendor", "other-model", {"output_tokens": 5})
        pricing.USD_PER_MILLION[("othervendor", "other-model")] = (1.0, 1.0, 1.0)
        specialist = ModelSpecialist(primary, tiers={Cognition.SURVEY: primary})
        researcher = ResearchSpecialist(
            specialist, ledger, ConfiguredPricing(), lambda _tier: ("testvendor", "test-model")
        )
        researcher.answer(question())
        with self.assertRaises(ResearchBudgetExceeded):
            researcher.answer(question())
        self.assertEqual(other.calls, 0)

    def test_unpriced_model_cannot_spend_and_is_refused_before_dispatch(self) -> None:
        ledger = self.ledger(daily=10.0, per_request=1.0)
        model = RecordingModel("unpricedvendor", "mystery-model", {"output_tokens": 5})
        researcher, _ = self.researcher(ledger, model)
        with self.assertRaises(ResearchModelUnpriced):
            researcher.answer(question())
        self.assertEqual(model.calls, 0)
        self.assertEqual(ledger.committed_usd(), 0.0)

    def test_price_is_never_guessed_for_an_unknown_model(self) -> None:
        self.assertIsNone(pricing.price_of("testvendor", "not-configured"))
        self.assertFalse(pricing.is_priced("testvendor", "not-configured"))
        self.assertIsNone(
            pricing.cost_usd("testvendor", "not-configured", {"output_tokens": 1000})
        )

    def test_failed_call_releases_its_reservation(self) -> None:
        ledger = self.ledger(daily=10.0, per_request=2.0)
        model = FailingModel()
        model.provider, model.model = "testvendor", "test-model"
        specialist = ModelSpecialist(model, tiers={Cognition.SURVEY: model})
        researcher = ResearchSpecialist(
            specialist, ledger, ConfiguredPricing(), lambda _tier: ("testvendor", "test-model")
        )
        with self.assertRaises(SpecialistError):
            researcher.answer(question())
        self.assertEqual(ledger.committed_usd(), 0.0)
        self.assertEqual(ledger.remaining_usd(), 10.0)

    def test_unmeasured_usage_settles_at_the_full_reservation(self) -> None:
        """An unmeasured call is not free; treating it so would uncap the day."""
        ledger = self.ledger(daily=10.0, per_request=2.0)
        model = RecordingModel("testvendor", "test-model", {})
        researcher, _ = self.researcher(ledger, model)
        researcher.answer(question())
        self.assertEqual(ledger.committed_usd(), 2.0)

    def test_measured_cost_is_recorded_and_the_remainder_returned(self) -> None:
        ledger = self.ledger(daily=10.0, per_request=2.0)
        # 1M output tokens at 50 USD/1M would be 50; 1000 tokens is 0.05.
        model = RecordingModel(
            "testvendor", "test-model", {"input_tokens": 1000, "output_tokens": 1000}
        )
        researcher, _ = self.researcher(ledger, model)
        researcher.answer(question())
        expected = round(1000 / 1e6 * 10.0 + 1000 / 1e6 * 50.0, 6)
        self.assertAlmostEqual(ledger.committed_usd(), expected, places=6)
        self.assertLess(ledger.committed_usd(), 2.0)

    def test_budget_survives_restart(self) -> None:
        """A restart must not hand AL/X a fresh day's money."""
        ledger = self.ledger(daily=10.0, per_request=2.0)
        ledger.reserve("survey", "testvendor", "test-model")
        reopened = self.ledger(daily=10.0, per_request=2.0)
        self.assertEqual(reopened.committed_usd(), 2.0)
        self.assertEqual(reopened.remaining_usd(), 8.0)

    def test_unreconciled_reservation_stays_withdrawn(self) -> None:
        """A crashed call keeps its money withdrawn rather than leaking it."""
        ledger = self.ledger(daily=4.0, per_request=2.0)
        ledger.reserve("judge", "testvendor", "test-model")
        reopened = self.ledger(daily=4.0, per_request=2.0)
        reopened.reserve("judge", "testvendor", "test-model")
        with self.assertRaises(ResearchBudgetExceeded):
            reopened.reserve("judge", "testvendor", "test-model")

    def test_a_budget_that_cannot_afford_one_request_is_rejected(self) -> None:
        with self.assertRaises(ValueError):
            ResearchBudget(daily_usd=1.0, per_request_max_usd=2.0)


if __name__ == "__main__":
    unittest.main()
