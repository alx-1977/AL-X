"""The notebook is hers, and research Friedl asked for does not fill it.

The standing instruction used to open "Research is your notebook work", and
told her to persist every research finding there. She obeyed it: the three
threads that existed when this was written all say, in their own `interest`
field, that Friedl had asked. The notebook had become a research findings
store rather than her own space.

What is proved here is the boundary after the repair. User-directed research
belongs to the goal and is not notebook material merely because research
happened; the notebook holds what she herself decides to keep. Nothing infers
her interest, nothing schedules an enquiry, and recording nothing is ordinary.

None of this can prove she *will* be curious — that is hers, and any test
asserting she must be would be the rule this repair exists to remove. What is
testable is that the machinery neither compels a write nor hides her enquiries
from her.
"""

from __future__ import annotations

import json
import sys
import tempfile
import unittest
from datetime import UTC, datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from alx.contracts import (
    AgentDecision,
    CapabilityAttempt,
    CapabilityAttemptDisposition,
    CapabilityCall,
    CapabilityResult,
    CapabilityResultState,
    CognitionOrigin,
    ConversationOrigin,
    ConversationSnapshot,
    ConversationTurn,
    EntryKind,
    EntryProposal,
    GoalMutationKind,
    GoalProposal,
    SuccessCriterion,
    ThreadProposal,
    ThreadStatus,
    WebPage,
)
from alx.contracts.core import ReasoningContext
from alx.contracts.notebook import OPEN_NOTEBOOK_THREAD_LIMIT
from alx.core import CoreAgent, CoreState
from alx.core.model_reasoner import _context_payload
from alx.goals import SQLiteGoalStore
from alx.research.store import SQLiteResearchStore
from alx.tools import ASK_WEB_PAGE, ASK_WEB_SEARCH, WEB_DEFINITION, WEB_SEARCH_DEFINITION
from alx.tools.notebook import DEFINITIONS as NOTEBOOK_DEFINITIONS


NOW = datetime(2026, 9, 5, 9, 0, tzinfo=UTC)
RETENTION = NOW + timedelta(days=30)
SOURCE = Path(__file__).resolve().parents[1] / "src" / "alx"


class Queued:
    """A fake Core model, following the harness the loop tests already use."""

    def __init__(self, *decisions, selects: str | None = None) -> None:
        self.decisions = list(decisions)
        self.contexts = []
        self._selects = selects

    def decide(self, context):
        from dataclasses import replace

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


class NotebookTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        self.notebook_path = self.root / "research-notebook.sqlite3"
        self.notebook = SQLiteResearchStore(self.notebook_path)
        self.addCleanup(self.notebook.close)
        self.goals = SQLiteGoalStore(self.root / "goals.sqlite3")
        self.addCleanup(self.goals.close)
        self.recorded: list[str] = []

    def thread(self, thread_id: str, question: str, interest: str,
               status: ThreadStatus = ThreadStatus.OPEN, entries: int = 0) -> None:
        self.notebook.open_thread(
            ThreadProposal(thread_id, question, interest, NOW), RETENTION
        )
        if status is not ThreadStatus.OPEN:
            self.notebook.set_status(thread_id, status)
        for index in range(entries):
            self.notebook.record_entry(
                EntryProposal(f"{thread_id}-e{index}", thread_id,
                              EntryKind.CLAIM, f"content {index}", NOW)
            )

    def agent(self, reasoner, capabilities=None, identifiers=("goal-1",)):
        values = iter(identifiers)
        current = ["call-1"]

        def dispatch(call, state):
            current[0] = call.call_id
            self.recorded.append(call.capability_id)
            return CapabilityAttempt(
                call, CapabilityAttemptDisposition.EXECUTED, True,
                CapabilityResult(call.call_id, call.capability_id,
                                 CapabilityResultState.SUCCEEDED, {"ok": True}),
            )

        return CoreAgent(
            self.goals, reasoner, dispatch,
            capabilities or (WEB_DEFINITION, WEB_SEARCH_DEFINITION,
                             *NOTEBOOK_DEFINITIONS),
            clock=lambda: NOW,
            identifier_factory=lambda: next(values),
            open_notebook_threads=lambda: self.notebook.open_threads(
                OPEN_NOTEBOOK_THREAD_LIMIT
            ),
        )


