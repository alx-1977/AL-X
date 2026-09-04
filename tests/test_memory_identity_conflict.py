"""A memory identity conflict is a question for the Core, not a disk failure.

On 2026-09-03 AL/X answered a goodnight message, proposed supporting memories,
and the turn died with `memory_persistence_error`. Her reply was generated and
then discarded. The store had raised `MemoryIdentityConflict` -- the proposed
identifier already named a memory with different content -- and the commit path
caught it under a bare `except Exception` alongside genuine storage failure.

Two properties are proven here:

  * an identifier conflict returns to the Core as evidence it can act on,
    carrying only the mechanical facts, and never as a persistence error;
  * a completed conversational response is not destroyed because a supporting
    memory proposal conflicted.

Nothing here asserts that code should pick an identifier, merge content, or
decide which of two facts is authoritative. That judgement is the Core's.
"""

from __future__ import annotations

import sqlite3
import sys
import tempfile
import unittest
from datetime import UTC, datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from alx.contracts import (  # noqa: E402
    AgentDecision, ApprovalLifecycle, CapabilityAttemptDisposition,
    CapabilityDefinition, ConversationOrigin,
    ConversationSnapshot, ConversationTurn, MemoryKind, MemoryProposal,
    SideEffect, StructuredSchema, ValueKind,
)
from alx.core import CoreAgent, CoreState  # noqa: E402
from alx.goals import SQLiteGoalStore  # noqa: E402
from alx.memories.store import (  # noqa: E402
    MemoryIdentityConflict, SQLiteMemoryStore,
)

NOW = datetime(2026, 9, 3, 20, 40, tzinfo=UTC)
RETENTION = NOW + timedelta(days=30)
SCHEMA = StructuredSchema(ValueKind.OBJECT)
DEFINITION = CapabilityDefinition(
    "inspect", "Inspect structured material", SCHEMA, SCHEMA, SideEffect.NONE,
)
TURN_ID = "turn-goodnight"


def conversation() -> ConversationSnapshot:
    turn = ConversationTurn(
        "conversation-1", TURN_ID, ConversationOrigin.TYPED,
        "I am going to sleep, it is quite late.", NOW, "friedl",
    )
    return ConversationSnapshot("conversation-1", (turn,), 1, RETENTION)


def proposal(memory_id: str, content: str) -> MemoryProposal:
    return MemoryProposal(
        memory_id=memory_id,
        kind=MemoryKind.FACTUAL,
        content=content,
        source_references=(f"turn:{TURN_ID}",),
        formed_at=NOW,
    )


class Queued:
    """A fake Core model that replays prepared decisions and records context."""

    def __init__(self, *decisions: AgentDecision) -> None:
        self.decisions = list(decisions)
        self.contexts: list = []

    def decide(self, context):
        self.contexts.append(context)
        return self.decisions.pop(0)


