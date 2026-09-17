"""Stage 1: the project-scope foundation, and the boundaries it must not cross.

A scope is a durable contextual coordinate. These tests hold it to being
exactly that: identity and lifecycle, attachable to records that already own
their own content, and incapable of becoming a second memory system, an
authority boundary, or a reason for an existing record to change.

The emphasis is deliberately on what must *not* happen. A new optional field
is easy to add and easy to get wrong in ways that only show up later: an
unscoped record that stops being valid, a scope that silently crosses a person
boundary, an archive that takes memory with it. Each of those has a test here.
"""

from __future__ import annotations

import inspect
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
    ContentOrigin,
    GoalState,
    MemoryKind,
    MemoryProposal,
    Objective,
    Project,
    ProjectStatus,
    RetentionPolicy,
    ScopeReference,
    SuccessCriterion,
    scope_from_storage,
    scope_to_storage,
)
from alx.goals.store import SQLiteGoalStore  # noqa: E402
from alx.memories import MemoryIdentityConflict, SQLiteMemoryStore  # noqa: E402
from alx.projects import (  # noqa: E402
    DuplicateProject,
    ProjectNotFound,
    SQLiteProjectStore,
    UnsupportedSchema,
)

NOW = datetime(2026, 9, 17, 9, 0, tzinfo=UTC)
RETENTION = NOW + timedelta(days=30)


def goal(goal_id: str = "g1") -> GoalState:
    return GoalState(
        goal_id,
        Objective("prepare the antenna measurements", "turn:1"),
        (SuccessCriterion("c1", "measurements recorded"),),
    )


class ScopeReferenceTests(unittest.TestCase):
    """The value object, which is the extension point for later dimensions."""

    def test_a_scope_names_its_dimensions(self) -> None:
        self.assertEqual(ScopeReference(project_id="p1").dimensions, ("project_id",))

    def test_an_empty_scope_is_refused(self) -> None:
        """Unscoped is a null column, not an object naming nothing."""
        with self.assertRaises(ValueError):
            ScopeReference()

    def test_a_blank_project_id_is_refused(self) -> None:
        with self.assertRaises(ValueError):
            ScopeReference(project_id="   ")

    def test_a_scope_round_trips_through_storage(self) -> None:
        scope = ScopeReference(project_id="pn532-antenna")
        self.assertEqual(scope_from_storage(scope_to_storage(scope)), scope)

    def test_unscoped_round_trips_as_none(self) -> None:
        """The property every pre-existing record depends on."""
        self.assertIsNone(scope_to_storage(None))
        self.assertIsNone(scope_from_storage(None))

    def test_a_corrupt_stored_scope_is_refused(self) -> None:
        for value in ('["p1"]', '{"project_id": 7}'):
            with self.subTest(value=value):
                with self.assertRaises(ValueError):
                    scope_from_storage(value)

    def test_a_stored_object_naming_nothing_is_refused(self) -> None:
        """Otherwise unscoped would have two representations."""
        with self.assertRaises(ValueError):
            scope_from_storage("{}")


class ProjectIdentityTests(unittest.TestCase):
    """Identity is durable; lifecycle describes work, not records."""

    def setUp(self) -> None:
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.path = Path(self.directory.name) / "projects.sqlite3"
        self.store = SQLiteProjectStore(self.path)
        self.addCleanup(self.store.close)

    def test_a_project_survives_reopening(self) -> None:
        self.store.create(Project("p1", "PN532 antenna", NOW))
        self.store.close()
        reopened = SQLiteProjectStore(self.path)
        self.addCleanup(reopened.close)
        loaded = reopened.load("p1")
        self.assertEqual(loaded.name, "PN532 antenna")
        self.assertEqual(loaded.created_at, NOW)
        self.assertIs(loaded.status, ProjectStatus.ACTIVE)

    def test_provenance_survives_a_round_trip(self) -> None:
        provenance = RetentionPolicy().non_mail(ContentOrigin.PERSON, NOW)
        self.store.create(Project("p1", "PN532 antenna", NOW, provenance=provenance))
        self.assertEqual(self.store.load("p1").provenance, provenance)

    def test_a_project_without_provenance_stays_unstamped(self) -> None:
        self.store.create(Project("p1", "PN532 antenna", NOW))
        self.assertIsNone(self.store.load("p1").provenance)

    def test_a_duplicate_identifier_is_refused(self) -> None:
        self.store.create(Project("p1", "PN532 antenna", NOW))
        with self.assertRaises(DuplicateProject):
            self.store.create(Project("p1", "A different name", NOW))

    def test_an_unknown_project_is_not_invented(self) -> None:
        with self.assertRaises(ProjectNotFound):
            self.store.load("never-created")

    def test_a_naive_timestamp_is_refused(self) -> None:
        with self.assertRaises(ValueError):
            Project("p1", "PN532 antenna", datetime(2026, 9, 17, 9, 0))

    def test_a_blank_name_is_refused(self) -> None:
        with self.assertRaises(ValueError):
            Project("p1", "   ", NOW)

    def test_projects_list_in_creation_order(self) -> None:
        self.store.create(Project("p1", "First", NOW))
        self.store.create(Project("p2", "Second", NOW))
        self.assertEqual(
            [item.project_id for item in self.store.list_projects()], ["p1", "p2"]
        )

    def test_a_newer_schema_is_refused_rather_than_guessed(self) -> None:
        connection = sqlite3.connect(self.path)
        connection.execute("PRAGMA user_version = 99")
        connection.commit()
        connection.close()
        with self.assertRaises(UnsupportedSchema):
            SQLiteProjectStore(self.path)


