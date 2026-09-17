"""Stage 2B: unfinished work outlives the conversation it began in.

A goal used to be visible only to the conversation that created it. That id
lives in browser storage, so clearing site data, opening a private window or
moving to another device orphaned every unfinished goal at once — durable on
disk, invisible to reasoning, with no way back because nothing could list what
AL/X could not already name.

Conversation is now an ordering fact and provenance on the row. These tests
hold the three properties that make that safe.

Deterministic code enumerates and orders by storage facts only: conversation,
exact project scope, recency. Nothing here reads what was said, scores a goal
against a message, or decides that an input belongs to old work. That
judgement is the Core's, and giving it away would be a classifier wearing a
different name.

Awareness stays bounded. The projection is capped whatever the history holds,
so the cost of remembering that work exists does not grow with how much of it
there has been.

Being listed proves only that work remains open. `from_current_conversation`
and `updated_at` are what separate that from having just been working on
something, which is a different claim needing different evidence.
"""

from __future__ import annotations

import ast
import json
import sqlite3
import sys
import tempfile
import unittest
from datetime import UTC, datetime, timedelta
from pathlib import Path

REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPOSITORY_ROOT / "src"))

from alx.contracts import (  # noqa: E402
    AgentDecision,
    GoalStopReason,
    ConversationOrigin,
    ConversationSnapshot,
    ConversationTurn,
    GoalMutationKind,
    GoalProposal,
    GoalState,
    GoalStatus,
    Objective,
    ScopeReference,
    SuccessCriterion,
    WorkItem,
)
from alx.core import CoreAgent, CoreState  # noqa: E402
from alx.core.loop import UNFINISHED_GOAL_CANDIDATES  # noqa: E402
from alx.goals import SQLiteGoalStore  # noqa: E402
from alx.goals import store as goals_store  # noqa: E402
from alx.goals.store import _goal_to_data  # noqa: E402

NOW = datetime(2026, 9, 17, 9, 0, tzinfo=UTC)
RETENTION = NOW + timedelta(days=30)


_STOP_REASONS = {
    GoalStatus.COMPLETED: GoalStopReason.SUCCESS_CRITERIA_MET,
    GoalStatus.CANCELLED: GoalStopReason.CANCELLED,
    GoalStatus.AWAITING_INPUT: GoalStopReason.REQUIRED_INPUT,
    GoalStatus.AWAITING_APPROVAL: GoalStopReason.REQUIRED_APPROVAL,
    GoalStatus.BLOCKED: GoalStopReason.GENUINELY_BLOCKED,
}


def goal(goal_id: str, status: GoalStatus = GoalStatus.ACTIVE) -> GoalState:
    return GoalState(
        goal_id,
        Objective(f"do {goal_id}", "turn:1"),
        (SuccessCriterion("c1", "done"),),
        status=status,
        stop_reason=_STOP_REASONS.get(status),
        # A paused goal must say what it is waiting for; the contract refuses
        # a status its state does not support.
        outstanding_work=(
            (WorkItem("w1", "one detail outstanding"),)
            if status is GoalStatus.AWAITING_INPUT
            else ()
        ),
    )


class StoreTestCase(unittest.TestCase):
    """Each write gets a distinct, increasing time, so order is checkable."""

    def setUp(self) -> None:
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.path = Path(self.directory.name) / "goals.sqlite3"
        self.tick = 0
        self.store = SQLiteGoalStore(self.path, clock=self.clock)
        self.addCleanup(self.store.close)

    def clock(self) -> datetime:
        self.tick += 1
        return NOW + timedelta(minutes=self.tick)

    def create(
        self,
        goal_id: str,
        conversation_id: str = "conv-1",
        *,
        project_id: str | None = None,
        status: GoalStatus = GoalStatus.ACTIVE,
    ) -> None:
        self.store.create(
            goal(goal_id, status),
            conversation_id,
            RETENTION,
            None,
            None if project_id is None else ScopeReference(project_id=project_id),
        )

    def ids(self, *arguments, **keywords) -> list[str]:
        return [
            item.goal_id for item in self.store.list_unfinished(*arguments, **keywords)
        ]