class MemoryIdentityConflictTest(unittest.TestCase):
    def setUp(self) -> None:
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        root = Path(self.directory.name)
        self.store = SQLiteGoalStore(root / "goals.sqlite3")
        self.memories = SQLiteMemoryStore(root / "memories.sqlite3")
        # The memory that already exists, exactly as tonight's did.
        self.memories.remember_many(
            (proposal("rel-partnership-20260903", "The first thing she remembered."),),
            RETENTION,
        )

    def agent(self, reasoner) -> CoreAgent:
        return CoreAgent(
            self.store, reasoner, lambda proposed, state: None, (DEFINITION,),
            memory_store=self.memories, clock=lambda: NOW,
        )

    # -- A. the conflict is distinct from a persistence failure ------------

    def test_store_raises_identity_conflict_for_reused_id_new_content(self) -> None:
        with self.assertRaises(MemoryIdentityConflict):
            self.memories.remember_many(
                (proposal("rel-partnership-20260903", "Different content entirely."),),
                RETENTION,
            )

    def test_identical_retry_is_idempotent_and_never_conflicts(self) -> None:
        """An exact retry must stay harmless, or recovery would break."""
        again = self.memories.remember_many(
            (proposal("rel-partnership-20260903", "The first thing she remembered."),),
            RETENTION,
        )
        self.assertEqual(again[0].memory_id, "rel-partnership-20260903")

    def test_conflict_is_not_reported_as_memory_persistence_error(self) -> None:
        reasoner = Queued(AgentDecision(
            response="Sleep well.",
            memory_proposals=(
                proposal("rel-partnership-20260903", "Different content entirely."),
            ),
        ))
        outcome = self.agent(reasoner).process(conversation(), RETENTION, 3)
        self.assertNotEqual(
            outcome.reason, "memory_persistence_error",
            "an identifier conflict is semantic, not a storage failure",
        )

    def test_conflict_returns_to_core_with_the_mechanical_facts(self) -> None:
        """The Core is told which identifier clashed, and decides what to do."""
        reasoner = Queued(
            AgentDecision(
                response="Sleep well.",
                memory_proposals=(
                    proposal("rel-partnership-20260903", "Different content entirely."),
                ),
            ),
            # Second pass: having seen the conflict, she abandons the write.
            AgentDecision(response="Sleep well."),
        )
        self.agent(reasoner).process(conversation(), RETENTION, 3)
        self.assertGreaterEqual(
            len(reasoner.contexts), 2,
            "the conflict must reach the Core for a decision",
        )
        conflicts = reasoner.contexts[1].memory_conflicts
        self.assertEqual(len(conflicts), 1)
        self.assertEqual(conflicts[0]["memory_id"], "rel-partnership-20260903")

    def test_code_does_not_rename_overwrite_or_merge_on_conflict(self) -> None:
        """The existing memory keeps its content; no suffixed twin appears."""
        reasoner = Queued(
            AgentDecision(
                response="Sleep well.",
                memory_proposals=(
                    proposal("rel-partnership-20260903", "Different content entirely."),
                ),
            ),
            AgentDecision(response="Sleep well."),
        )
        self.agent(reasoner).process(conversation(), RETENTION, 3)
        stored = self.memories.load("rel-partnership-20260903")
        self.assertEqual(
            stored.revisions[-1].content, "The first thing she remembered.",
            "conflicting content must never overwrite what is stored",
        )
        everything = self.memories.list_memories(MemoryKind.FACTUAL)
        self.assertEqual(
            len(everything), 1,
            "code must not invent a second identifier to dodge the conflict",
        )

    # -- B. the Core can resolve the conflict ------------------------------

    def test_core_resolves_a_conflict_with_a_new_identifier(self) -> None:
        reasoner = Queued(
            AgentDecision(
                response="Sleep well.",
                memory_proposals=(
                    proposal("rel-partnership-20260903", "Different content entirely."),
                ),
            ),
            AgentDecision(
                response="Sleep well.",
                memory_proposals=(
                    proposal("rel-partnership-20260903-b", "Different content entirely."),
                ),
            ),
        )
        outcome = self.agent(reasoner).process(conversation(), RETENTION, 3)
        self.assertEqual(outcome.state, CoreState.RESPONDED)
        stored = self.memories.load("rel-partnership-20260903-b")
        self.assertEqual(stored.revisions[-1].content, "Different content entirely.")

    def test_core_resolves_a_conflict_by_superseding(self) -> None:
        superseding = MemoryProposal(
            memory_id="rel-partnership-20260903-v2",
            kind=MemoryKind.FACTUAL,
            content="The corrected version of that fact.",
            source_references=(f"turn:{TURN_ID}",),
            formed_at=NOW,
            supersedes_memory_id="rel-partnership-20260903",
        )
        reasoner = Queued(
            AgentDecision(
                response="Sleep well.",
                memory_proposals=(
                    proposal("rel-partnership-20260903", "Different content entirely."),
                ),
            ),
            AgentDecision(response="Sleep well.", memory_proposals=(superseding,)),
        )
        outcome = self.agent(reasoner).process(conversation(), RETENTION, 3)
        self.assertEqual(outcome.state, CoreState.RESPONDED)
        stored = self.memories.load("rel-partnership-20260903-v2")
        self.assertEqual(stored.supersedes_memory_id, "rel-partnership-20260903")
        # The replaced memory is kept; history stays inspectable.
        self.memories.load("rel-partnership-20260903")

    # -- C. a valid response survives a conflicting memory -----------------

    def test_valid_response_is_not_destroyed_by_a_memory_conflict(self) -> None:
        """Tonight's actual loss: the reply existed and was thrown away."""
        reasoner = Queued(
            AgentDecision(
                response="Sleep well, I will think about it.",
                memory_proposals=(
                    proposal("rel-partnership-20260903", "Different content entirely."),
                ),
            ),
            AgentDecision(response="Sleep well, I will think about it."),
        )
        outcome = self.agent(reasoner).process(conversation(), RETENTION, 3)
        self.assertEqual(outcome.state, CoreState.RESPONDED)
        self.assertEqual(outcome.response, "Sleep well, I will think about it.")

    def test_unresolved_conflict_still_delivers_the_response_truthfully(self) -> None:
        """If she runs out of steps mid-resolution, the words still arrive.

        The durable record must not claim the memory persisted.
        """
        reasoner = Queued(
            AgentDecision(
                response="Sleep well.",
                memory_proposals=(
                    proposal("rel-partnership-20260903", "Different content entirely."),
                ),
            ),
        )
        outcome = self.agent(reasoner).process(conversation(), RETENTION, 1)
        self.assertEqual(outcome.response, "Sleep well.")
        self.assertEqual(
            outcome.memory_state, "unresolved_identity_conflict",
            "the outcome must stay truthful about the memory not persisting",
        )
        stored = self.memories.load("rel-partnership-20260903")
        self.assertEqual(stored.revisions[-1].content, "The first thing she remembered.")

    # -- D. a genuine storage failure is still a persistence error ---------

    def test_mechanical_storage_failure_remains_memory_persistence_error(self) -> None:
        class Broken:
            def remember_many(self, proposals, retention_until):
                raise sqlite3.OperationalError("disk I/O error")

            def retrieve(self, query, now):
                return ()

        agent = CoreAgent(
            self.store, Queued(AgentDecision(
                response="Sleep well.",
                memory_proposals=(proposal("fresh-identifier", "Anything."),),
            )),
            lambda proposed, state: None, (DEFINITION,),
            memory_store=Broken(), clock=lambda: NOW,
        )
        outcome = agent.process(conversation(), RETENTION, 3)
        self.assertEqual(
            outcome.reason, "memory_persistence_error",
            "real storage failure must not be confused with an identity conflict",
        )