class ProjectLifecycleTests(unittest.TestCase):
    """Archiving retires the work. It must not retire what was learned."""

    def setUp(self) -> None:
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        root = Path(self.directory.name)
        self.projects = SQLiteProjectStore(root / "projects.sqlite3")
        self.memories = SQLiteMemoryStore(root / "memories.sqlite3")
        self.addCleanup(self.projects.close)
        self.addCleanup(self.memories.close)
        self.projects.create(Project("p1", "PN532 antenna", NOW))

    def scoped_memory(self, memory_id: str = "m1") -> None:
        self.memories.remember(
            MemoryProposal(
                memory_id,
                MemoryKind.FACTUAL,
                "the antenna matched at 13.56 MHz",
                ("turn:1",),
                NOW,
                scope=ScopeReference(project_id="p1"),
            ),
            RETENTION,
        )

    def test_archiving_hides_the_project_from_active_listings(self) -> None:
        self.projects.set_status("p1", ProjectStatus.ARCHIVED)
        self.assertEqual(self.projects.list_projects(status=ProjectStatus.ACTIVE), ())
        self.assertEqual(
            [item.project_id for item in self.projects.list_projects()], ["p1"]
        )

    def test_archiving_does_not_touch_scoped_memory(self) -> None:
        """A project's lifecycle says nothing about the truth of what it holds."""
        self.scoped_memory()
        self.projects.set_status("p1", ProjectStatus.ARCHIVED)
        remembered = self.memories.load("m1")
        self.assertEqual(remembered.current.content, "the antenna matched at 13.56 MHz")
        self.assertEqual(remembered.scope, ScopeReference(project_id="p1"))

    def test_archiving_needs_no_reference_information(self) -> None:
        """Archive and delete are different acts, and stay different.

        Archiving retires the work and destroys nothing, so it takes no count
        and must not have acquired one when deletion was made to fail closed.
        """
        self.scoped_memory()
        self.projects.set_status("p1", ProjectStatus.ARCHIVED)
        self.assertIs(self.projects.load("p1").status, ProjectStatus.ARCHIVED)
        self.assertEqual(
            list(inspect.signature(SQLiteProjectStore.set_status).parameters),
            ["self", "project_id", "status"],
        )

    def test_a_project_can_be_reopened(self) -> None:
        self.projects.set_status("p1", ProjectStatus.ARCHIVED)
        self.projects.set_status("p1", ProjectStatus.ACTIVE)
        self.assertIs(self.projects.load("p1").status, ProjectStatus.ACTIVE)

    def test_no_public_api_can_remove_a_project(self) -> None:
        """The Stage 1 invariant, asserted against the surface itself.

        A record may name an archived project indefinitely, so nothing here may
        take that project away. Physical deletion cannot be made safe at this
        boundary: the references live in stores this one must not read, and a
        count taken from a caller is only true until the moment after it is
        taken. Removing the operation removes the race with it.
        """
        for removed in ("delete", "remove", "purge", "drop", "destroy"):
            with self.subTest(operation=removed):
                self.assertFalse(hasattr(SQLiteProjectStore, removed))

    def test_the_store_issues_no_delete_statement(self) -> None:
        """A soft-delete alias that physically deletes underneath is still it."""
        source = (REPOSITORY_ROOT / "src/alx/projects/store.py").read_text()
        self.assertNotIn("DELETE FROM", source.upper())

    def test_a_scoped_record_keeps_a_resolvable_project(self) -> None:
        """The point of the invariant: a reference stays resolvable for good."""
        self.scoped_memory()
        self.projects.set_status("p1", ProjectStatus.ARCHIVED)
        scope = self.memories.load("m1").scope
        assert scope is not None and scope.project_id is not None
        resolved = self.projects.load(scope.project_id)
        self.assertEqual(resolved.project_id, "p1")
        self.assertIs(resolved.status, ProjectStatus.ARCHIVED)

    def test_an_archived_project_survives_reopening_the_store(self) -> None:
        """Archived is retired, never gone: identity outlives the process."""
        self.scoped_memory()
        self.projects.set_status("p1", ProjectStatus.ARCHIVED)
        self.projects.close()
        reopened = SQLiteProjectStore(
            Path(self.directory.name) / "projects.sqlite3"
        )
        self.addCleanup(reopened.close)
        self.assertIs(reopened.load("p1").status, ProjectStatus.ARCHIVED)
        self.assertEqual(reopened.load("p1").name, "PN532 antenna")