class VisibilityTests(StoreTestCase):
    """What exists is no longer decided by which conversation is current."""

    def test_a_conversations_own_goals_are_listed(self) -> None:
        """Same-conversation behaviour, unchanged."""
        self.create("a")
        self.create("b")
        self.assertEqual(sorted(self.ids("conv-1")), ["a", "b"])

    def test_a_goal_is_visible_from_a_different_conversation(self) -> None:
        """The defect: durable work orphaned by a browser storage key."""
        self.create("orphan", "conv-old")
        self.assertEqual(self.ids("conv-new"), ["orphan"])

    def test_a_goal_is_visible_with_no_conversation_at_all(self) -> None:
        self.create("orphan", "conv-old")
        self.assertEqual(self.ids(), ["orphan"])

    def test_the_origin_conversation_is_preserved(self) -> None:
        """Resuming elsewhere must not rewrite where the work began."""
        self.create("a", "conv-origin")
        self.store.list_unfinished("conv-other")
        self.assertEqual(self.store.load("a").conversation_id, "conv-origin")

    def test_a_goal_says_whether_it_is_this_conversations_own(self) -> None:
        self.create("mine", "conv-1")
        self.create("theirs", "conv-2")
        listed = {
            item.goal_id: item.from_current_conversation
            for item in self.store.list_unfinished("conv-1")
        }
        self.assertTrue(listed["mine"])
        self.assertFalse(listed["theirs"])

    def test_terminal_goals_stay_out(self) -> None:
        """Finished work is not open work, whatever conversation asks."""
        self.create("dropped", status=GoalStatus.CANCELLED)
        self.create("open", status=GoalStatus.AWAITING_INPUT)
        self.assertEqual(self.ids("conv-1"), ["open"])
        self.assertEqual(self.ids("conv-other"), ["open"])


