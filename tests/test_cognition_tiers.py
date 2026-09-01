"""A cognition tier buys thinking; it does not decide what AL/X thinks about.

Tiers are cognitive difficulty, not subject matter. A question about jellyfish
may be genuinely hard and a question about power electronics may be a lookup,
so nothing here may infer a tier from a topic, a keyword or the user's wording.
AL/X sets the tier as a structured value when she composes the question.

These tests also prove the tiers remain one production path under Law 0: three
configurations of the existing bounded-specialist call, not three routes.
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPOSITORY_ROOT / "src"))

from alx.contracts import (  # noqa: E402
    Cognition,
    SpecialistError,
    ModelCompletion,
    ModelRole,
    SpecialistQuestion,
)
from alx.specialists import ModelSpecialist  # noqa: E402


SCHEMA = {
    "type": "object",
    "properties": {"finding": {"type": "string"}},
    "required": ["finding"],
    "additionalProperties": False,
}


class TierModel:
    def __init__(self, name: str) -> None:
        self.name = name
        self.calls = 0
        self.requests = []

    def complete(self, request):
        self.calls += 1
        self.requests.append(request)
        return ModelCompletion("testvendor", self.name, {"finding": self.name}, {})


def question(cognition: Cognition, material: str = "material") -> SpecialistQuestion:
    return SpecialistQuestion(
        question_id="q",
        instruction="Answer from the material.",
        material=material,
        answer_schema=SCHEMA,
        cognition=cognition,
    )


class CognitionTierTest(unittest.TestCase):
    def setUp(self) -> None:
        self.survey = TierModel("survey-model")
        self.compare = TierModel("compare-model")
        self.judge = TierModel("judge-model")
        self.specialist = ModelSpecialist(
            self.survey,
            tiers={
                Cognition.SURVEY: self.survey,
                Cognition.COMPARE: self.compare,
                Cognition.JUDGE: self.judge,
            },
        )

    def test_each_tier_reaches_its_configured_model(self) -> None:
        self.specialist.answer(question(Cognition.SURVEY))
        self.specialist.answer(question(Cognition.COMPARE))
        self.specialist.answer(question(Cognition.JUDGE))
        self.assertEqual(
            (self.survey.calls, self.compare.calls, self.judge.calls), (1, 1, 1)
        )

    def test_the_tier_is_a_value_not_an_inference_from_the_subject(self) -> None:
        """The same words at different tiers, and the same tier for opposites.

        Topic cannot move the call. A jellyfish question at JUDGE reaches the
        frontier model, and an engineering question at SURVEY reaches the cheap
        one: exactly the inversion topic routing would get wrong.
        """
        self.specialist.answer(question(Cognition.JUDGE, "Why do jellyfish age?"))
        self.assertEqual(self.judge.calls, 1)
        self.assertEqual(self.survey.calls, 0)

        self.specialist.answer(
            question(Cognition.SURVEY, "List the pin numbers in this datasheet.")
        )
        self.assertEqual(self.survey.calls, 1)
        self.assertEqual(self.judge.calls, 1)

    def test_identical_material_routes_purely_by_the_declared_tier(self) -> None:
        text = "The same sentence, considered twice."
        self.specialist.answer(question(Cognition.SURVEY, text))
        self.specialist.answer(question(Cognition.JUDGE, text))
        self.assertEqual(self.survey.calls, 1)
        self.assertEqual(self.judge.calls, 1)

    def test_default_tier_is_the_cheapest(self) -> None:
        """Nothing silently buys expensive thinking it was not asked for."""
        default = SpecialistQuestion(
            question_id="q",
            instruction="Answer from the material.",
            material="material",
            answer_schema=SCHEMA,
        )
        self.assertIs(default.cognition, Cognition.SURVEY)

    def test_an_unconfigured_tier_refuses_rather_than_substituting(self) -> None:
        """Silent substitution would run a tier AL/X did not choose.

        Falling back would answer a hard question with a cheap model, or a cheap
        question with an expensive one, and the tier would stop meaning anything.
        A misconfiguration is made visible instead.
        """
        specialist = ModelSpecialist(self.survey, tiers={Cognition.SURVEY: self.survey})
        with self.assertRaises(SpecialistError) as caught:
            specialist.answer(question(Cognition.JUDGE))
        self.assertIn("cognition_tier_unconfigured", str(caught.exception))
        self.assertEqual(self.survey.calls, 0)
        self.assertEqual(self.judge.calls, 0)

    def test_a_tier_carries_no_laws_identity_catalogue_or_goal(self) -> None:
        """A tiered call is still a bounded question, not the Core.

        Raising the tier buys a better model for one question. It must not turn
        the call into AL/X's reasoning path, or there would be two Cores.
        """
        self.specialist.answer(question(Cognition.JUDGE))
        request = self.judge.requests[0]
        self.assertEqual(len(request.messages), 2)
        self.assertIs(request.messages[0].role, ModelRole.SYSTEM)
        self.assertIs(request.messages[1].role, ModelRole.USER)
        self.assertEqual(request.messages[0].content, "Answer from the material.")

    def test_a_tier_cannot_dispatch_a_capability_or_continue(self) -> None:
        """One call in, one structured answer out, and nothing else."""
        answer = self.specialist.answer(question(Cognition.JUDGE))
        self.assertEqual(answer, {"finding": "judge-model"})
        self.assertEqual(self.judge.calls, 1)
        request = self.judge.requests[0]
        self.assertFalse(hasattr(request, "tools"))
        self.assertFalse(hasattr(request, "capabilities"))

    def test_an_invalid_tier_is_refused(self) -> None:
        with self.assertRaises(ValueError):
            SpecialistQuestion(
                question_id="q",
                instruction="i",
                material="m",
                answer_schema=SCHEMA,
                cognition="judge",  # a string is not a tier
            )

    def test_tiers_are_difficulty_names_not_vendor_names(self) -> None:
        """The architecture must not name a vendor or a model family."""
        values = {tier.value for tier in Cognition}
        self.assertEqual(values, {"survey", "compare", "judge"})
        for value in values:
            for vendor in ("luna", "terra", "sol", "opus", "grok", "gpt", "claude"):
                self.assertNotIn(vendor, value)


class SingleResearchPathTest(unittest.TestCase):
    """Law 0: three tiers are three configurations, not three paths."""

    def test_one_specialist_class_serves_every_tier(self) -> None:
        source = (
            REPOSITORY_ROOT / "src" / "alx" / "specialists" / "runner.py"
        ).read_text()
        self.assertEqual(source.count("class ModelSpecialist"), 1)
        self.assertEqual(source.count("def answer"), 1)

    def test_no_tier_specific_specialist_class_exists(self) -> None:
        root = REPOSITORY_ROOT / "src" / "alx"
        for name in ("SurveySpecialist", "CompareSpecialist", "JudgeSpecialist"):
            hits = [
                path
                for path in root.rglob("*.py")
                if name in path.read_text()
            ]
            self.assertEqual(hits, [], f"{name} would be a second research path")

    def test_research_goes_through_the_one_bounded_question_contract(self) -> None:
        source = (
            REPOSITORY_ROOT / "src" / "alx" / "specialists" / "research.py"
        ).read_text()
        self.assertIn("SpecialistQuestion", source)
        # The researcher delegates to the one specialist rather than calling a
        # model itself; a direct provider call here would be a second path.
        self.assertNotIn(".complete(", source)


if __name__ == "__main__":
    unittest.main()