class UserDirectedResearchTests(NotebookTestCase):
    """Friedl asks; she researches and answers. The notebook stays hers."""

    def search_call(self, call_id="call-s1"):
        return CapabilityCall(call_id, ASK_WEB_SEARCH,
                              {"search_id": "s1", "subject": "device os release"})

    def page_call(self, call_id="call-p1"):
        return CapabilityCall(call_id, ASK_WEB_PAGE,
                              {"page_id": "p1", "url": "https://example.com/a"})

    def test_she_can_search_and_read_to_answer_him(self) -> None:
        reasoner = Queued(
            AgentDecision(call=self.search_call(), goal_proposal=GoalProposal(
                GoalMutationKind.CREATE, "Answer the version question",
                (SuccessCriterion("criterion-1", "answered"),))),
            AgentDecision(call=self.page_call()),
            AgentDecision(response="It is 6.5.0."),
        )
        outcome = self.agent(reasoner).process(
            conversation("what is the latest Device OS release?"), RETENTION, 4
        )
        self.assertEqual(outcome.state, CoreState.RESPONDED)
        self.assertEqual(self.recorded, [ASK_WEB_SEARCH, ASK_WEB_PAGE])

    def test_answering_writes_nothing_to_the_notebook(self) -> None:
        """The acceptance criterion: research is not notebook material."""
        reasoner = Queued(
            AgentDecision(call=self.search_call(), goal_proposal=GoalProposal(
                GoalMutationKind.CREATE, "Answer the version question",
                (SuccessCriterion("criterion-1", "answered"),))),
            AgentDecision(response="It is 6.5.0."),
        )
        self.agent(reasoner).process(
            conversation("what is the latest Device OS release?"), RETENTION, 3
        )
        self.assertEqual(self.notebook.open_threads(), ())
        for capability in self.recorded:
            self.assertNotIn("research", capability)

    def test_nothing_compels_a_notebook_write_after_research(self) -> None:
        """No instruction, no code path, requires one. It is her call."""
        instructions = (SOURCE / "core" / "model_reasoner.py").read_text()
        self.assertNotIn("Research is your notebook work", instructions)
        self.assertIn(
            "It does not become notebook work merely because research happened",
            instructions,
        )

    def test_the_goal_keeps_its_own_research_progress(self) -> None:
        """It belongs to the goal, so the goal is where it is recorded."""
        instructions = (SOURCE / "core" / "model_reasoner.py").read_text()
        self.assertIn(
            "belongs primarily to the goal you are working on", instructions
        )
        # The old instruction forbade goal progress; that prohibition is gone.
        self.assertNotIn("Do not copy the finding into goal", instructions)

    def test_she_may_still_choose_to_keep_something_from_his_work(self) -> None:
        """Origin does not permanently bar an interest of her own."""
        instructions = (SOURCE / "core" / "model_reasoner.py").read_text()
        self.assertIn("Something Friedl asked about may go there too",
                      instructions)
        self.assertIn("if you yourself want to keep", instructions)