class MigratedRecencyTests(unittest.TestCase):
    """A goal written before the column has unknown recency, not invented one.

    `updated_at` was briefly backfilled from `retention_until`. Every write
    moves that value, which is true per goal and does not carry across them: a
    retention horizon is policy, clamped for mail-derived content and never
    extendable by a replacement, so a goal written yesterday can expire before
    one written last month. Ordering by it would have ranked the older work
    first while looking like a fact.
    """

    def setUp(self) -> None:
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.path = Path(self.directory.name) / "goals.sqlite3"

    def legacy_database(self, goals: dict[str, datetime]) -> None:
        """A v6 database: no `updated_at`, and retention set per goal."""
        connection = sqlite3.connect(self.path)
        connection.execute(
            "CREATE TABLE goals (goal_id TEXT PRIMARY KEY, revision INTEGER "
            "NOT NULL, retention_until TEXT NOT NULL, state_json TEXT NOT "
            "NULL, conversation_id TEXT, content_origins TEXT, "
            "content_recorded_at TEXT, content_expires_at TEXT, "
            "mail_references TEXT, scope TEXT)"
        )
        for goal_id, retention in goals.items():
            connection.execute(
                "INSERT INTO goals(goal_id, revision, retention_until, "
                "state_json, conversation_id) VALUES (?, ?, ?, ?, ?)",
                (
                    goal_id,
                    1,
                    retention.isoformat(),
                    json.dumps(_goal_to_data(goal(goal_id))),
                    "conv-old",
                ),
            )
        connection.execute("PRAGMA user_version = 6")
        connection.commit()
        connection.close()

    def open(self, clock=None) -> SQLiteGoalStore:
        store = SQLiteGoalStore(self.path, clock=clock)
        self.addCleanup(store.close)
        return store

    def test_a_migrated_goal_has_unknown_recency(self) -> None:
        """No authoritative write time exists, so none is fabricated."""
        self.legacy_database({"old": RETENTION})
        store = self.open()
        summary = store.list_unfinished("conv-old")[0]
        self.assertIsNone(summary.updated_at)

    def test_retention_does_not_decide_recency(self) -> None:
        """The inversion the backfill would have produced.

        `short` was written after `long` but expires sooner, which is ordinary
        for mail-derived content. Ordering by retention would put `long` first
        and call it the more recent work.
        """
        self.legacy_database(
            {"long": RETENTION + timedelta(days=60), "short": RETENTION}
        )
        store = self.open()
        listed = [item.goal_id for item in store.list_unfinished("conv-old")]
        # Both have unknown recency, so neither outranks the other by a
        # retention horizon. Order falls back to rowid, not to expiry.
        self.assertEqual(sorted(listed), ["long", "short"])
        for item in store.list_unfinished("conv-old"):
            self.assertIsNone(item.updated_at)

    def test_a_touched_goal_outranks_migrated_ones(self) -> None:
        """Unknown recency sorts after known recency, so a real write leads."""
        self.legacy_database({"old-a": RETENTION, "old-b": RETENTION})
        store = self.open(clock=lambda: NOW + timedelta(hours=1))
        snapshot = store.load("old-a")
        store.replace(snapshot.state, RETENTION, snapshot.revision)
        listed = [item.goal_id for item in store.list_unfinished("conv-old")]
        self.assertEqual(listed[0], "old-a")
        self.assertIsNotNone(store.list_unfinished("conv-old")[0].updated_at)

    def test_the_migration_is_idempotent(self) -> None:
        self.legacy_database({"old": RETENTION})
        first = self.open()
        first.close()
        reopened = self.open()
        summary = reopened.list_unfinished("conv-old")[0]
        self.assertIsNone(summary.updated_at)
        self.assertEqual(summary.goal_id, "old")

    def test_migration_rewrites_no_other_metadata(self) -> None:
        """Ordering is not worth altering history for."""
        self.legacy_database({"old": RETENTION})
        store = self.open()
        snapshot = store.load("old")
        self.assertEqual(snapshot.conversation_id, "conv-old")
        self.assertEqual(snapshot.retention_until, RETENTION)
        self.assertEqual(snapshot.revision, 1)


class BoundedWorkTests(StoreTestCase):
    """The cost of one reasoning call must not grow with the history."""

    def test_only_the_consumed_rows_leave_the_database(self) -> None:
        """The bound must reach the rows, not only the decoding.

        Decoding was already bounded by the loop breaking at the cap, so
        counting decodes proves nothing about `fetchall()`: it materialises
        every matching row into Python first and the break happens after. The
        rows actually taken from the cursor are what distinguishes the two, so
        those are counted.
        """
        for index in range(60):
            self.create(f"g{index}", "conv-1")

        taken = []

        class CountingCursor:
            def __init__(self, cursor) -> None:
                self._cursor = cursor

            def __iter__(self):
                for row in self._cursor:
                    taken.append(row[0])
                    yield row

            def fetchall(self):
                rows = self._cursor.fetchall()
                taken.extend(row[0] for row in rows)
                return rows

            def __getattr__(self, name):
                return getattr(self._cursor, name)

        class CountingConnection:
            """The store's connection, with the candidate query observed."""

            def __init__(self, connection) -> None:
                self._connection = connection

            def execute(self, sql, *arguments):
                cursor = self._connection.execute(sql, *arguments)
                if "FROM goals ORDER BY" in sql:
                    return CountingCursor(cursor)
                return cursor

            def __getattr__(self, name):
                return getattr(self._connection, name)

        real_connection = self.store._connection
        self.store._connection = CountingConnection(real_connection)
        try:
            self.store.list_unfinished("conv-1", limit=10)
        finally:
            self.store._connection = real_connection

        self.assertLessEqual(
            len(taken), 12, f"took {len(taken)} rows to return 10 candidates"
        )

    def test_an_unbounded_listing_still_returns_everything(self) -> None:
        """Recovery asks for all of it deliberately, and must still get it."""
        for index in range(15):
            self.create(f"g{index}", "conv-1")
        self.assertEqual(len(self.store.list_unfinished()), 15)


