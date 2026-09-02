"""Research prose is handed to Core but not retained as goal-state storage."""

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
from alx.tools.research import ASK_RESEARCH_QUESTION, build_research_executors  # noqa: E402


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


if __name__ == "__main__":
    unittest.main()