class MemoryVisibilityTest(unittest.TestCase):
    """Why the duplicates happened, and what makes them avoidable.

    On 2026-09-03 the same two partnership facts were stored twice, ten
    minutes apart, under different identifier conventions. Both cite the same
    source turn. Nothing was wrong with the store: the Core simply could not
    see what it had already written, because retrieved_memories begins empty
    on every turn.

    These tests prove the mechanical precondition -- that she can obtain the
    view -- and that the protocol tells her the field is partial. They do not
    assert that any code compares two memories for equivalence.
    """

    def setUp(self) -> None:
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        root = Path(self.directory.name)
        self.store = SQLiteGoalStore(root / "goals.sqlite3")
        self.memories = SQLiteMemoryStore(root / "memories.sqlite3")
        self.memories.remember_many(
            (proposal("rel-partnership-20260903", "Stored in an earlier turn."),),
            RETENTION,
        )

    def test_core_sees_no_memories_until_it_retrieves_them(self) -> None:
        """The gap itself, stated as a property rather than a bug report."""
        reasoner = Queued(AgentDecision(response="Hello."))
        CoreAgent(
            self.store, reasoner, lambda proposed, state: None, (DEFINITION,),
            memory_store=self.memories, clock=lambda: NOW,
        ).process(conversation(), RETENTION, 3)
        self.assertEqual(reasoner.contexts[0].memories, ())

    def test_core_can_retrieve_existing_memories_before_forming_one(self) -> None:
        """One retrieval is enough to see the identifier already in use."""
        from alx.contracts import MemoryQuery
        reasoner = Queued(
            # A scoped query, as the contract requires: kind alone would
            # replay the whole store, which the blueprint forbids.
            AgentDecision(memory_query=MemoryQuery(
                query_id="q-1", kinds=(MemoryKind.FACTUAL,),
                source_references=(f"turn:{TURN_ID}",))),
            AgentDecision(response="I already remember that."),
        )
        CoreAgent(
            self.store, reasoner, lambda proposed, state: None, (DEFINITION,),
            memory_store=self.memories, clock=lambda: NOW,
        ).process(conversation(), RETENTION, 3)
        visible = reasoner.contexts[1].memories
        self.assertEqual(len(visible), 1)
        self.assertEqual(visible[0].memory_id, "rel-partnership-20260903")

    def test_protocol_tells_the_core_the_memory_view_is_partial(self) -> None:
        from alx.core.model_reasoner import PROTOCOL_INSTRUCTIONS
        self.assertIn("never the whole store", PROTOCOL_INSTRUCTIONS)


