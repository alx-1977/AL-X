"""D-023 (amended 2026-10-14): research on AL/X's own Claude subscription.

Friedl: research uses her Claude account (Haiku for quick look-ups, Opus for
hard questions) instead of a metered key, bounded by a daily call cap because
the subscription has no per-token price for a dollar ceiling to measure.
"""

from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from alx.config import ConfigurationError  # noqa: E402
from alx.config.settings import _research_settings  # noqa: E402
from alx.observability.pricing import ConfiguredPricingWorstCase  # noqa: E402
from alx.observability.research_budget import (  # noqa: E402
    ResearchBudget,
    ResearchCallsExhausted,
    SQLiteResearchLedger,
)

SUBSCRIPTION = {
    "ALX_RESEARCH_ENABLED_TIERS": "survey,judge",
    "ALX_RESEARCH_SURVEY_PROVIDER": "claude_subscription",
    "ALX_RESEARCH_SURVEY_MODEL": "claude-haiku-5-5",
    "ALX_RESEARCH_JUDGE_PROVIDER": "claude_subscription",
    "ALX_RESEARCH_JUDGE_MODEL": "claude-opus-5-5",
    "ALX_RESEARCH_DAILY_CALLS": "30",
    "RESEARCH_DAILY_BUDGET_USD": "1",
    "RESEARCH_PER_REQUEST_MAX_USD": "0.1",
    "ALX_SPECIALIST_PROVIDER": "none",
}


class SubscriptionTierTests(unittest.TestCase):
    def test_tiers_on_the_subscription_need_no_key(self) -> None:
        settings = _research_settings(SUBSCRIPTION, "claude_subscription")
        self.assertEqual(settings.enabled_tiers, frozenset({"survey", "judge"}))
        self.assertEqual(
            (settings.survey.provider, settings.survey.model, settings.survey.api_key),
            ("claude_subscription", "claude-haiku-5-5", ""),
        )
        self.assertEqual(settings.judge.model, "claude-opus-5-5")
        self.assertEqual(settings.limits.daily_calls, 30)

    def test_a_key_set_for_a_subscription_tier_is_refused(self) -> None:
        with self.assertRaises(ConfigurationError):
            _research_settings(
                {**SUBSCRIPTION, "ALX_RESEARCH_SURVEY_API_KEY": "sk-x"},
                "claude_subscription",
            )

    def test_a_subscription_tier_must_name_its_model(self) -> None:
        environment = dict(SUBSCRIPTION)
        del environment["ALX_RESEARCH_SURVEY_MODEL"]
        with self.assertRaises(ConfigurationError):
            _research_settings(environment, "claude_subscription")

    def test_the_subscription_models_are_priced_at_zero(self) -> None:
        pricing = ConfiguredPricingWorstCase()
        for model in ("claude-haiku-5-5", "claude-sonnet-5-5", "claude-opus-5-5"):
            with self.subTest(model=model):
                self.assertEqual(
                    pricing.worst_case_usd("claude_subscription", model, 8_000, 4_000), 0.0
                )


class DailyCallCapTests(unittest.TestCase):
    def ledger(self, calls):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        return SQLiteResearchLedger(
            Path(directory.name) / "research.sqlite3",
            ResearchBudget(daily_usd=1.0, per_request_max_usd=0.1, daily_calls=calls),
        )

    def test_research_stops_at_the_daily_call_cap(self) -> None:
        ledger = self.ledger(3)
        for _ in range(3):
            ledger.reserve("survey", "claude_subscription", "claude-haiku-5-5",
                           worst_case_usd=0.0)
        with self.assertRaises(ResearchCallsExhausted):
            ledger.reserve("survey", "claude_subscription", "claude-haiku-5-5",
                           worst_case_usd=0.0)

    def test_no_cap_means_only_the_dollar_ceiling(self) -> None:
        ledger = self.ledger(0)
        for _ in range(50):
            ledger.reserve("survey", "claude_subscription", "claude-haiku-5-5",
                           worst_case_usd=0.0)

    def test_a_negative_cap_is_refused(self) -> None:
        with self.assertRaises(ValueError):
            ResearchBudget(daily_usd=1.0, per_request_max_usd=0.1, daily_calls=-1)


if __name__ == "__main__":
    unittest.main()
