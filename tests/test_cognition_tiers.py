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
from types import SimpleNamespace

REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPOSITORY_ROOT / "src"))

from alx.contracts import (  # noqa: E402
    Cognition,
    SpecialistError,
    ModelCompletion,
    ModelRole,
    ResearchQuestion,
    SpecialistQuestion,
)
from alx.specialists import ResearchSpecialist, ResearchTierModel  # noqa: E402


SCHEMA = {
    "type": "object",
    "properties": {"finding": {"type": "string"}},
    "required": ["finding"],
    "additionalProperties": False,
}


class TierModel:
    supports_bounded_research = True

    def __init__(self, name: str) -> None:
        self.name = name
        self.calls = 0
        self.requests = []

    def complete(self, request):
        self.calls += 1
        self.requests.append(request)
        return ModelCompletion("testvendor", self.name, {"finding": self.name}, {})


class FreePricing:
    def is_priced(self, provider, model): return True
    def worst_case_usd(self, provider, model, max_input, max_output): return 0.01
    def cost_usd(self, provider, model, usage): return 0.0


class RecordingLedger:
    def __init__(self): self.reservations = 0
    def overrun_usd(self, day=None): return 0.0
    def reserve(self, tier, provider, model, kind="research", worst_case_usd=None):
        self.reservations += 1
        return SimpleNamespace(reservation_id=f"r-{self.reservations}", reserved_usd=worst_case_usd)
    def settle(
        self, reservation, actual_usd, usage=None, provider=None, model=None
    ):
        return actual_usd
    def abandon(
        self, reservation, failure_code="provider_failed", usage=None,
        provider=None, model=None,
    ):
        return reservation.reserved_usd
    def remaining_usd(self, day=None): return 1.0


def question(cognition: Cognition, material: str = "material") -> ResearchQuestion:
    return ResearchQuestion(
        SpecialistQuestion(
            question_id="q",
            instruction="Answer from the material.",
            material=material,
            answer_schema=SCHEMA,
        ),
        cognition,
    )