class OrderingTests(StoreTestCase):
    """Priority is by storage facts, in a fixed order, and nothing else."""

    def test_the_current_conversation_leads(self) -> None:
        self.create("other", "conv-2")
        self.create("mine", "conv-1")
        self.assertEqual(self.ids("conv-1")[0], "mine")

    def test_the_active_project_follows_the_current_conversation(self) -> None:
        self.create("unrelated", "conv-2")
        self.create("project", "conv-2", project_id="pn532")
        self.create("mine", "conv-1")
        self.assertEqual(self.ids("conv-1", project_id="pn532"), [
            "mine", "project", "unrelated",
        ])

    def test_the_rest_are_ordered_by_recency(self) -> None:
        self.create("oldest", "conv-2")
        self.create("middle", "conv-2")
        self.create("newest", "conv-2")
        self.assertEqual(self.ids("conv-1"), ["newest", "middle", "oldest"])

    def test_a_revision_makes_a_goal_recent(self) -> None:
        """Ordering follows the last write, not the first."""
        self.create("first", "conv-2")
        self.create("second", "conv-2")
        snapshot = self.store.load("first")
        self.store.replace(snapshot.state, RETENTION, snapshot.revision)
        self.assertEqual(self.ids("conv-1"), ["first", "second"])

    def test_project_scope_is_exact_and_not_semantic(self) -> None:
        """A project narrows by identifier. It never judges relevance."""
        self.create("inside", "conv-2", project_id="pn532")
        self.create("outside", "conv-2", project_id="pn532-antenna")
        listed = self.store.list_unfinished("conv-1", project_id="pn532")
        by_id = {item.goal_id: item for item in listed}
        # Both are listed — nothing is hidden for being unrelated — but only
        # the exact identifier match is promoted.
        self.assertEqual([item.goal_id for item in listed][0], "inside")
        self.assertEqual(by_id["outside"].project_id, "pn532-antenna")

    def test_ordering_is_stable_across_reopening(self) -> None:
        self.create("older", "conv-2")
        self.create("newer", "conv-2")
        before = self.ids("conv-1")
        self.store.close()
        reopened = SQLiteGoalStore(self.path)
        self.addCleanup(reopened.close)
        after = [item.goal_id for item in reopened.list_unfinished("conv-1")]
        self.assertEqual(before, after)


class BoundTests(StoreTestCase):
    """Awareness of open work must not grow with the history."""

    def test_the_limit_caps_the_result(self) -> None:
        for index in range(UNFINISHED_GOAL_CANDIDATES + 5):
            self.create(f"g{index}", "conv-1")
        self.assertEqual(
            len(self.ids("conv-1", limit=UNFINISHED_GOAL_CANDIDATES)),
            UNFINISHED_GOAL_CANDIDATES,
        )

    def test_the_cap_keeps_the_highest_priority_candidates(self) -> None:
        for index in range(5):
            self.create(f"other-{index}", "conv-2")
        self.create("mine", "conv-1")
        self.assertEqual(self.ids("conv-1", limit=1), ["mine"])

    def test_a_nonsensical_limit_is_refused(self) -> None:
        for value in (0, -1, "3", True):
            with self.subTest(limit=value):
                with self.assertRaises((TypeError, ValueError)):
                    self.store.list_unfinished("conv-1", limit=value)

    def test_two_goals_in_one_project_both_remain_selectable(self) -> None:
        self.create("first", "conv-2", project_id="pn532")
        self.create("second", "conv-2", project_id="pn532")
        self.assertEqual(
            sorted(self.ids("conv-1", project_id="pn532")), ["first", "second"]
        )