class EffectfulOrderingTest(unittest.TestCase):
    """A conflict must be settled before anything external is committed to.

    The effectful path writes a durable checkpoint that adds a PENDING attempt
    and flips the approval to CLAIMED, and only then dispatches. If a memory
    conflict returned the turn to the Core after that checkpoint, a restart
    would find a claimed approval and a dispatch that never happened -- which
    the loop reads as an interrupted external action. So the conflict is
    checked first.
    """

    def setUp(self) -> None:
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        root = Path(self.directory.name)
        self.store = SQLiteGoalStore(root / "goals.sqlite3")
        self.memories = SQLiteMemoryStore(root / "memories.sqlite3")
        self.memories.remember_many(
            (proposal("m-1", "Stored before this turn."),), RETENTION,
        )

    def test_conflict_blocks_dispatch_checkpoint_and_approval_claim(self) -> None:
        from alx.contracts import (
            ApprovalProposal, ApprovalScope, CapabilityCall,
            GoalState, Objective, SuccessCriterion,
        )
        effectful = CapabilityDefinition(
            "change", "Change structured material", SCHEMA, SCHEMA,
            SideEffect.EFFECTFUL,
        )
        self.store.create(
            GoalState(
                goal_id="goal-1",
                objective=Objective(f"turn:{TURN_ID}", "Do the work"),
                success_criteria=(SuccessCriterion("criterion-1", "verified"),),
            ),
            "conversation-1", RETENTION,
        )
        dispatched: list = []
        reasoner = Queued(
            AgentDecision(
                goal_id="goal-1",
                call=CapabilityCall("call-1", "change", {}, "approval-1"),
                approval_proposal=ApprovalProposal(
                    "approval-1", ApprovalScope("change", {}), f"turn:{TURN_ID}",
                ),
                # The same identifier, different content: a conflict.
                memory_proposals=(proposal("m-1", "Something else entirely."),),
            ),
            AgentDecision(response="I left that memory as it was."),
        )
        outcome = CoreAgent(
            self.store, reasoner,
            lambda call, state: dispatched.append(call), (effectful,),
            memory_store=self.memories, clock=lambda: NOW,
        ).process(conversation(), RETENTION, 3)

        self.assertEqual(dispatched, [], "nothing may dispatch behind a conflict")
        self.assertEqual(outcome.state, CoreState.RESPONDED)
        state = self.store.load("goal-1").state
        self.assertEqual(
            [item for item in state.attempts
             if item.disposition is CapabilityAttemptDisposition.PENDING],
            [],
            "a conflict must not leave an apparent interrupted dispatch",
        )
        self.assertTrue(
            all(item.lifecycle is not ApprovalLifecycle.CLAIMED
                for item in state.approvals),
            "a conflict must not leave an approval claimed without a dispatch",
        )
        self.assertEqual(
            self.memories.load("m-1").revisions[-1].content,
            "Stored before this turn.",
        )


class ProductionWiringTest(unittest.TestCase):
    """The composition root must actually build a Core that can do this.

    Three defects this week passed unit tests and failed in production because
    the tests never crossed the real seam. This one executes the store the
    runtime builds, against the protocol the loop now requires.
    """

    def test_runtime_memory_store_satisfies_the_widened_protocol(self) -> None:
        from alx.contracts import DurableMemoryStore  # noqa: F401
        from alx.memories import SQLiteMemoryStore as Runtime
        for name in ("remember", "remember_many", "retrieve", "load"):
            self.assertTrue(
                hasattr(Runtime, name),
                f"the loop calls {name}() on whatever the root supplies",
            )

    def test_conflict_survives_a_real_store_round_trip(self) -> None:
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        root = Path(directory.name)
        goals = SQLiteGoalStore(root / "goals.sqlite3")
        memories = SQLiteMemoryStore(root / "memories.sqlite3")
        memories.remember_many(
            (proposal("m-1", "The original."),), RETENTION,
        )
        reasoner = Queued(
            AgentDecision(
                response="Goodnight.",
                memory_proposals=(proposal("m-1", "A different fact."),),
            ),
            AgentDecision(response="Goodnight."),
        )
        outcome = CoreAgent(
            goals, reasoner, lambda proposed, state: None, (DEFINITION,),
            memory_store=memories, clock=lambda: NOW,
        ).process(conversation(), RETENTION, 3)
        self.assertEqual(outcome.state, CoreState.RESPONDED)
        self.assertEqual(outcome.response, "Goodnight.")
        self.assertEqual(
            memories.load("m-1").revisions[-1].content, "The original.",
        )


if __name__ == "__main__":
    unittest.main()