class CognitionTierTest(unittest.TestCase):
    def setUp(self) -> None:
        self.survey = TierModel("survey-model")
        self.compare = TierModel("compare-model")
        self.judge = TierModel("judge-model")
        self.ledger = RecordingLedger()
        self.specialist = ResearchSpecialist(
            {
                Cognition.SURVEY: ResearchTierModel("testvendor", "survey-model", self.survey),
                Cognition.COMPARE: ResearchTierModel("testvendor", "compare-model", self.compare),
                Cognition.JUDGE: ResearchTierModel("testvendor", "judge-model", self.judge),
            },
            self.ledger,
            FreePricing(),
            4_000,
            1_000,
            0.10,
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
        default = ResearchQuestion(
            SpecialistQuestion(
                question_id="q",
                instruction="Answer from the material.",
                material="material",
                answer_schema=SCHEMA,
            )
        )
        self.assertIs(default.cognition, Cognition.SURVEY)

    def test_an_unconfigured_tier_refuses_rather_than_substituting(self) -> None:
        """Silent substitution would run a tier AL/X did not choose.

        Falling back would answer a hard question with a cheap model, or a cheap
        question with an expensive one, and the tier would stop meaning anything.
        A misconfiguration is made visible instead.
        """
        specialist = ResearchSpecialist(
            {Cognition.SURVEY: ResearchTierModel("testvendor", "survey-model", self.survey)},
            RecordingLedger(), FreePricing(), 4_000, 1_000, 0.10,
        )
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
        with self.assertRaises(TypeError):
            ResearchQuestion(
                SpecialistQuestion(
                    question_id="q",
                    instruction="i",
                    material="m",
                    answer_schema=SCHEMA,
                ),
                "judge",  # a string is not a tier
            )

    def test_tiers_are_difficulty_names_not_vendor_names(self) -> None:
        """The architecture must not name a vendor or a model family."""
        values = {tier.value for tier in Cognition}
        self.assertEqual(values, {"survey", "compare", "judge"})
        for value in values:
            for vendor in ("luna", "terra", "sol", "opus", "grok", "gpt", "claude"):
                self.assertNotIn(vendor, value)


class OmittedMaterialTest(unittest.TestCase):
    """Material that does not fit the priced bound is reported, never hidden.

    Truncating to a prefix is a mechanical consequence of the input ceiling,
    but an answer read from part of the material is weaker evidence than an
    answer read from all of it. Law 3 puts that judgement with AL/X, so the
    shortfall travels with the finding instead of being silently resolved.
    """

    def setUp(self) -> None:
        self.model = TierModel("survey-model")
        self.emitted: list[tuple[str, dict]] = []
        # A ceiling that comfortably carries the instruction and short
        # material, so only genuinely long material overflows it.
        self.specialist = ResearchSpecialist(
            {Cognition.SURVEY: ResearchTierModel("testvendor", "survey-model", self.model)},
            RecordingLedger(),
            FreePricing(),
            1_000,
            1_000,
            0.10,
            telemetry_sink=lambda task_id, values: self.emitted.append((task_id, dict(values))),
        )

    def test_a_complete_read_reports_no_omission(self) -> None:
        answer = self.specialist.answer(question(Cognition.SURVEY, "short"))
        self.assertEqual(answer, {"finding": "survey-model"})
        self.assertNotIn(
            "research.material_omitted",
            [values["code"] for _, values in self.emitted],
        )

    def test_an_oversized_read_reports_what_was_left_out(self) -> None:
        material = "x" * 5_000
        answer = self.specialist.answer(question(Cognition.SURVEY, material))
        omitted = answer["material_omitted_characters"]
        self.assertGreater(omitted, 0)
        sent = self.model.requests[-1].messages[-1].content
        self.assertEqual(len(sent) + omitted, len(material))
        self.assertTrue(material.startswith(sent))

    def test_the_omission_is_visible_to_an_operator(self) -> None:
        self.specialist.answer(question(Cognition.SURVEY, "x" * 5_000))
        events = [
            values for _, values in self.emitted
            if values["code"] == "research.material_omitted"
        ]
        self.assertEqual(len(events), 1)
        self.assertGreater(events[0]["omitted_characters"], 0)
        self.assertEqual(events[0]["question_id"], "q")

    def test_the_question_s_own_material_limit_is_counted_too(self) -> None:
        """Two cuts can remove material; the report must cover both.

        `bounded_material` applies the question's material_limit before this
        specialist sees the text. Measuring the shortfall only against that
        already-shortened string would report a complete read of a document
        that had in fact been cut upstream.
        """
        material = "x" * 9_000
        asked = ResearchQuestion(
            SpecialistQuestion(
                question_id="q",
                instruction="Answer from the material.",
                material=material,
                answer_schema=SCHEMA,
                material_limit=6_000,
            ),
            Cognition.SURVEY,
        )
        # A ceiling generous enough that the priced bound removes nothing, so
        # only the question's own limit is in play.
        specialist = ResearchSpecialist(
            {Cognition.SURVEY: ResearchTierModel("testvendor", "survey-model", self.model)},
            RecordingLedger(), FreePricing(), 100_000, 1_000, 0.10,
        )
        answer = specialist.answer(asked)
        sent = self.model.requests[-1].messages[-1].content
        self.assertEqual(len(sent), 6_000)
        self.assertEqual(answer["material_omitted_characters"], 3_000)
        self.assertEqual(len(sent) + answer["material_omitted_characters"], len(material))

    def test_both_cuts_are_reported_together(self) -> None:
        """The upstream limit and the priced bound add up to one shortfall."""
        material = "x" * 9_000
        asked = ResearchQuestion(
            SpecialistQuestion(
                question_id="q",
                instruction="Answer from the material.",
                material=material,
                answer_schema=SCHEMA,
                material_limit=6_000,
            ),
            Cognition.SURVEY,
        )
        answer = self.specialist.answer(asked)
        sent = self.model.requests[-1].messages[-1].content
        self.assertLess(len(sent), 6_000)
        self.assertEqual(len(sent) + answer["material_omitted_characters"], len(material))

    def test_the_specialist_draws_no_conclusion_from_the_omission(self) -> None:
        """It reports the shortfall and still answers; it does not refuse."""
        answer = self.specialist.answer(question(Cognition.SURVEY, "x" * 5_000))
        self.assertEqual(answer["finding"], "survey-model")
        self.assertEqual(self.model.calls, 1)


class SingleResearchPathTest(unittest.TestCase):
    """Law 0: three tiers are three configurations, not three paths."""

    def test_one_research_class_serves_every_tier(self) -> None:
        source = (
            REPOSITORY_ROOT / "src" / "alx" / "specialists" / "research.py"
        ).read_text()
        self.assertEqual(source.count("class ResearchSpecialist"), 1)
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

    def test_generic_specialist_has_no_tier_dispatch(self) -> None:
        source = (
            REPOSITORY_ROOT / "src" / "alx" / "specialists" / "runner.py"
        ).read_text()
        self.assertNotIn("tiers", source)
        self.assertNotIn("Cognition", source)

    def test_research_owns_the_only_tier_dispatch(self) -> None:
        source = (
            REPOSITORY_ROOT / "src" / "alx" / "specialists" / "research.py"
        ).read_text()
        self.assertEqual(source.count(".transport.complete(request)"), 1)


if __name__ == "__main__":
    unittest.main()
