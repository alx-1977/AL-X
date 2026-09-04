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

    def test_a_correction_changes_which_page_she_reads(self) -> None:
        """Friedl corrects an earlier assumption; no new code path is needed."""
        corrected = "https://example.com/2026-report"
        fetcher = StubFetcher(StubFetcher.page(), StubFetcher.page("Ninety percent."))
        first = Queued(
            AgentDecision(call=self.read_call(), goal_proposal=a_goal()),
            AgentDecision(response="That one says sixty percent."),
        )
        self.agent(first, fetcher).process(
            conversation("read the reservoir report"), RETENTION, 3
        )

        # The correction arrives as ordinary conversation. She reads the page
        # she is now told is the right one and revises what she reports.
        after = Queued(
            AgentDecision(
                call=CapabilityCall(
                    "call-web-2", ASK_WEB_PAGE,
                    {"page_id": "p2", "url": corrected},
                )
            ),
            AgentDecision(response="Corrected: the 2026 report says ninety percent."),
            selects="goal-1",
        )
        outcome = self.agent(after, fetcher, identifiers=()).process(
            conversation("no, I meant the 2026 one"), RETENTION, 3
        )
        self.assertEqual(outcome.state, CoreState.RESPONDED)
        self.assertEqual(fetcher.calls, [URL, corrected])
        # One goal throughout: a correction refines the work, it does not
        # start a second piece of it.
        self.assertEqual(len(self.store.list_goals()), 1)
        capabilities = [
            item.call.capability_id
            for item in self.store.load("goal-1").state.attempts
        ]
        self.assertEqual(capabilities, [ASK_WEB_PAGE, ASK_WEB_PAGE])

    def test_an_interruption_is_followed_by_resumption(self) -> None:
        """Unrelated conversation between two turns loses none of the work."""
        fetcher = StubFetcher()
        first = Queued(
            AgentDecision(call=self.read_call(), goal_proposal=a_goal()),
            AgentDecision(response="Sixty percent."),
        )
        self.agent(first, fetcher).process(
            conversation("read the reservoir report"), RETENTION, 3
        )

        # An interruption about something else entirely, attaching to no goal.
        interruption = Queued(AgentDecision(response="Twenty past four."))
        self.agent(interruption, fetcher, identifiers=()).process(
            conversation("hang on, what's the time?"), RETENTION, 2
        )

        # She picks the work up again: first naming the goal to see its state,
        # then answering from what that state already holds.
        resumed = Queued(
            AgentDecision(goal_id="goal-1"),
            AgentDecision(response="Back to the reservoir: sixty percent."),
            selects="goal-1",
        )
        outcome = self.agent(resumed, fetcher, identifiers=()).process(
            conversation("right, back to the reservoir"), RETENTION, 3
        )
        self.assertEqual(outcome.state, CoreState.RESPONDED)
        self.assertEqual(len(self.store.list_goals()), 1)
        # The retrieval made before the interruption is still hers to cite,
        # and no page was fetched again to recover it.
        attempt = resumed.contexts[-1].active_goal.attempts[-1]
        self.assertEqual(attempt.call.call_id, "call-web-1")
        self.assertIs(attempt.result.state, CapabilityResultState.SUCCEEDED)
        self.assertEqual(fetcher.calls, [URL])

    def test_she_verifies_the_retrieval_before_claiming_completion(self) -> None:
        """A failed read is not a finished goal, however tempting the story."""
        fetcher = StubFetcher(
            WebRetrievalError("retrieval_blocked"), StubFetcher.page()
        )
        reasoner = Queued(
            AgentDecision(call=self.read_call(), goal_proposal=a_goal()),
            # She does not declare completion on the failure. She looks again.
            AgentDecision(
                call=CapabilityCall(
                    "call-web-2", ASK_WEB_PAGE,
                    {"page_id": "p2", "url": "https://example.org/mirror"},
                )
            ),
            AgentDecision(
                response="Sixty percent, and I have read the page myself.",
                goal_proposal=GoalProposal(
                    GoalMutationKind.REQUEST_COMPLETION,
                    new_evidence=(
                        Evidence(
                            "evidence-1",
                            "web_page",
                            {"url": "https://example.org/mirror",
                             "retrieved_at": NOW.isoformat()},
                            supports=("criterion-1",),
                            source_references=("attempt:call-web-2",),
                        ),
                    ),
                ),
                response_requires_goal_commit=True,
            ),
        )
        outcome = self.agent(reasoner, fetcher).process(
            conversation("read the reservoir report"), RETENTION, 4
        )
        self.assertEqual(outcome.state, CoreState.RESPONDED)
        state = self.store.load("goal-1").state
        # Completion cites the successful read, not the blocked one.
        self.assertEqual(
            state.evidence[-1].source_references, ("attempt:call-web-2",)
        )
        failed, succeeded = state.attempts
        self.assertIs(failed.result.state, CapabilityResultState.FAILED)
        self.assertIs(succeeded.result.state, CapabilityResultState.SUCCEEDED)


class ScenariosNotApplicableTests(unittest.TestCase):
    """Why two of the nine required scenarios have no test in this slice.

    LAW_ENFORCEMENT requires nine scenarios for a conversational capability.
    Seven are exercised above. The remaining two are recorded here rather than
    silently omitted, because a missing scenario and an inapplicable one look
    identical from a test list.
    """

    def test_no_approval_pause_exists_to_exercise(self) -> None:
        """Reading a public page is not a consequential action.

        D-025 makes the network boundary, the resource bounds and GET-only the
        control, deliberately rather than an approval ceremony: asking Friedl
        to approve each page would make reading something he directs rather
        than something she does while thinking. There is therefore no approval
        pause in this capability to test. Any future capability that writes,
        posts or spends would need its own decision and its own gate.
        """
        from alx.bootstrap.web import build_web_runtime

        runtime = build_web_runtime(True, lambda: "call-1")
        self.addCleanup(runtime.provider.close)
        self.assertFalse(runtime.policies[ASK_WEB_PAGE].approval_required)

    def test_this_slice_registers_only_one_capability(self) -> None:
        """A multi-capability goal needs a second capability to combine with.

        Steps 1-3 deliver `ask_web_page` alone; `ask_web_search` waits for the
        step-3 review. The multi-capability scenario becomes testable, and must
        be tested, when search lands.
        """
        from alx.bootstrap.web import build_web_runtime

        runtime = build_web_runtime(True, lambda: "call-1")
        self.addCleanup(runtime.provider.close)
        self.assertEqual(
            [item.capability_id for item in runtime.definitions], [ASK_WEB_PAGE]
        )


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