class SelfDirectedInterestTests(NotebookTestCase):
    """Her own enquiry: no goal, no request, entirely her judgement."""

    def test_open_enquiries_are_visible_without_being_asked_for(self) -> None:
        self.thread("t-curious", "Why do agents forget?", "It bothers me.")
        reasoner = Queued(AgentDecision(finish_silently=True))
        self.agent(reasoner).process(conversation("anything?"), RETENTION, 1)
        threads = reasoner.contexts[0].open_notebook_threads
        self.assertEqual(len(threads), 1)
        self.assertEqual(threads[0]["thread_id"], "t-curious")
        self.assertEqual(threads[0]["question"], "Why do agents forget?")
        self.assertEqual(threads[0]["interest"], "It bothers me.")

    def test_an_enquiry_needs_no_request_from_friedl(self) -> None:
        """Nobody asked. She names the work herself and opens her thread.

        Every effectful capability requires an active goal — a pre-existing
        invariant, because an attempt has to hang on something durable. That
        is a goal she can create for herself in the same decision, so it does
        not require a request from Friedl; it requires her to say what she is
        doing.
        """
        reasoner = Queued(
            AgentDecision(
                call=CapabilityCall(
                    "call-n1", "open_research_thread",
                    {"thread_id": "t-mine", "question": "How does X work?",
                     "interest": "I want to understand it."}),
                goal_proposal=GoalProposal(
                    GoalMutationKind.CREATE, "Understand X for myself",
                    (SuccessCriterion("criterion-1", "I understand it"),)),
            ),
            AgentDecision(finish_silently=True),
        )
        self.agent(reasoner).process(
            conversation("(an occasion nobody asked for)"), RETENTION, 3
        )
        self.assertEqual(self.recorded, ["open_research_thread"])

    def test_a_goalless_turn_cannot_write_and_says_so(self) -> None:
        """The refusal is mechanical and reported, never silent.

        Worth pinning: it is the one thing standing between an occasion and
        her notebook, and she is told the reason so she can create the goal
        and proceed rather than being left guessing.
        """
        reasoner = Queued(
            AgentDecision(call=CapabilityCall(
                "call-n1", "open_research_thread",
                {"thread_id": "t-mine", "question": "How does X work?",
                 "interest": "I want to understand it."})),
            AgentDecision(finish_silently=True),
        )
        self.agent(reasoner).process(
            conversation("(an occasion)"), RETENTION, 3
        )
        self.assertEqual(self.recorded, [])
        refused = reasoner.contexts[-1].refused_calls
        self.assertEqual(refused[0]["reason"], "active_goal_required")

    def test_she_may_search_and_read_while_pursuing_her_own_enquiry(self) -> None:
        self.thread("t-mine", "How does X work?", "I want to understand it.")
        reasoner = Queued(
            AgentDecision(
                call=CapabilityCall(
                    "call-s1", ASK_WEB_SEARCH, {"search_id": "s", "subject": "x"}),
                goal_proposal=GoalProposal(
                    GoalMutationKind.CREATE, "Understand X for myself",
                    (SuccessCriterion("criterion-1", "I understand it"),)),
            ),
            AgentDecision(call=CapabilityCall(
                "call-p1", ASK_WEB_PAGE,
                {"page_id": "p", "url": "https://example.com/x"})),
            AgentDecision(call=CapabilityCall(
                "call-e1", "record_research_entry",
                {"entry_id": "e1", "thread_id": "t-mine", "kind": "claim",
                 "content": "What I now think.",
                 "source_references": ["attempt:call-p1"]})),
            AgentDecision(finish_silently=True),
        )
        self.agent(reasoner).process(
            conversation("(her own occasion)"), RETENTION, 5
        )
        self.assertEqual(
            self.recorded,
            [ASK_WEB_SEARCH, ASK_WEB_PAGE, "record_research_entry"],
        )

    def test_she_may_ask_for_a_later_occasion_herself(self) -> None:
        """Continuity of interest; opportunity is still hers to request."""
        reasoner = Queued(
            AgentDecision(call=CapabilityCall(
                "call-f1", "request_future_cognition",
                {"request_id": "r1", "not_before": "2026-09-06T06:00:00+00:00",
                 "note": "come back to this"})),
            AgentDecision(finish_silently=True),
        )
        # Deliberately no goal: asking for her own later occasion is
        # side-effect free, so it needs none.
        from alx.tools.continuity import DEFINITIONS as CONTINUITY

        self.agent(reasoner, capabilities=(*NOTEBOOK_DEFINITIONS, *CONTINUITY)
                   ).process(conversation("(her own occasion)"), RETENTION, 3)
        self.assertEqual(self.recorded, ["request_future_cognition"])

    def test_an_open_thread_schedules_nothing(self) -> None:
        """Continuity is not autonomy: no thread creates an occasion.

        The opportunity source reads matured requests and nothing else. If it
        ever read the notebook, an open enquiry would become a reason to think,
        which is a rule about what deserves thought.
        """
        source = (SOURCE / "continuity" / "source.py").read_text()
        for forbidden in ("open_threads", "SQLiteResearchStore",
                          "research_threads", "notebook_threads",
                          "ThreadSnapshot", "alx.research"):
            self.assertNotIn(forbidden, source)
        # And the module still says so itself, which the gate relies on.
        self.assertIn("does not read goals, notebook entries", source)


