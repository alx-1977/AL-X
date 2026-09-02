"""Research prose reaches Core unretained, and every declared failure is reachable."""

from __future__ import annotations

import unittest
import tempfile
from datetime import UTC, datetime, timedelta
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from alx.contracts import (  # noqa: E402
    CapabilityAttempt, CapabilityAttemptDisposition, CapabilityCall,
    CapabilityResult, CapabilityResultState, GoalState, Objective,
    SuccessCriterion,
)
from alx.goals import SQLiteGoalStore  # noqa: E402
from alx.contracts import (  # noqa: E402
    ResearchModelUnpriced, SpecialistError,
)
from alx.observability.research_budget import ResearchBudgetExceeded  # noqa: E402
from alx.specialists.research import (  # noqa: E402
    ResearchCeilingFailed, ResearchInputUnbounded, ResearchModelUnbounded,
)
from alx.tools.research import (  # noqa: E402
    ASK_RESEARCH_QUESTION, DEFINITION, build_research_executors,
)


class FakeResearcher:
    def answer(self, question, task_id=""):
        return {"finding": "A full paid research finding."}


class ResearchResultPersistenceTests(unittest.TestCase):
    def test_finding_reaches_core_but_goal_receipt_contains_no_prose(self) -> None:
        execute = build_research_executors(
            FakeResearcher(), lambda: "research-call"
        )[ASK_RESEARCH_QUESTION]
        result = execute({
            "question_id": "question-1",
            "instruction": "Investigate.",
            "material": "Material.",
            "cognition": "survey",
        })
        self.assertIs(result.state, CapabilityResultState.SUCCEEDED)
        self.assertEqual(result.values["finding"], "A full paid research finding.")
        self.assertEqual(dict(result.durable_values), {})

    def test_goal_restart_retains_only_notebook_references(self) -> None:
        finding = "A full paid research finding."
        research_call = CapabilityCall(
            "research-call", ASK_RESEARCH_QUESTION, {"question_id": "q-1"}
        )
        research_result = CapabilityResult(
            "research-call", ASK_RESEARCH_QUESTION,
            CapabilityResultState.SUCCEEDED, {"finding": finding},
            durable_values={},
        )
        notebook_call = CapabilityCall(
            "notebook-call", "record_research_entry",
            {"entry_id": "entry-1", "thread_id": "thread-1", "content": finding},
            durable_arguments={"entry_id": "entry-1", "thread_id": "thread-1"},
        )
        notebook_result = CapabilityResult(
            "notebook-call", "record_research_entry",
            CapabilityResultState.SUCCEEDED,
            {"entry_id": "entry-1", "thread_id": "thread-1", "content": finding},
            durable_values={
                "entry_id": "entry-1", "thread_id": "thread-1", "revision": 1,
            },
        )
        state = GoalState(
            "goal-1", Objective("turn:turn-1", "Research"),
            (SuccessCriterion("criterion-1", "Finding recorded"),),
            context={"notebook_thread_id": "thread-1", "notebook_entry_id": "entry-1"},
            attempts=(
                CapabilityAttempt(
                    research_call, CapabilityAttemptDisposition.EXECUTED,
                    True, research_result,
                ),
                CapabilityAttempt(
                    notebook_call, CapabilityAttemptDisposition.EXECUTED,
                    True, notebook_result,
                ),
            ),
        )
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "goals.sqlite3"
            store = SQLiteGoalStore(path)
            store.create(
                state, "conversation-1",
                datetime(2026, 9, 2, tzinfo=UTC) + timedelta(days=30),
            )
            store.close()
            reopened = SQLiteGoalStore(path)
            try:
                recovered = reopened.load("goal-1").state
            finally:
                reopened.close()
        self.assertNotIn(finding, repr(recovered))
        self.assertEqual(
            recovered.context,
            {"notebook_thread_id": "thread-1", "notebook_entry_id": "entry-1"},
        )
        self.assertEqual(
            dict(recovered.attempts[-1].call.arguments),
            {"entry_id": "entry-1", "thread_id": "thread-1"},
        )


class FailingResearcher:
    """Raises one prepared failure, exactly as the real specialist would."""

    def __init__(self, error: Exception) -> None:
        self._error = error

    def answer(self, question, task_id=""):
        raise self._error


ARGUMENTS = {
    "question_id": "question-1",
    "instruction": "Investigate.",
    "material": "Material.",
    "cognition": "survey",
}