class Queued:
    def __init__(self, *decisions) -> None:
        self.decisions = list(decisions)
        self.contexts = []

    def decide(self, context):
        self.contexts.append(context)
        return self.decisions.pop(0)


def conversation(conversation_id: str = "conv-new") -> ConversationSnapshot:
    return ConversationSnapshot(
        conversation_id,
        (
            ConversationTurn(
                conversation_id, "t1", ConversationOrigin.TYPED,
                "where were we?", NOW, "friedl",
            ),
        ),
        1,
        RETENTION,
    )


class CoreProjectionTests(unittest.TestCase):
    """What one reasoning call is shown, and what it may take."""

    def setUp(self) -> None:
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.tick = 0
        self.store = SQLiteGoalStore(
            Path(self.directory.name) / "goals.sqlite3", clock=self.clock
        )
        self.addCleanup(self.store.close)

    def clock(self) -> datetime:
        self.tick += 1
        return NOW + timedelta(minutes=self.tick)

    def agent(self, reasoner) -> CoreAgent:
        return CoreAgent(
            self.store, reasoner, lambda call, state: None, (),
            clock=lambda: NOW, identifier_factory=lambda: "goal-new",
        )

    def create(self, goal_id: str, conversation_id: str = "conv-old") -> None:
        self.store.create(goal(goal_id), conversation_id, RETENTION)

    def test_a_new_conversation_sees_earlier_unfinished_work(self) -> None:
        """The scenario the stage exists for: a new chat the next day."""
        self.create("yesterday")
        reasoner = Queued(AgentDecision(response="Noted."))
        self.agent(reasoner).process(conversation(), RETENTION, 5)
        offered = reasoner.contexts[0].unfinished_goals
        self.assertEqual([item.goal_id for item in offered], ["yesterday"])
        self.assertFalse(offered[0].from_current_conversation)

    def test_the_projection_is_capped(self) -> None:
        for index in range(UNFINISHED_GOAL_CANDIDATES + 5):
            self.create(f"g{index}")
        reasoner = Queued(AgentDecision(response="Noted."))
        self.agent(reasoner).process(conversation(), RETENTION, 5)
        self.assertEqual(
            len(reasoner.contexts[0].unfinished_goals), UNFINISHED_GOAL_CANDIDATES
        )

    def test_candidates_never_repeat_a_goal(self) -> None:
        self.create("mine", "conv-new")
        reasoner = Queued(AgentDecision(response="Noted."))
        self.agent(reasoner).process(conversation(), RETENTION, 5)
        offered = [item.goal_id for item in reasoner.contexts[0].unfinished_goals]
        self.assertEqual(len(offered), len(set(offered)))

    def test_a_historical_goal_can_be_resumed_under_its_own_identity(self) -> None:
        """One durable goal, continued — never copied into a new one."""
        self.create("yesterday")
        reasoner = Queued(
            AgentDecision(goal_id="yesterday"),
            AgentDecision(response="Picking that up.", goal_id="yesterday"),
        )
        outcome = self.agent(reasoner).process(conversation(), RETENTION, 5)
        self.assertIs(outcome.state, CoreState.RESPONDED)
        self.assertEqual(len(self.store.list_goals()), 1)
        self.assertEqual(
            self.store.load("yesterday").conversation_id, "conv-old"
        )

    def test_the_whole_resume_path_works_end_to_end(self) -> None:
        """Listing and loading, in one test, because there were two gates.

        The design found the conversation-scoped listing and missed the load
        guard that refused a goal whose conversation differed. Widening either
        alone leaves the path broken in a way neither half's tests would show:
        offered but not loadable, or loadable but never offered. This walks the
        whole thing, so a return of either gate fails here.
        """
        self.create("yesterday", "conv-old")
        reasoner = Queued(
            AgentDecision(goal_id="yesterday"),
            AgentDecision(response="Back on it.", goal_id="yesterday"),
        )
        outcome = self.agent(reasoner).process(conversation("conv-new"), RETENTION, 5)

        # It was offered from a conversation that did not create it.
        offered = reasoner.contexts[0].unfinished_goals
        self.assertIn("yesterday", [item.goal_id for item in offered])
        self.assertFalse(offered[0].from_current_conversation)

        # Selecting it loaded the real state rather than being refused.
        self.assertIs(outcome.state, CoreState.RESPONDED)
        self.assertIsNotNone(reasoner.contexts[1].active_goal)
        self.assertEqual(reasoner.contexts[1].active_goal.goal_id, "yesterday")

        # One durable identity, and its origin is untouched by being resumed.
        self.assertEqual(len(self.store.list_goals()), 1)
        self.assertEqual(self.store.load("yesterday").conversation_id, "conv-old")

    def test_full_state_arrives_only_after_selection(self) -> None:
        """A candidate answers whether to reopen, never what the work holds."""
        self.create("yesterday")
        reasoner = Queued(
            AgentDecision(goal_id="yesterday"),
            AgentDecision(response="Now I have it.", goal_id="yesterday"),
        )
        self.agent(reasoner).process(conversation(), RETENTION, 5)
        self.assertIsNone(reasoner.contexts[0].active_goal)
        self.assertIsNotNone(reasoner.contexts[1].active_goal)

    def test_the_selected_goal_stays_visible_beyond_the_cap(self) -> None:
        """Selected work cannot fall off the list it is being worked under.

        The projection is asked directly, because reaching this through a turn
        would need the goal to be a candidate first — and a goal outside the
        cap cannot be selected at all, which is the candidacy guard rather than
        this one. Here the goal is already selected and recency has since
        buried it: it is kept by replacing the lowest-priority candidate, so
        the bound holds rather than stretching by one.
        """
        self.create("chosen")
        for index in range(UNFINISHED_GOAL_CANDIDATES + 5):
            self.create(f"filler-{index}")
        agent = self.agent(Queued())
        offered = agent._selectable_goals("conv-new", self.store.load("chosen"))
        self.assertIn("chosen", [item.goal_id for item in offered])
        self.assertLessEqual(len(offered), UNFINISHED_GOAL_CANDIDATES)

    def test_a_restart_preserves_visibility(self) -> None:
        """Nothing about continuity depends on the process staying up."""
        self.create("yesterday")
        self.store.close()
        reopened = SQLiteGoalStore(Path(self.directory.name) / "goals.sqlite3")
        self.addCleanup(reopened.close)
        self.store = reopened
        reasoner = Queued(AgentDecision(response="Noted."))
        self.agent(reasoner).process(conversation(), RETENTION, 5)
        self.assertEqual(
            [item.goal_id for item in reasoner.contexts[0].unfinished_goals],
            ["yesterday"],
        )

    def test_an_autonomous_turn_sees_the_same_durable_work(self) -> None:
        """Visibility follows durable rules, not how the turn was woken.

        An occasion carries the conversation it arose from. When that differs
        from where the work began, autonomous AL/X used to be blind to it while
        interactive AL/X was not — the same goal, two different answers about
        whether it exists.
        """
        self.create("yesterday", "conv-old")
        reasoner = Queued(AgentDecision(finish_silently=True))
        self.agent(reasoner).process(conversation("conv-autonomous"), RETENTION, 5)
        self.assertEqual(
            [item.goal_id for item in reasoner.contexts[0].unfinished_goals],
            ["yesterday"],
        )