class SilenceTests(NotebookTestCase):
    """Doing nothing is an ordinary outcome, not a failure."""

    def test_an_occasion_may_produce_nothing_at_all(self) -> None:
        self.thread("t-mine", "An open question", "Mine to think about.")
        reasoner = Queued(AgentDecision(finish_silently=True))
        outcome = self.agent(reasoner).process(
            conversation("(an occasion)"), RETENTION, 1
        )
        self.assertEqual(outcome.state, CoreState.FINISHED_SILENTLY)
        self.assertEqual(self.recorded, [])
        self.assertEqual(len(self.notebook.open_threads()), 1)

    def test_recording_nothing_is_described_as_ordinary(self) -> None:
        instructions = (SOURCE / "core" / "model_reasoner.py").read_text()
        self.assertIn("a turn where you record nothing", instructions)
        self.assertIn("is ordinary", instructions)


class BoundedContextTests(NotebookTestCase):
    """Awareness of her enquiries, not the enquiries themselves."""

    def test_entry_content_never_reaches_context(self) -> None:
        self.thread("t-1", "A question", "Mine.", entries=3)
        self.notebook.record_entry(
            EntryProposal("t-1-secret", "t-1", EntryKind.CONCLUSION,
                          "THE ACTUAL CONTENT OF WHAT SHE THINKS", NOW)
        )
        threads = self.notebook.open_threads()
        blob = json.dumps([dict(item) for item in threads])
        self.assertNotIn("THE ACTUAL CONTENT", blob)
        self.assertEqual(threads[0]["entry_count"], 4)

    def test_the_shape_is_exactly_six_fields(self) -> None:
        self.thread("t-1", "A question", "Mine.", entries=1)
        self.assertEqual(
            sorted(self.notebook.open_threads()[0]),
            ["entry_count", "interest", "opened_at", "question", "status",
             "thread_id"],
        )

    def test_the_count_is_bounded(self) -> None:
        for index in range(OPEN_NOTEBOOK_THREAD_LIMIT + 6):
            self.thread(f"t-{index:02d}", f"Question {index}", "Mine.")
        self.assertEqual(
            len(self.notebook.open_threads()), OPEN_NOTEBOOK_THREAD_LIMIT
        )

    def test_a_larger_limit_cannot_be_asked_for(self) -> None:
        for index in range(OPEN_NOTEBOOK_THREAD_LIMIT + 6):
            self.thread(f"t-{index:02d}", f"Question {index}", "Mine.")
        self.assertEqual(
            len(self.notebook.open_threads(999)), OPEN_NOTEBOOK_THREAD_LIMIT
        )

    def test_paused_and_archived_enquiries_stay_put_away(self) -> None:
        """She set them aside; resurfacing them would override that."""
        self.thread("t-open", "Open one", "Mine.")
        self.thread("t-paused", "Paused one", "Mine.", ThreadStatus.PAUSED)
        self.thread("t-archived", "Archived one", "Mine.", ThreadStatus.ARCHIVED)
        ids = [item["thread_id"] for item in self.notebook.open_threads()]
        self.assertEqual(ids, ["t-open"])

    def test_the_newest_enquiry_comes_first(self) -> None:
        for index in range(3):
            self.notebook.open_thread(
                ThreadProposal(f"t-{index}", f"Q{index}", "Mine.",
                               NOW + timedelta(minutes=index)),
                RETENTION,
            )
        self.assertEqual(
            [item["thread_id"] for item in self.notebook.open_threads()],
            ["t-2", "t-1", "t-0"],
        )

    def test_the_payload_stays_small(self) -> None:
        for index in range(OPEN_NOTEBOOK_THREAD_LIMIT):
            self.thread(f"t-{index:02d}", "Q" * 200, "I" * 200, entries=2)
        blob = json.dumps([dict(i) for i in self.notebook.open_threads()])
        self.assertLess(len(blob), 8_000)