class ResearchFailureTranslationTests(unittest.TestCase):
    """Every failure code the capability declares must be reachable.

    The executor maps a specialist exception to a declared code by class name,
    because ResearchBudgetExceeded lives in observability, which `tools` may
    not import. That coupling is invisible to the type checker: renaming a
    class would silently collapse a distinct failure into provider_failed and
    AL/X would lose the difference between "the budget is spent" and "the
    provider broke". These tests raise the real classes, so a rename fails here
    instead of degrading her evidence in production.
    """

    def _failure_for(self, error: Exception) -> str:
        execute = build_research_executors(
            FailingResearcher(error), lambda: "research-call"
        )[ASK_RESEARCH_QUESTION]
        result = execute(dict(ARGUMENTS))
        self.assertIs(result.state, CapabilityResultState.FAILED)
        self.assertIsNotNone(result.failure)
        return result.failure["code"]

    def test_an_exhausted_budget_is_distinguishable_from_a_provider_fault(self) -> None:
        self.assertEqual(
            self._failure_for(ResearchBudgetExceeded(0.01, 0.10)),
            "research_budget_exhausted",
        )

    def test_an_unpriced_model_reports_the_model_as_unavailable(self) -> None:
        self.assertEqual(
            self._failure_for(ResearchModelUnpriced("testvendor", "survey-model")),
            "research_model_unavailable",
        )

    def test_a_model_above_the_request_ceiling_is_unavailable(self) -> None:
        self.assertEqual(
            self._failure_for(
                ResearchModelUnbounded("testvendor", "survey-model", 1.0, 0.1)
            ),
            "research_model_unavailable",
        )

    def test_an_unbounded_instruction_is_an_argument_fault(self) -> None:
        self.assertEqual(
            self._failure_for(ResearchInputUnbounded()), "arguments_unusable"
        )

    def test_a_breached_ceiling_stops_research_with_its_own_code(self) -> None:
        self.assertEqual(
            self._failure_for(ResearchCeilingFailed(0.02)), "research_ceiling_failed"
        )

    def test_an_unconfigured_tier_is_not_reported_as_a_provider_fault(self) -> None:
        self.assertEqual(
            self._failure_for(SpecialistError("cognition_tier_unconfigured: judge")),
            "cognition_tier_unconfigured",
        )

    def test_another_specialist_failure_stays_a_provider_fault(self) -> None:
        self.assertEqual(
            self._failure_for(SpecialistError("answer_not_structured")),
            "provider_failed",
        )

    def test_an_unrecognised_error_degrades_to_a_provider_fault(self) -> None:
        self.assertEqual(self._failure_for(RuntimeError("boom")), "provider_failed")

    def test_unusable_arguments_never_reach_the_paid_specialist(self) -> None:
        """A malformed call must not spend money before it is rejected."""

        class Unreachable:
            def answer(self, question, task_id=""):
                raise AssertionError("a malformed call must not reach the specialist")

        execute = build_research_executors(
            Unreachable(), lambda: "research-call"
        )[ASK_RESEARCH_QUESTION]
        for arguments in (
            {"instruction": "Investigate.", "material": "Material."},
            dict(ARGUMENTS, cognition="omniscient"),
        ):
            with self.subTest(arguments=sorted(arguments)):
                result = execute(arguments)
                self.assertIs(result.state, CapabilityResultState.FAILED)
                self.assertEqual(result.failure["code"], "arguments_unusable")

    def test_a_blank_finding_is_a_provider_fault_rather_than_a_success(self) -> None:
        class Blank:
            def answer(self, question, task_id=""):
                return {"finding": "   "}

        execute = build_research_executors(
            Blank(), lambda: "research-call"
        )[ASK_RESEARCH_QUESTION]
        result = execute(dict(ARGUMENTS))
        self.assertIs(result.state, CapabilityResultState.FAILED)
        self.assertEqual(result.failure["code"], "provider_failed")

    def test_every_declared_failure_code_is_reachable(self) -> None:
        """The declared contract and the reachable behaviour must not drift."""
        reachable = {
            self._failure_for(error)
            for error in (
                ResearchBudgetExceeded(0.01, 0.10),
                ResearchModelUnpriced("testvendor", "survey-model"),
                ResearchModelUnbounded("testvendor", "survey-model", 1.0, 0.1),
                ResearchInputUnbounded(),
                ResearchCeilingFailed(0.02),
                SpecialistError("cognition_tier_unconfigured: judge"),
                RuntimeError("boom"),
            )
        }
        self.assertEqual(reachable, set(DEFINITION.possible_failure_codes))


if __name__ == "__main__":
    unittest.main()