class NoSemanticRankingTests(StoreTestCase):
    """The line that must not be crossed."""

    def test_wording_changes_nothing_about_the_candidates(self) -> None:
        """Ordering is a fact about storage, never about the message."""
        self.create("antenna", "conv-2")
        self.create("invoice", "conv-2")
        first = self.ids("conv-1")

        class Queued:
            def __init__(self) -> None:
                self.contexts = []

            def decide(self, context):
                self.contexts.append(context)
                return AgentDecision(response="Noted.")

        offered = []
        for message in ("tell me about the antenna", "what about the invoice"):
            reasoner = Queued()
            CoreAgent(
                self.store, reasoner, lambda call, state: None, (),
                clock=lambda: NOW, identifier_factory=lambda: "goal-new",
            ).process(
                ConversationSnapshot(
                    "conv-1",
                    (
                        ConversationTurn(
                            "conv-1", "t1", ConversationOrigin.TYPED,
                            message, NOW, "friedl",
                        ),
                    ),
                    1,
                    RETENTION,
                ),
                RETENTION,
                5,
            )
            offered.append(
                [item.goal_id for item in reasoner.contexts[0].unfinished_goals]
            )
        self.assertEqual(offered[0], offered[1])
        self.assertEqual(offered[0], first)

    def test_the_listing_takes_no_conversation_content(self) -> None:
        """Law 1 at the signature: it is given identifiers, never language.

        Inspected through the AST of the real function rather than by
        searching the file for a string. A source-text check phrased as
        `"def list_unfinish" + token` can never match anything, so it passes
        whatever the code does — which is how this test first shipped.
        """
        node = self.listing_function()
        arguments = [
            item.arg
            for item in (*node.args.args, *node.args.kwonlyargs)
            if item.arg != "self"
        ]
        self.assertEqual(arguments, ["conversation_id", "project_id", "limit"])

    def test_the_listing_body_never_touches_a_conversation(self) -> None:
        """Law 1 in the body: no attribute of a turn is reachable from here.

        The signature alone would not catch a lookup added inside, so every
        attribute the function reads is checked. `conversation_id` is an
        identifier and stays allowed; anything that could carry what was said
        does not.
        """
        node = self.listing_function()
        attributes = {
            item.attr
            for item in ast.walk(node)
            if isinstance(item, ast.Attribute)
        }
        for forbidden in (
            "turns", "content", "message", "text", "utterance", "body", "prompt",
        ):
            with self.subTest(attribute=forbidden):
                self.assertNotIn(forbidden, attributes)

    def test_wording_cannot_change_the_candidates(self) -> None:
        """The property the two checks above exist to protect.

        Identical stored facts, radically different things said. If anything
        in the projection ever reads language, these two calls diverge.
        """
        self.create("antenna", "conv-2")
        self.create("invoice", "conv-2")
        first = self.candidates("the antenna is detuned and I am worried")
        second = self.candidates("unrelated: what is the invoice total")
        self.assertEqual(first, second)
        self.assertEqual(sorted(first), ["antenna", "invoice"])

    def candidates(self, message: str) -> list[str]:
        """The candidates one turn is shown, for a given thing said."""

        class Recorder:
            def __init__(self) -> None:
                self.contexts = []

            def decide(self, context):
                self.contexts.append(context)
                return AgentDecision(response="Noted.")

        reasoner = Recorder()
        CoreAgent(
            self.store, reasoner, lambda call, state: None, (),
            clock=lambda: NOW, identifier_factory=lambda: "goal-new",
        ).process(
            ConversationSnapshot(
                "conv-1",
                (
                    ConversationTurn(
                        "conv-1", "t1", ConversationOrigin.TYPED, message, NOW,
                        "friedl",
                    ),
                ),
                1,
                RETENTION,
            ),
            RETENTION,
            5,
        )
        return [item.goal_id for item in reasoner.contexts[0].unfinished_goals]

    @staticmethod
    def listing_function() -> ast.FunctionDef:
        source = (REPOSITORY_ROOT / "src/alx/goals/store.py").read_text()
        for node in ast.walk(ast.parse(source)):
            if (
                isinstance(node, ast.FunctionDef)
                and node.name == "list_unfinished"
            ):
                return node
        raise AssertionError("list_unfinished is missing from the goal store")


if __name__ == "__main__":
    unittest.main()