class MemoryScopeTests(unittest.TestCase):
    """Scope is optional on memory, and never an isolation boundary."""

    def setUp(self) -> None:
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.path = Path(self.directory.name) / "memories.sqlite3"
        self.store = SQLiteMemoryStore(self.path)
        self.addCleanup(self.store.close)

    def test_a_memory_without_a_project_remains_valid(self) -> None:
        """Memory is platform-wide: unscoped is ordinary, not incomplete."""
        self.store.remember(
            MemoryProposal("m1", MemoryKind.FACTUAL, "a fact", ("turn:1",), NOW),
            RETENTION,
        )
        self.assertIsNone(self.store.load("m1").scope)

    def test_a_scoped_memory_round_trips(self) -> None:
        self.store.remember(
            MemoryProposal(
                "m1", MemoryKind.FACTUAL, "a fact", ("turn:1",), NOW,
                scope=ScopeReference(project_id="p1"),
            ),
            RETENTION,
        )
        self.store.close()
        reopened = SQLiteMemoryStore(self.path)
        self.addCleanup(reopened.close)
        self.assertEqual(reopened.load("m1").scope, ScopeReference(project_id="p1"))

    def test_scope_does_not_cross_a_person_boundary(self) -> None:
        """Relationship memory is isolated by person_id, and only by person_id.

        Two people's relationship memories sharing one project must stay
        separated exactly as before: a scope is a label, never a permission.
        """
        for person in ("friedl", "someone-else"):
            self.store.remember(
                MemoryProposal(
                    f"m-{person}",
                    MemoryKind.RELATIONSHIP,
                    f"a preference of {person}",
                    ("turn:1",),
                    NOW,
                    person_id=person,
                    scope=ScopeReference(project_id="p1"),
                ),
                RETENTION,
            )
        friedl = self.store.list_memories(
            MemoryKind.RELATIONSHIP, person_id="friedl"
        )
        self.assertEqual([item.memory_id for item in friedl], ["m-friedl"])
        self.assertEqual(friedl[0].scope, ScopeReference(project_id="p1"))

    def test_supersession_is_unchanged_by_scope(self) -> None:
        """The existing evolve-by-supersede behaviour must not shift."""
        self.store.remember(
            MemoryProposal(
                "m1", MemoryKind.FACTUAL, "older", ("turn:1",), NOW,
                scope=ScopeReference(project_id="p1"),
            ),
            RETENTION,
        )
        self.store.remember(
            MemoryProposal(
                "m2", MemoryKind.FACTUAL, "newer", ("turn:2",), NOW,
                supersedes_memory_id="m1",
                scope=ScopeReference(project_id="p1"),
            ),
            RETENTION,
        )
        self.assertEqual(self.store.load("m2").supersedes_memory_id, "m1")
        # The superseded memory is kept and stays inspectable.
        self.assertEqual(self.store.load("m1").current.content, "older")

    def test_a_differing_scope_under_one_identifier_conflicts(self) -> None:
        """One identifier must not quietly come to mean two places.

        Scope is part of what constitutes the memory, so a proposal reusing an
        identifier with a different scope is a different memory. Before this
        was compared, the retry resolved silently to the stored memory and the
        caller received the old scope having asked for the new one.
        """
        self.store.remember(
            MemoryProposal(
                "m1", MemoryKind.FACTUAL, "a fact", ("turn:1",), NOW,
                scope=ScopeReference(project_id="A"),
            ),
            RETENTION,
        )
        with self.assertRaises(MemoryIdentityConflict):
            self.store.remember(
                MemoryProposal(
                    "m1", MemoryKind.FACTUAL, "a fact", ("turn:1",), NOW,
                    scope=ScopeReference(project_id="B"),
                ),
                RETENTION,
            )
        self.assertEqual(self.store.load("m1").scope, ScopeReference(project_id="A"))

    def test_scoping_a_previously_unscoped_identifier_conflicts(self) -> None:
        """Gaining a scope is also a change of what the identifier means."""
        self.store.remember(
            MemoryProposal("m1", MemoryKind.FACTUAL, "a fact", ("turn:1",), NOW),
            RETENTION,
        )
        with self.assertRaises(MemoryIdentityConflict):
            self.store.remember(
                MemoryProposal(
                    "m1", MemoryKind.FACTUAL, "a fact", ("turn:1",), NOW,
                    scope=ScopeReference(project_id="A"),
                ),
                RETENTION,
            )

    def test_an_identical_scoped_retry_is_still_idempotent(self) -> None:
        """The conflict guard must not become an obstacle to a plain retry.

        Comparing provenance once made this guard unreachable and ended a live
        conversation mid-sentence. Adding scope to the comparison must not
        repeat that: the same memory proposed twice still resolves quietly.
        """
        proposal = MemoryProposal(
            "m1", MemoryKind.FACTUAL, "a fact", ("turn:1",), NOW,
            scope=ScopeReference(project_id="A"),
        )
        self.store.remember(proposal, RETENTION)
        again = self.store.remember(proposal, RETENTION)
        self.assertEqual(again.scope, ScopeReference(project_id="A"))
        self.assertEqual(len(again.revisions), 1)

    def test_a_scope_of_the_wrong_type_is_refused(self) -> None:
        with self.assertRaises(TypeError):
            MemoryProposal(
                "m1", MemoryKind.FACTUAL, "a fact", ("turn:1",), NOW,
                scope="p1",  # type: ignore[arg-type]
            )

    def test_a_legacy_database_migrates_and_reads_unscoped(self) -> None:
        """Every memory written before scopes existed must still load."""
        legacy = Path(self.directory.name) / "legacy.sqlite3"
        connection = sqlite3.connect(legacy)
        connection.execute(
            "CREATE TABLE memories (memory_id TEXT PRIMARY KEY, kind TEXT NOT "
            "NULL, person_id TEXT, supersedes_memory_id TEXT, retention_until "
            "TEXT NOT NULL)"
        )
        connection.execute(
            "CREATE TABLE memory_revisions (memory_id TEXT NOT NULL REFERENCES "
            "memories(memory_id) ON DELETE CASCADE, revision INTEGER NOT NULL, "
            "revision_json TEXT NOT NULL, content_origins TEXT, "
            "content_recorded_at TEXT, content_expires_at TEXT, "
            "mail_references TEXT, PRIMARY KEY(memory_id, revision))"
        )
        connection.execute(
            "INSERT INTO memories VALUES (?, ?, ?, ?, ?)",
            ("old", "factual", None, None, RETENTION.isoformat()),
        )
        connection.execute(
            "INSERT INTO memory_revisions(memory_id, revision, revision_json) "
            "VALUES (?, ?, ?)",
            (
                "old",
                1,
                json.dumps(
                    {
                        "revision": 1,
                        "content": "a fact learned before scopes existed",
                        "source_references": ["turn:0"],
                        "recorded_at": NOW.isoformat(),
                        "reason": None,
                        "meaning": None,
                    }
                ),
            ),
        )
        connection.execute("PRAGMA user_version = 2")
        connection.commit()
        connection.close()

        store = SQLiteMemoryStore(legacy)
        self.addCleanup(store.close)
        remembered = store.load("old")
        self.assertEqual(
            remembered.current.content, "a fact learned before scopes existed"
        )
        self.assertIsNone(remembered.scope)


