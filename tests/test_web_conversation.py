"""Web reading exercised through AL/X, not by calling the capability directly.

LAW_ENFORCEMENT requires every conversational capability to survive paraphrase,
follow-up, restart and replanning. What is proved here is that reading a page
is ordinary work inside the agent loop: differently worded goals reach the same
capability without new code, a refusal is evidence she can act on rather than a
dead end, and a retrieval stays citable after the process restarts.
"""

from __future__ import annotations

import sys
import tempfile
import unittest
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from alx.contracts import (
    AgentDecision,
    CapabilityAttempt,
    CapabilityAttemptDisposition,
    CapabilityCall,
    CapabilityResultState,
    ConversationOrigin,
    ConversationSnapshot,
    ConversationTurn,
    Evidence,
    GoalMutationKind,
    GoalProposal,
    SuccessCriterion,
    WebPage,
    WebRetrievalError,
)
from alx.core import CoreAgent, CoreState
from alx.goals import SQLiteGoalStore
from alx.tools import ASK_WEB_PAGE, WEB_DEFINITION
from alx.tools.web import build_web_executors


NOW = datetime(2026, 9, 4, 10, 30, tzinfo=UTC)
RETENTION = NOW + timedelta(days=30)
URL = "https://example.com/reservoir-report"


class StubFetcher:
    """Answers with a page, or refuses, exactly as the real provider would."""

    def __init__(self, *outcomes) -> None:
        self.outcomes = list(outcomes) or [self.page()]
        self.calls: list[str] = []

    @staticmethod
    def page(content: str = "The reservoir stood at sixty percent.") -> WebPage:
        return WebPage(
            requested_url=URL,
            final_url=URL,
            source_domain="example.com",
            retrieved_at=NOW,
            http_status=200,
            content=content,
            title="Reservoir Report",
        )

    def fetch(self, url: str, max_characters: int) -> WebPage:
        self.calls.append(url)
        outcome = self.outcomes.pop(0) if len(self.outcomes) > 1 else self.outcomes[0]
        if isinstance(outcome, Exception):
            raise outcome
        return outcome


class Queued:
    """A fake Core model, following the harness the loop tests already use."""

    def __init__(self, *decisions, selects: str | None = None) -> None:
        self.decisions = list(decisions)
        self.contexts = []
        self._selects = selects

    def decide(self, context):
        self.contexts.append(context)
        item = self.decisions.pop(0)
        if isinstance(item, Exception):
            raise item
        if self._selects is not None and item.goal_id is None:
            item = replace(item, goal_id=self._selects)
        return item


def conversation(wording: str) -> ConversationSnapshot:
    return ConversationSnapshot(
        "conversation-1",
        (ConversationTurn("conversation-1", "turn-1", ConversationOrigin.TYPED,
                          wording, NOW, "friedl"),),
        1,
        RETENTION,
    )


def a_goal() -> GoalProposal:
    return GoalProposal(
        GoalMutationKind.CREATE,
        "Find out what the report says",
        (SuccessCriterion("criterion-1", "the page is read"),),
    )


class WebConversationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.directory = tempfile.TemporaryDirectory()
        self.path = Path(self.directory.name) / "goals.sqlite3"
        self.store = SQLiteGoalStore(self.path)

    def tearDown(self) -> None:
        self.store.close()
        self.directory.cleanup()

    def agent(self, reasoner, fetcher, identifiers=("goal-1",)):
        values = iter(identifiers)
        executors = build_web_executors(fetcher, lambda: current_call[0])
        current_call = ["call-web-1"]

        def dispatch(call, state):
            current_call[0] = call.call_id
            return CapabilityAttempt(
                call,
                CapabilityAttemptDisposition.EXECUTED,
                True,
                executors[call.capability_id](call.arguments),
            )

        return CoreAgent(
            self.store, reasoner, dispatch, (WEB_DEFINITION,),
            clock=lambda: NOW, identifier_factory=lambda: next(values),
        )

    def read_call(self, call_id: str = "call-web-1") -> CapabilityCall:
        return CapabilityCall(
            call_id, ASK_WEB_PAGE, {"page_id": "p1", "url": URL}
        )

    def test_differently_worded_goals_reach_the_same_capability(self) -> None:
        """No production change is needed for new wording."""
        wordings = (
            "Have a look at that reservoir report and tell me what it says",
            "what's on this page? https://example.com/reservoir-report",
            "Could you check the current reservoir level online for me",
            "pull up the report",
        )
        for index, wording in enumerate(wordings):
            with self.subTest(wording=wording):
                fetcher = StubFetcher()
                reasoner = Queued(
                    AgentDecision(call=self.read_call(), goal_proposal=a_goal()),
                    AgentDecision(response="Sixty percent."),
                )
                outcome = self.agent(
                    reasoner, fetcher, identifiers=(f"goal-{index}",)
                ).process(conversation(wording), RETENTION, 3)
                self.assertEqual(outcome.state, CoreState.RESPONDED)
                self.assertEqual(fetcher.calls, [URL])

    def test_the_page_returns_to_the_core_as_evidence(self) -> None:
        fetcher = StubFetcher()
        reasoner = Queued(
            AgentDecision(call=self.read_call(), goal_proposal=a_goal()),
            AgentDecision(response="The report says sixty percent."),
        )
        outcome = self.agent(reasoner, fetcher).process(
            conversation("read the report"), RETENTION, 3
        )
        self.assertEqual(outcome.state, CoreState.RESPONDED)
        attempt = outcome.snapshot.state.attempts[-1]
        self.assertIs(attempt.result.state, CapabilityResultState.SUCCEEDED)
        # The retrieved text reached the reasoning turn that followed it.
        seen = str(reasoner.contexts[-1].active_goal.attempts[-1].result.values)
        self.assertIn("sixty percent", seen)

    def test_the_core_sees_the_retrieval_before_it_answers(self) -> None:
        """She reasons over the page rather than answering ahead of it."""
        fetcher = StubFetcher()
        reasoner = Queued(
            AgentDecision(call=self.read_call(), goal_proposal=a_goal()),
            AgentDecision(response="Sixty percent."),
        )
        self.agent(reasoner, fetcher).process(
            conversation("read the report"), RETENTION, 3
        )
        self.assertEqual(len(reasoner.contexts), 2)
        self.assertIsNone(reasoner.contexts[0].active_goal)
        self.assertEqual(len(reasoner.contexts[1].active_goal.attempts), 1)

    def test_a_refusal_is_evidence_she_can_replan_from(self) -> None:
        """A blocked page ends the attempt, not the thinking."""
        fetcher = StubFetcher(WebRetrievalError("retrieval_blocked"))
        reasoner = Queued(
            AgentDecision(call=self.read_call(), goal_proposal=a_goal()),
            AgentDecision(response="That page would not let me read it."),
        )
        outcome = self.agent(reasoner, fetcher).process(
            conversation("read the report"), RETENTION, 3
        )
        attempt = reasoner.contexts[-1].active_goal.attempts[-1]
        self.assertIs(attempt.result.state, CapabilityResultState.FAILED)
        self.assertEqual(attempt.result.failure["code"], "retrieval_blocked")
        self.assertEqual(outcome.state, CoreState.RESPONDED)

    def test_she_may_try_another_source_after_a_refusal(self) -> None:
        """Replanning needs no fallback handler; it is another decision."""
        other = "https://example.org/mirror"
        fetcher = StubFetcher(
            WebRetrievalError("retrieval_blocked"), StubFetcher.page()
        )
        reasoner = Queued(
            AgentDecision(call=self.read_call(), goal_proposal=a_goal()),
            AgentDecision(
                call=CapabilityCall(
                    "call-web-2", ASK_WEB_PAGE, {"page_id": "p2", "url": other}
                )
            ),
            AgentDecision(response="The mirror had it: sixty percent."),
        )
        outcome = self.agent(reasoner, fetcher).process(
            conversation("read the report"), RETENTION, 4
        )
        self.assertEqual(outcome.state, CoreState.RESPONDED)
        self.assertEqual(fetcher.calls, [URL, other])

    def test_a_refusal_does_not_resolve_itself_into_a_conclusion(self) -> None:
        """Code reports what happened; she decides what it means."""
        fetcher = StubFetcher(WebRetrievalError("unsupported_dynamic_page"))
        reasoner = Queued(
            AgentDecision(call=self.read_call(), goal_proposal=a_goal()),
            AgentDecision(response="That page needs a browser to read."),
        )
        outcome = self.agent(reasoner, fetcher).process(
            conversation("read it"), RETENTION, 3
        )
        result = outcome.snapshot.state.attempts[-1].result
        self.assertEqual(set(result.failure), {"code"})
        self.assertEqual(result.failure["code"], "unsupported_dynamic_page")

    def test_a_retrieval_survives_a_restart_and_stays_citable(self) -> None:
        fetcher = StubFetcher()
        reasoner = Queued(
            AgentDecision(call=self.read_call(), goal_proposal=a_goal()),
            AgentDecision(response="Sixty percent."),
        )
        self.agent(reasoner, fetcher).process(
            conversation("read the report"), RETENTION, 3
        )
        self.store.close()

        # A new process, reading the same durable store.
        reopened = SQLiteGoalStore(self.path)
        self.addCleanup(reopened.close)
        attempt = reopened.load("goal-1").state.attempts[-1]
        self.assertEqual(attempt.call.capability_id, ASK_WEB_PAGE)
        self.assertIs(attempt.result.state, CapabilityResultState.SUCCEEDED)
        # The anchor a later notebook entry or evidence record would cite.
        self.assertEqual(attempt.call.call_id, "call-web-1")
        # Metadata survived; the page body did not.
        self.assertEqual(attempt.result.durable_values["final_url"], URL)
        self.assertNotIn("content", attempt.result.durable_values)

    def test_the_url_asked_for_survives_a_restart(self) -> None:
        fetcher = StubFetcher()
        reasoner = Queued(
            AgentDecision(call=self.read_call(), goal_proposal=a_goal()),
            AgentDecision(response="Sixty percent."),
        )
        self.agent(reasoner, fetcher).process(
            conversation("read the report"), RETENTION, 3
        )
        self.store.close()
        reopened = SQLiteGoalStore(self.path)
        self.addCleanup(reopened.close)
        attempt = reopened.load("goal-1").state.attempts[-1]
        self.assertEqual(attempt.call.durable_arguments["url"], URL)

    def test_she_may_cite_the_retrieval_as_evidence(self) -> None:
        """`attempt:<call_id>` is accepted by the grounding rule unchanged."""
        fetcher = StubFetcher()
        reasoner = Queued(
            AgentDecision(call=self.read_call(), goal_proposal=a_goal()),
            AgentDecision(
                response="Sixty percent, according to the report.",
                goal_proposal=GoalProposal(
                    GoalMutationKind.UPDATE,
                    new_evidence=(
                        Evidence(
                            "evidence-1",
                            "web_page",
                            {"url": URL, "retrieved_at": NOW.isoformat(),
                             "title": "Reservoir Report"},
                            supports=("criterion-1",),
                            source_references=("attempt:call-web-1",),
                        ),
                    ),
                ),
            ),
        )
        outcome = self.agent(reasoner, fetcher).process(
            conversation("read the report"), RETENTION, 3
        )
        self.assertEqual(outcome.state, CoreState.RESPONDED)
        recorded = self.store.load("goal-1").state.evidence
        self.assertEqual(recorded[-1].source_references, ("attempt:call-web-1",))
        self.assertEqual(recorded[-1].attributes["url"], URL)

    def test_nothing_is_recorded_unless_she_records_it(self) -> None:
        """Retrieval alone creates no evidence and no notebook entry."""
        fetcher = StubFetcher()
        reasoner = Queued(
            AgentDecision(call=self.read_call(), goal_proposal=a_goal()),
            AgentDecision(response="Sixty percent."),
        )
        outcome = self.agent(reasoner, fetcher).process(
            conversation("read the report"), RETENTION, 3
        )
        self.assertEqual(outcome.snapshot.state.evidence, ())

    def test_a_follow_up_reuses_the_goal_without_restating_it(self) -> None:
        fetcher = StubFetcher()
        first = Queued(
            AgentDecision(call=self.read_call(), goal_proposal=a_goal()),
            AgentDecision(response="Sixty percent."),
        )
        self.agent(first, fetcher).process(
            conversation("read the report"), RETENTION, 3
        )

        follow_up = Queued(
            AgentDecision(response="It was measured on Tuesday."),
            selects="goal-1",
        )
        self.agent(follow_up, fetcher, identifiers=()).process(
            conversation("and when was that measured?"), RETENTION, 2
        )
        self.assertEqual(len(self.store.list_goals()), 1)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