class ContinuityTests(NotebookTestCase):
    """An enquiry outlives the process that opened it."""

    def test_a_thread_survives_a_store_reload(self) -> None:
        self.thread("t-mine", "A question of mine", "Because I want to know.",
                    entries=2)
        self.notebook.close()
        reopened = SQLiteResearchStore(self.notebook_path)
        self.addCleanup(reopened.close)
        threads = reopened.open_threads()
        self.assertEqual(len(threads), 1)
        self.assertEqual(threads[0]["question"], "A question of mine")
        self.assertEqual(threads[0]["entry_count"], 2)

    def test_it_reaches_a_later_turn_as_bounded_context(self) -> None:
        self.thread("t-mine", "A question of mine", "Because I want to know.",
                    entries=2)
        self.notebook.close()
        reopened = SQLiteResearchStore(self.notebook_path)
        self.addCleanup(reopened.close)
        context = ReasoningContext(
            active_goal=None, turns=(), capabilities=(), conversation_id="c1",
            origin=CognitionOrigin.SELF_REQUESTED,
            open_notebook_threads=reopened.open_threads(),
        )
        payload = json.loads(_context_payload(context))
        self.assertEqual(len(payload["open_notebook_threads"]), 1)
        self.assertEqual(
            payload["open_notebook_threads"][0]["thread_id"], "t-mine"
        )

    def test_full_content_is_still_fetched_explicitly(self) -> None:
        """The summary is a reminder; reading is a separate, deliberate act."""
        self.thread("t-mine", "A question", "Mine.", entries=2)
        summary = self.notebook.open_threads()[0]
        self.assertNotIn("entries", summary)
        full = self.notebook.read_thread("t-mine")
        self.assertEqual(len(full.entries), 2)
        self.assertTrue(full.entries[0].current.content)

    def test_every_origin_gets_the_same_context(self) -> None:
        """No second builder for what she is like when nobody is watching."""
        self.thread("t-mine", "A question", "Mine.")
        seen = {}
        for origin in (CognitionOrigin.PERSON_TURN,
                       CognitionOrigin.SELF_REQUESTED):
            reasoner = Queued(AgentDecision(finish_silently=True))
            agent = self.agent(reasoner, identifiers=())
            agent.process(conversation("x"), RETENTION, 1, origin=origin)
            seen[origin] = reasoner.contexts[0].open_notebook_threads
        self.assertEqual(seen[CognitionOrigin.PERSON_TURN],
                         seen[CognitionOrigin.SELF_REQUESTED])


class NoInferredInterestTests(unittest.TestCase):
    """Nothing decides what she finds interesting."""

    def setUp(self) -> None:
        self.store = (SOURCE / "research" / "store.py").read_text()
        self.instructions = (SOURCE / "core" / "model_reasoner.py").read_text()

    def test_the_reader_ranks_nothing(self) -> None:
        reader = self.store[self.store.index("def open_threads"):]
        reader = reader[: reader.index("\n    def ")]
        # The SQL and the returned shape, not the prose explaining them.
        code = "\n".join(
            line for line in reader.splitlines()
            if not line.strip().startswith("#")
        )
        code = code.split('"""')[0] + "".join(code.split('"""')[2:])
        for forbidden in ("score", "rank", "relevance", "priority", "important",
                          "interesting", "sentiment", "order by t.question"):
            self.assertNotIn(forbidden, code.lower())

    def test_ordering_is_by_time_alone(self) -> None:
        reader = self.store[self.store.index("def open_threads"):]
        reader = reader[: reader.index("\n    def ")]
        self.assertIn("ORDER BY t.opened_at DESC", reader)

    def test_no_topic_is_prescribed(self) -> None:
        self.assertIn("Nothing infers your interest for you", self.instructions)

    def test_the_notebook_is_named_as_hers(self) -> None:
        self.assertIn("The notebook is yours", self.instructions)

    def test_the_capability_says_whose_interest_it_records(self) -> None:
        from alx.tools.notebook import OPEN_THREAD_DEFINITION

        self.assertIn("you want to understand", OPEN_THREAD_DEFINITION.purpose)
        self.assertIn("interests you", OPEN_THREAD_DEFINITION.purpose)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