class GoalScopeTests(unittest.TestCase):
    """Scope labels the work without becoming a second way to find it."""

    def setUp(self) -> None:
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.path = Path(self.directory.name) / "goals.sqlite3"
        self.store = SQLiteGoalStore(self.path)
        self.addCleanup(self.store.close)

    def test_a_goal_without_a_project_remains_valid(self) -> None:
        self.store.create(goal(), "conv-1", RETENTION)
        self.assertIsNone(self.store.load("g1").scope)

    def test_a_scoped_goal_round_trips(self) -> None:
        self.store.create(
            goal(), "conv-1", RETENTION, None, ScopeReference(project_id="p1")
        )
        self.store.close()
        reopened = SQLiteGoalStore(self.path)
        self.addCleanup(reopened.close)
        self.assertEqual(reopened.load("g1").scope, ScopeReference(project_id="p1"))

    def test_goal_selection_is_unchanged(self) -> None:
        """Stage 1 adds no new way to reach a goal; selection still lists by
        conversation, exactly as before."""
        self.store.create(
            goal("g1"), "conv-1", RETENTION, None, ScopeReference(project_id="p1")
        )
        self.store.create(goal("g2"), "conv-1", RETENTION)
        self.store.create(goal("g3"), "conv-2", RETENTION)
        self.assertEqual(
            [item.goal_id for item in self.store.list_unfinished("conv-1")],
            ["g1", "g2"],
        )
        self.assertEqual(
            [item.goal_id for item in self.store.list_unfinished("conv-2")], ["g3"]
        )

    def test_scope_survives_a_goal_revision(self) -> None:
        """Updating a goal must not quietly drop where it belongs.

        Both the snapshot `replace` hands back and the one reloaded from the
        store are checked. Asserting only on the reload hid a real defect: the
        database kept the scope because the UPDATE leaves the column alone,
        while the returned snapshot took the field's default and reported the
        goal as unscoped until somebody happened to load it again.
        """
        snapshot = self.store.create(
            goal(), "conv-1", RETENTION, None, ScopeReference(project_id="p1")
        )
        replaced = GoalState(
            "g1",
            Objective("prepare the antenna measurements", "turn:1"),
            (SuccessCriterion("c1", "measurements recorded"),),
            progress=(),
        )
        returned = self.store.replace(replaced, RETENTION, snapshot.revision)
        self.assertEqual(returned.scope, ScopeReference(project_id="p1"))
        self.assertEqual(self.store.load("g1").scope, ScopeReference(project_id="p1"))

    def test_scope_survives_a_revision_carrying_memories(self) -> None:
        """The second replacement path returns the same scope as the first."""
        snapshot = self.store.create(
            goal(), "conv-1", RETENTION, None, ScopeReference(project_id="p1")
        )
        returned = self.store.replace_with_memory_batch(
            goal(),
            RETENTION,
            snapshot.revision,
            (
                MemoryProposal(
                    "m1", MemoryKind.FACTUAL, "a fact", ("turn:1",), NOW
                ),
            ),
        )
        self.assertEqual(returned.scope, ScopeReference(project_id="p1"))
        self.assertEqual(self.store.load("g1").scope, ScopeReference(project_id="p1"))

    def test_an_unscoped_goal_revision_stays_unscoped(self) -> None:
        snapshot = self.store.create(goal(), "conv-1", RETENTION)
        returned = self.store.replace(goal(), RETENTION, snapshot.revision)
        self.assertIsNone(returned.scope)
        self.assertIsNone(self.store.load("g1").scope)


class ScopeIsNotAStoreTests(unittest.TestCase):
    """The project-state warning, held to mechanically."""

    def test_a_project_carries_no_content_fields(self) -> None:
        """A project that accumulated content would be a second memory system."""
        fields = set(Project.__dataclass_fields__)
        self.assertEqual(
            fields, {"project_id", "name", "created_at", "status", "provenance"}
        )

    def test_the_project_store_offers_no_content_operations(self) -> None:
        for forbidden in ("remember", "record", "retrieve", "search", "append"):
            with self.subTest(operation=forbidden):
                self.assertFalse(hasattr(SQLiteProjectStore, forbidden))

    def test_the_project_module_does_not_read_other_stores(self) -> None:
        """Law 0: one owner per record. Projects must not become a reader."""
        source = (REPOSITORY_ROOT / "src/alx/projects/store.py").read_text()
        for forbidden in ("alx.memories", "alx.goals", "alx.research",
                          "alx.conversation"):
            with self.subTest(module=forbidden):
                self.assertNotIn(forbidden, source)


if __name__ == "__main__":
    unittest.main()
